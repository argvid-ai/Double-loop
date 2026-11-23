#include "MotorProtocol.h"
#include "SerialPort.h"
#include "arm_motor_bus.h"

#include <openssl/sha.h>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <csignal>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <iterator>
#include <mutex>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <vector>

namespace {
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
using Pose = std::array<double, kArmJointCount>;
using Calibration = std::array<JointCalibration, kArmJointCount>;

constexpr int rate = 100;
constexpr auto period = 10ms;
// Same advisory lock as the existing O6N followcam process.
constexpr const char* lock_path = "/tmp/r1-arm-server.lock";
volatile std::sig_atomic_t interrupted = 0;
volatile std::sig_atomic_t recordingReady = 0;
volatile std::sig_atomic_t returnRequested = 0;
volatile std::sig_atomic_t parkRequested = 0;
volatile std::sig_atomic_t shotRequested = 0;
void interrupt(int) { interrupted = 1; }
void beginRecording(int) { recordingReady = 1; }
void requestReturn(int) { returnRequested = 1; }
void requestPark(int) { parkRequested = 1; }
void requestShot(int) { shotRequested = 1; }

double wallSeconds() {
    return std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count();
}

void printPose(const Pose& q) {
    std::cout << '[';
    for (std::size_t j = 0; j < q.size(); ++j) std::cout << (j ? ", " : "") << q[j];
    std::cout << ']';
}

std::string jointName(int joint) { return "J" + std::to_string(joint + 1); }

Pose readPose(std::istream& input, const char* name) {
    Pose pose{};
    for (double& value : pose) {
        if (!(input >> value) || !std::isfinite(value)) {
            throw std::runtime_error(std::string("Invalid ") + name);
        }
    }
    return pose;
}

struct Plan {
    bool field;
    std::string calibrationHash;
    Pose low, high, speed, accel, kp{}, kd{}, torque{}, feedForward{}, velocityReference{};
    std::size_t shotEnd;
    std::vector<Pose> q;
    bool panProbe = false;
    bool passiveShoulder = false;
    bool holdStandby = false;
    bool retryFeedback = false;
};

Plan loadPlan(const char* name) {
    std::ifstream input(name);
    std::string magic, mode, digest;
    int frequency = 0;
    std::size_t count = 0;
    std::size_t shotEnd = 0;
    if (!(input >> magic >> mode >> frequency >> digest >> count >> shotEnd) ||
        magic != "R1TRAJ1" || (mode != "field" && mode != "simulation") ||
        frequency != rate || count < 3 || count > 120000 || shotEnd < 2 || shotEnd >= count) {
        throw std::runtime_error("Invalid R1 trajectory header");
    }
    Plan plan{};
    plan.field = mode == "field";
    plan.shotEnd = shotEnd;
    plan.calibrationHash = digest;
    plan.low = readPose(input, "joint minima");
    plan.high = readPose(input, "joint maxima");
    plan.speed = readPose(input, "velocity limits");
    plan.accel = readPose(input, "acceleration limits");
    if (plan.field) {
        if (digest.size() != 64 || digest.find_first_not_of("0123456789abcdef") != std::string::npos) {
            throw std::runtime_error("Invalid calibration digest");
        }
        plan.kp = readPose(input, "field Kp");
        plan.kd = readPose(input, "field Kd");
        plan.torque = readPose(input, "field torque limits");
    }
    for (int j = 0; j < kArmJointCount; ++j) {
        if (plan.low[j] < -3.5 || plan.high[j] > 3.5 || plan.low[j] >= plan.high[j] ||
            plan.speed[j] <= 0 || plan.speed[j] > 0.25 ||
            plan.accel[j] <= 0 || plan.accel[j] > 0.5) {
            throw std::runtime_error("Invalid playback limits for " + jointName(j));
        }
        if (plan.field && (plan.kp[j] <= 0 || plan.kp[j] > 100 ||
                           plan.kd[j] <= 0 || plan.kd[j] > 10 ||
                           plan.torque[j] <= 0 || plan.torque[j] > 15)) {
            throw std::runtime_error("Field gains or torque limits outside replay caps at " + jointName(j));
        }
    }
    plan.q.reserve(count);
    Pose previousSpeed{};
    for (std::size_t i = 0; i < count; ++i) {
        Pose q = readPose(input, "joint sample");
        for (int j = 0; j < kArmJointCount; ++j) {
            if (q[j] < plan.low[j] + 0.0119 || q[j] > plan.high[j] - 0.0119) {
                throw std::runtime_error("Joint limit at sample " + std::to_string(i));
            }
            if (i == 0) continue;
            const auto speed = rate * (q[j] - plan.q.back()[j]);
            if (std::abs(speed) > plan.speed[j] + 1e-6 ||
                std::abs(rate * (speed - previousSpeed[j])) > plan.accel[j] + 4e-5) {
                throw std::runtime_error("Step speed or acceleration at sample " + std::to_string(i));
            }
            previousSpeed[j] = speed;
        }
        plan.q.push_back(q);
    }
    for (int j = 0; j < kArmJointCount; ++j) {
        if (plan.q.back()[j] != plan.q.front()[j] ||
            std::abs(previousSpeed[j] * rate) > plan.accel[j] + 4e-5) {
            throw std::runtime_error("Plan must end and settle at the supported start pose");
        }
    }
    std::string extra;
    if (input >> extra) throw std::runtime_error("Unexpected data after trajectory");
    return plan;
}

Calibration readCalibration(const char* path, const std::string& expectedHash = {}) {
    std::ifstream file(path, std::ios::binary);
    if (!file) throw std::runtime_error("Cannot open calibration file");
    std::string data{std::istreambuf_iterator<char>(file), {}};
    std::array<unsigned char, SHA256_DIGEST_LENGTH> digest{};
    SHA256(reinterpret_cast<const unsigned char*>(data.data()), data.size(), digest.data());
    std::ostringstream hex;
    hex << std::hex << std::setfill('0');
    for (auto byte : digest) hex << std::setw(2) << unsigned(byte);
    if (!expectedHash.empty() && hex.str() != expectedHash) {
        throw std::runtime_error("Calibration differs from the reference file");
    }

    Calibration calibration{};
    std::array<bool, kArmJointCount> seen{};
    std::istringstream lines(data);
    for (std::string line; std::getline(lines, line);) {
        line = line.substr(0, line.find('#'));
        std::istringstream row(line);
        row >> std::ws;
        if (row.eof()) continue;
        int index;
        double dir, scale, offset;
        std::string extra;
        if (!(row >> index >> dir >> scale >> offset) || (row >> extra) ||
            index < 0 || index >= kArmJointCount || seen[index] ||
            (dir != 1 && dir != -1) || !(scale > 0 && scale <= 10) ||
            !std::isfinite(offset) || std::abs(offset) > 100) {
            throw std::runtime_error("Expected a unique joint dir scale offset row for each joint");
        }
        seen[index] = true;
        calibration[index] = {float(dir), float(scale), float(offset)};
    }
    if (!std::all_of(seen.begin(), seen.end(), [](bool value) { return value; })) {
        throw std::runtime_error("Incomplete joint calibration");
    }
    return calibration;
}

class PortLock {
    int fd_;
public:
    PortLock() : fd_(open(lock_path, O_CREAT | O_RDWR | O_CLOEXEC, 0644)) {
        if (fd_ < 0) throw std::runtime_error("Could not open arm lock");
        if (flock(fd_, LOCK_EX | LOCK_NB) != 0) {
            close(fd_);
            throw std::runtime_error("An arm process already holds the lock");
        }
    }
    ~PortLock() { close(fd_); }
};

struct PanSpeedExceeded : std::runtime_error {
    using std::runtime_error::runtime_error;
};

struct StandbyDisturbance : std::runtime_error {
    using std::runtime_error::runtime_error;
};

class Bus {
    struct Feedback {
        MotorFeedback motor{};
        Clock::time_point received{};
    };
    std::array<Feedback, kArmJointCount> feedback_{};
    std::mutex mutex_;
    std::condition_variable updated_;
    MotorProtocol decoder_, encoder_;
    SerialPort serial_;
    Calibration calibration_;
    Pose measured_{}, speeds_{}, torques_{};
    Pose driverTemp_{}, windingTemp_{};
    int fastPanSamples_ = 0;
    int fastPassiveShoulderSamples_ = 0;
    unsigned int feedbackRetries_ = 0;

    static int axisMode(const Plan& plan, int mode, int joint) {
        return plan.passiveShoulder && joint == 1 ? 0 : mode;
    }

    void sendJoint(int j, const Pose& q, const Plan& plan, int mode, double gain) {
        const auto& cal = calibration_[j];
        const float motor = mode == 0 ? 0 : (q[j] - cal.offset) / (cal.dir * cal.scale);
        const float torque = mode == 0 ? 0 : plan.feedForward[j] * gain / (cal.dir * cal.scale);
        const float velocity = mode == 0 ? 0 : plan.velocityReference[j] / (cal.dir * cal.scale);
        serial_.write(encoder_.buildControlPacket(
            j, mode, mode == 1, torque, velocity, motor, plan.kp[j] * gain, plan.kd[j] * gain));
        std::this_thread::sleep_for(plan.retryFeedback ? 350us : 200us);
    }

public:
    Bus(const char* port, const Calibration& calibration) : calibration_(calibration) {
        if (!serial_.open(port, kArmDefaultBaudrate, 0.03)) {
            throw std::runtime_error("Could not open IM6014 serial port");
        }
        serial_.startAsyncRead([this](const auto& packet) {
            auto response = decoder_.parseFeedbackPacket(packet);
            if (!response || response->motor_id >= kArmJointCount) return;
            const auto received = Clock::now();
            {
                std::lock_guard<std::mutex> lock(mutex_);
                feedback_[response->motor_id] = {*response, received};
            }
            updated_.notify_one();
        });
    }

    ~Bus() {
        serial_.stopAsyncRead();
        serial_.close();
    }

    std::array<Feedback, kArmJointCount> exchange(
        const Pose& target, const Plan& plan, int mode, double gain) {
        std::array<Clock::time_point, kArmJointCount> sent{};
        for (int j = 0; j < kArmJointCount; ++j) {
            sent[j] = Clock::now();
            sendJoint(j, target, plan, axisMode(plan, mode, j), gain);
        }
        const auto cycleStarted = sent.front();
        auto allFresh = [&] {
            for (int j = 0; j < kArmJointCount; ++j) {
                if (feedback_[j].received < sent[j]) return false;
            }
            return true;
        };
        std::array<Feedback, kArmJointCount> feedback;
        if (plan.retryFeedback) {
            std::array<bool, kArmJointCount> retry{};
            {
                std::unique_lock<std::mutex> lock(mutex_);
                updated_.wait_until(lock, cycleStarted + 7ms, allFresh);
                for (int j = 0; j < kArmJointCount; ++j) retry[j] = feedback_[j].received < sent[j];
            }
            for (int j = 0; j < kArmJointCount; ++j) {
                if (!retry[j]) continue;
                if (Clock::now() > cycleStarted + 12ms) break;
                ++feedbackRetries_;
                sent[j] = Clock::now();
                sendJoint(j, target, plan, axisMode(plan, mode, j), gain);
            }
        }
        {
            std::unique_lock<std::mutex> lock(mutex_);
            if (!updated_.wait_until(lock, cycleStarted + 15ms, allFresh)) {
                std::string missing;
                for (int j = 0; j < kArmJointCount; ++j) {
                    if (feedback_[j].received < sent[j]) missing += " " + jointName(j);
                }
                throw std::runtime_error("Missing fresh feedback at" + missing);
            }
            feedback = feedback_;
        }
        return feedback;
    }

    Pose cycle(const Pose& target, const Plan& plan, int mode, double gain = 1,
               bool enabling = false, bool* settled = nullptr, Pose* motor = nullptr, int enablingJoint = -1,
               const Pose* standbyReference = nullptr) {
        const auto feedback = exchange(target, plan, mode, gain);
        Pose measured{};
        if (settled) *settled = true;
        for (int j = 0; j < kArmJointCount; ++j) {
            const auto& r = feedback[j].motor;
            const auto& c = calibration_[j];
            const int expectedMode = axisMode(plan, mode, j);
            const bool jointEnabling = enabling && (enablingJoint < 0 || j == enablingJoint);
            const bool invalidMode = jointEnabling && expectedMode == 1 ? r.mode > 1 : r.mode != expectedMode;
            if (invalidMode || r.timeout || r.error_code || r.warning_code ||
                !std::isfinite(r.position) || !std::isfinite(r.speed) || !std::isfinite(r.torque)) {
                throw std::runtime_error("Invalid motor state at " + jointName(j));
            }
            const double previous = measured_[j];
            measured[j] = c.dir * c.scale * r.position + c.offset;
            measured_[j] = measured[j];
            speeds_[j] = c.dir * c.scale * r.speed;
            torques_[j] = c.dir * c.scale * r.torque;
            driverTemp_[j] = r.temp_driver;
            windingTemp_[j] = r.temp_winding;
            if (motor) (*motor)[j] = r.position;
            const auto speed = std::abs(r.speed * c.scale);
            if (settled && speed >= 0.04) *settled = false;
            const auto speedLimit = expectedMode == 0 ? 0.04 : plan.speed[j] + 0.12;
            bool overSpeed = speed > speedLimit;
            if (j == 1 && plan.passiveShoulder && mode == 1) {
                fastPassiveShoulderSamples_ = overSpeed ? fastPassiveShoulderSamples_ + 1 : 0;
                // J2 is supported and unpowered here. Cross-check a brief low
                // speed spike against consecutive encoder positions.
                if (overSpeed && fastPassiveShoulderSamples_ <= 2 && speed < .060 &&
                    std::abs(measured[j] - previous) < .00045 && std::abs(measured[j] - target[j]) < .004) {
                    overSpeed = false;
                }
            }
            if (plan.panProbe && j == 0) {
                // Require two samples at the lower limit for this axis' brief
                // breakaway spike. Keep the higher limit immediate.
                fastPanSamples_ = mode == 1 && overSpeed ? fastPanSamples_ + 1 : 0;
                if (mode == 1) overSpeed = fastPanSamples_ >= 2 || speed > 0.25;
            }
            const bool outsideLimits = measured[j] < plan.low[j] || measured[j] > plan.high[j] ||
                std::abs(measured[j] - target[j]) > (mode == 0 || jointEnabling ? 0.025 : 0.060) ||
                (expectedMode == 1 && std::abs(r.torque * c.scale) > plan.torque[j]);
            if (overSpeed || outsideLimits) {
                std::ostringstream detail;
                detail << "Position, speed, tracking error or torque limit at " << jointName(j)
                       << " q=" << measured[j] << " target=" << target[j]
                       << " dq=" << speeds_[j] << " tau=" << torques_[j];
                if (overSpeed && plan.panProbe && j == 0 && mode == 1 &&
                    measured[j] >= plan.low[j] && measured[j] <= plan.high[j] &&
                    std::abs(measured[j] - target[j]) <= .060 && std::abs(torques_[j]) <= plan.torque[j]) {
                    throw PanSpeedExceeded(detail.str());
                }
                // A single table bump can upset the supported idle J3 position.
                // Abort the trigger and require fresh quiet feedback before rearming.
                if (standbyReference && j == 2 && expectedMode == 1 && overSpeed && !outsideLimits &&
                    speed < .32 && std::abs(measured[j] - (*standbyReference)[j]) < .016) {
                    throw StandbyDisturbance(detail.str());
                }
                throw std::runtime_error(detail.str());
            }
        }
        return measured;
    }

    double lastPosition(int joint) const { return measured_[joint]; }
    double lastSpeed(int joint) const { return speeds_[joint]; }

    void telemetry(double elapsed, const Pose& reference) const {
        std::cout << std::fixed << std::setprecision(7)
                  << "TELEMETRY {\"wall_s\":" << wallSeconds() << ",\"elapsed_s\":" << elapsed
                  << ",\"target_rad\":";
        printPose(reference);
        std::cout << ",\"q_rad\":";
        printPose(measured_);
        std::cout << ",\"dq_rad_s\":";
        printPose(speeds_);
        std::cout << ",\"tau_nm\":";
        printPose(torques_);
        std::cout << ",\"driver_temp_c\":";
        printPose(driverTemp_);
        std::cout << ",\"winding_temp_c\":";
        printPose(windingTemp_);
        std::cout << ",\"feedback_retries\":" << feedbackRetries_;
        std::cout << "}" << std::endl;
    }

    Pose stoppedPose(Pose& raw) {
        Plan bounds{};
        bounds.low.fill(-100);
        bounds.high.fill(100);
        // Calibration output stays on the original encoder branch.
        // Use a zero-gain mode 0 query and check finiteness/state/speed in cycle().
        const auto frames = exchange({}, bounds, 0, 0);
        Pose start{};
        for (int j = 0; j < kArmJointCount; ++j) {
            const auto& motor = frames[j].motor;
            const auto& cal = calibration_[j];
            start[j] = cal.dir * cal.scale * motor.position + cal.offset;
        }
        return cycle(start, bounds, 0, 0, false, nullptr, &raw);
    }

    bool stop() {
        auto since = Clock::now();
        for (int i = 0; i < 20; ++i) {
            for (int j = 0; j < kArmJointCount; ++j) {
                try { sendJoint(j, {}, {}, 0, 0); } catch (const std::exception&) {}
            }
            std::this_thread::sleep_for(10ms);
        }
        std::lock_guard<std::mutex> lock(mutex_);
        return std::all_of(feedback_.begin(), feedback_.end(), [&](const auto& r) {
            return r.received >= since && Clock::now() - r.received < 60ms &&
                   r.motor.mode == 0 && !r.motor.error_code;
        });
    }
};

bool near(const Pose& first, const Pose& second, double tolerance) {
    for (int j = 0; j < kArmJointCount; ++j) {
        if (std::abs(first[j] - second[j]) > tolerance) return false;
    }
    return true;
}

void waitTick(Clock::time_point& next, Clock::duration interval) {
    if (interrupted) throw std::runtime_error("Interrupted by operator");
    next += interval;
    if (Clock::now() > next + 5ms) throw std::runtime_error("Playback loop missed its deadline");
    std::this_thread::sleep_until(next);
    // A late tick still sends the next sample without catching up in bursts.
    next = std::max(next, Clock::now() - 1ms);
}

double ease(double fraction) {
    return fraction * fraction * fraction * (10 + fraction * (-15 + 6 * fraction));
}

std::optional<std::size_t> selectedShot(std::size_t generation, std::size_t count) {
    char packet[64];
    const auto length = recv(STDIN_FILENO, packet, sizeof(packet), MSG_DONTWAIT | MSG_TRUNC);
    if (length < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) return std::nullopt;
    if (length <= 0) throw std::runtime_error("Director control socket disconnected");
    if (length > static_cast<ssize_t>(sizeof(packet))) return std::nullopt;
    const char* begin = packet;
    const char* end = begin + length;
    const char* separator = std::find(begin, end, ' ');
    if (separator == end) return std::nullopt;
    std::size_t received{}, index{};
    const auto id = std::from_chars(begin, separator, received);
    const auto shot = std::from_chars(separator + 1, end, index);
    if (id.ec != std::errc{} || id.ptr != separator || shot.ec != std::errc{} || shot.ptr != end ||
        received != generation || index >= count) return std::nullopt;
    return index;
}

void requireDirectorSocket() {
    int type{};
    socklen_t size = sizeof(type);
    if (getsockopt(STDIN_FILENO, SOL_SOCKET, SO_TYPE, &type, &size) || type != SOCK_SEQPACKET) {
        throw std::runtime_error("Use director/field_session.py for camera-triggered playback");
    }
}

void runPlayback(Bus& bus, const Plan& firstPlan, Clock::duration interval,
                 double leftMinimum = 0, double rightMinimum = 0, const Pose* approved = nullptr,
                 const std::vector<Plan>* standbySequence = nullptr, bool directorControl = false) {
    const int perSecond = 1s / interval;
    double left = 0, right = 0;
    try {
        if (interrupted) throw std::runtime_error("Interrupted by operator");
        const auto& start = firstPlan.q.front();
        Pose measured = bus.cycle(start, firstPlan, 0, 0);
        if (!near(measured, start, 0.012)) throw std::runtime_error("Arm is not at the surveyed start");
        if (approved && !near(measured, *approved, .008)) {
            throw std::runtime_error("Arm is not at the approved standby");
        }
        auto next = Clock::now();
        for (int i = 0; i < perSecond / 2; ++i) {
            waitTick(next, interval);
            measured = bus.cycle(start, firstPlan, 1, std::min(1.0, (i + 1) / (0.3 * perSecond)), i < 5);
        }
        if (!near(measured, start, 0.012)) throw std::runtime_error("Arm did not settle at the start");
        const auto started = Clock::now();
        auto lastTelemetry = started - 1s;
        if (firstPlan.holdStandby) {
            const auto holdBegin = started;
            auto stableSince = holdBegin;
            bool stable = false;
            while (Clock::now() - holdBegin < 9s) {
                waitTick(next, interval);
                bool still = false;
                measured = bus.cycle(start, firstPlan, 1, 1, false, &still);
                const auto now = Clock::now();
                if (!still || !near(measured, start, 0.012) || (approved && !near(measured, *approved, .008))) {
                    stableSince = now;
                }
                if (now - lastTelemetry >= 200ms) {
                    bus.telemetry(std::chrono::duration<double>(now - holdBegin).count(), start);
                    lastTelemetry = now;
                }
                if (now - stableSince >= 5s) {
                    stable = true;
                    break;
                }
            }
            if (!stable) throw std::runtime_error("Standby was not stable for five continuous seconds");
            std::cout << "STANDBY_HELD_5S" << std::endl;
        }
        auto play = [&](const Plan& plan) {
            const auto shotStarted = standbySequence ? Clock::now() : started;
            std::cout << "PLAYBACK_START" << std::endl;
            for (std::size_t index = 0; index < plan.q.size(); ++index) {
                const auto& target = plan.q[index];
                waitTick(next, interval);
                Pose limited{};
                for (int j = 0; j < kArmJointCount; ++j) {
                    limited[j] = std::clamp(target[j], measured[j] - 0.035, measured[j] + 0.035);
                }
                measured = bus.cycle(limited, plan, 1);
                const auto now = Clock::now();
                if (now - lastTelemetry >= 200ms || index + 1 == plan.q.size()) {
                    bus.telemetry(std::chrono::duration<double>(now - shotStarted).count(), target);
                    lastTelemetry = now;
                }
                if (leftMinimum > 0) {
                    left = std::min(left, measured[0] - start[0]);
                    right = std::max(right, measured[0] - start[0]);
                }
                if (!near(measured, target, 0.055)) throw std::runtime_error("Excessive trajectory lag");
                if (index == plan.shotEnd) std::cout << "PLAYBACK_RETURN" << std::endl;
            }
            int stable = 0;
            const int stableTicks = perSecond * 4 / 10;
            for (int i = 0; i < perSecond * 3 / 2 && stable < stableTicks; ++i) {
                waitTick(next, interval);
                bool settled = false;
                measured = bus.cycle(start, plan, 1, 1, false, &settled);
                stable = settled && near(measured, start, .012) &&
                         (!standbySequence || near(measured, *approved, .008)) ? stable + 1 : 0;
            }
            if (stable < stableTicks) throw std::runtime_error("Did not settle at the supported start");
        };
        if (standbySequence) {
            std::size_t index = 0;
            std::size_t generation = 0;
            while (!interrupted && !parkRequested) {
                const auto& holdPlan = directorControl ? firstPlan : standbySequence->at(index);
                shotRequested = 0;
                if (directorControl) {
                    std::cout << "DIRECTOR_STANDBY " << ++generation << std::endl;
                } else {
                    std::cout << "POWERED_STANDBY next_shot=" << index + 1 << std::endl;
                }
                const int stableTicks = perSecond * 4 / 10;
                int stable = stableTicks;
                std::optional<std::size_t> chosen;
                do {
                    if (interrupted || parkRequested) break;
                    waitTick(next, interval);
                    bool still = false;
                    measured = bus.cycle(start, holdPlan, 1, 1, false, &still);
                    if (still && near(measured, *approved, .008)) {
                        stable = std::min(stable + 1, stableTicks);
                    } else {
                        stable = 0;
                    }
                    if (directorControl) {
                        if (auto request = selectedShot(generation, standbySequence->size())) {
                            if (stable == stableTicks) chosen = request;
                            else std::cout << "DIRECTOR_REJECTED " << generation << std::endl;
                        }
                    } else if (shotRequested && stable == stableTicks) {
                        chosen = index;
                    }
                } while (!chosen);
                if (interrupted || parkRequested) break;
                index = *chosen;
                if (directorControl) std::cout << "DIRECTOR_ACCEPTED " << generation << ' ' << index << std::endl;
                play(standbySequence->at(index));
                std::cout << "RETURNED_POWERED_STANDBY shot=" << index + 1 << std::endl;
                index = (index + 1) % standbySequence->size();
            }
        } else {
            play(firstPlan);
        }
    } catch (...) {
        if (!bus.stop()) std::cerr << "Motor stop could not be verified on all joints\n";
        throw;
    }
    if (!bus.stop()) throw std::runtime_error("Motor stop could not be verified on all joints");
    std::cout << (standbySequence ? "Ended standby session and verified mode 0 on all joints\n"
                                 : "Returned to start and verified mode 0 on all joints\n");
    if (leftMinimum > 0) {
        constexpr double degrees = 180.0 / 3.141592653589793;
        std::cout << "Measured J1 offsets: " << left * degrees << " / " << right * degrees << " deg\n";
        if (left > -leftMinimum || right < rightMinimum) {
            throw std::runtime_error("Measured J1 swing did not reach half the target on both sides");
        }
    }
}

Pose readApprovedStandby(const char* path, const Calibration& calibration) {
    std::ifstream standbyFile(path);
    std::string standbyHash, kind;
    if (!(standbyFile >> standbyHash) || standbyHash.size() != 64) {
        throw std::runtime_error("Invalid approved standby reference");
    }
    readPose(standbyFile, "approved standby in SDK coordinates");
    if (!(standbyFile >> kind) || kind != "standby") throw std::runtime_error("Expected an approved standby pose");
    const Pose motorPose = readPose(standbyFile, "approved standby motor angles");
    Pose approved{};
    for (int joint = 0; joint < kArmJointCount; ++joint) {
        const auto& cal = calibration[joint];
        approved[joint] = cal.dir * cal.scale * motorPose[joint] + cal.offset;
    }
    return approved;
}

void validateStandbySequence(const std::vector<Plan>& plans, const Pose& approved) {
    const auto& first = plans.front();
    for (const auto& plan : plans) {
        if (!near(plan.q.front(), approved, .008) || plan.q.front() != first.q.front()) {
            throw std::runtime_error("All field trajectories must share the approved standby start");
        }
        if (plan.calibrationHash != first.calibrationHash || plan.kp != first.kp ||
            plan.kd != first.kd || plan.torque != first.torque) {
            throw std::runtime_error("All field trajectories must share the same calibration and hold settings");
        }
    }
}

Pose sampleStandstill(Bus& bus, Pose& raw) {
    constexpr auto interval = 40ms;
    Pose first = bus.stoppedPose(raw);
    Pose current = first;
    for (int i = 0; i < 6; ++i) {
        if (interrupted) throw std::runtime_error("Interrupted by operator");
        std::this_thread::sleep_for(interval);
        current = bus.stoppedPose(raw);
        if (!near(current, first, 0.008)) throw std::runtime_error("Arm moved during the stopped snapshot");
    }
    return current;
}

void snapshot(const char* port, const char* calibrationFile) {
    const auto calibration = readCalibration(calibrationFile);
    PortLock lock;
    Bus bus(port, calibration);
    Pose raw{};
    const Pose pose = sampleStandstill(bus, raw);
    std::cout << std::fixed << std::setprecision(7)
              << "SNAPSHOT {\"wall_s\":" << wallSeconds() << ",\"mode\":0,\"q_sdk_rad\":";
    printPose(pose);
    std::cout << ",\"q_motor_rad\":";
    printPose(raw);
    std::cout << "}\n";
}

Pose readSnapshotReference(const char* referenceFile, std::string& digest) {
    std::ifstream reference(referenceFile);
    std::string extra;
    if (!(reference >> digest) || digest.size() != 64) throw std::runtime_error("Invalid snapshot digest");
    const auto pose = readPose(reference, "stopped snapshot pose");
    if (reference >> extra) throw std::runtime_error("Unexpected data after snapshot pose");
    return pose;
}

void holdSnapshot(const char* port, const char* calibrationFile, const char* referenceFile,
                  const char* startFile = nullptr, const char* clearanceFile = nullptr) {
    std::string digest;
    const Pose target = readSnapshotReference(referenceFile, digest);
    const auto calibration = readCalibration(calibrationFile, digest);
    double openElbow = target[3];
    if (clearanceFile) {
        std::ifstream clearance(clearanceFile);
        std::string clearanceDigest, trailing;
        if (!(clearance >> clearanceDigest >> openElbow) || clearanceDigest != digest ||
            !std::isfinite(openElbow) || openElbow <= target[3] || openElbow > -3.51 ||
            (clearance >> trailing)) {
            throw std::runtime_error("Invalid surveyed elbow waypoint");
        }
    }
    Pose referenceStart = target;
    if (startFile) {
        std::string startDigest;
        referenceStart = readSnapshotReference(startFile, startDigest);
        if (startDigest != digest) throw std::runtime_error("Snapshot calibrations differ");
        const Pose maxTravel{clearanceFile ? .045 : .020, .165, clearanceFile ? .055 : .040,
                             .180, .035, .035, .025};
        for (int joint = 0; joint < kArmJointCount; ++joint) {
            if (std::abs(target[joint] - referenceStart[joint]) > maxTravel[joint]) {
                throw std::runtime_error("Snapshot return exceeds its small local travel at " + jointName(joint));
            }
        }
        if (target[3] <= referenceStart[3] || target[1] <= referenceStart[1]) {
            throw std::runtime_error("This return requires the elbow to open before lowering the shoulder");
        }
        if (openElbow - referenceStart[3] > .48) {
            throw std::runtime_error("Temporary elbow opening exceeds the checked local travel");
        }
    }
    PortLock lock;
    Bus bus(port, calibration);
    Pose motor{};
    Pose measured = sampleStandstill(bus, motor);
    if (!near(measured, referenceStart, .008)) {
        std::cout << "HOLD_START_MISMATCH ";
        printPose(measured);
        std::cout << std::endl;
        throw std::runtime_error("The arm is not at the saved pose. Position control was not enabled.");
    }

    Plan hold{};
    hold.retryFeedback = true;
    hold.kp.fill(20.);
    hold.kd.fill(1.);
    hold.speed.fill(.02);
    hold.torque = {4., 7., 4., 5., 3., 2.5, 2.5};
    if (clearanceFile) hold.torque[1] = 4.5;
    for (int joint = 0; joint < kArmJointCount; ++joint) {
        hold.low[joint] = std::min(referenceStart[joint], target[joint]) - .020;
        hold.high[joint] = std::max(referenceStart[joint], target[joint]) + .020;
    }
    hold.high[3] = openElbow + .020;

    constexpr auto tick = 20ms;
    const auto started = Clock::now();
    auto next = started;
    auto lastReport = started;
    int stableSamples = 0;
    bool holding = false;
    try {
        if (startFile) {
            hold.passiveShoulder = true;
            hold.kp[1] = hold.kp[3] = 85.;
            hold.kd[1] = clearanceFile ? 7. : 3.5;
            hold.kd[3] = 8.;
            if (clearanceFile) {
                hold.kp[0] = 40.;
                hold.kd[0] = 5.;
            }
            const Pose observed = measured;
            Pose setpoint = observed;
            double stationaryElbow = openElbow;
            bool shoulderReached = false;
            auto step = [&](double gain, bool enabling, int enablingJoint = -1, int movingJoint = -1) {
                waitTick(next, tick);
                bool still = false;
                measured = bus.cycle(setpoint, hold, 1, gain, enabling, &still, nullptr, enablingJoint);
                const auto now = Clock::now();
                Pose checkTarget = setpoint;
                if (movingJoint == 3 || (clearanceFile && movingJoint == 1)) {
                    checkTarget[movingJoint] = measured[movingJoint];
                }
                if ((hold.passiveShoulder && std::abs(measured[1] - observed[1]) > .012) ||
                    (!hold.passiveShoulder && movingJoint != 3 &&
                     std::abs(measured[3] - stationaryElbow) > .016) ||
                    (shoulderReached && std::abs(measured[1] - target[1]) > .016) ||
                    !near(measured, checkTarget, .034)) {
                    throw std::runtime_error("Snapshot return lost tracking or support");
                }
                if (now - lastReport >= (clearanceFile ? 200ms : 1s)) {
                    bus.telemetry(std::chrono::duration<double>(now - started).count(), setpoint);
                    lastReport = now;
                }
                return still;
            };
            for (int i = 1; i <= 40; ++i) step(std::min(1., i / 30.), i <= 5);
            auto moveTo = [&](int joint, double destination) {
                const double from = measured[joint];
                const double delta = destination - from;
                if (joint == 1) {
                    setpoint[1] = from;
                    hold.passiveShoulder = false;
                    for (int i = 0; i < 20; ++i) step(1., i < 5, 1);
                }
                std::cout << "SNAPSHOT_RETURN_MOVE " << jointName(joint) << std::endl;
                const bool assistedShoulder = clearanceFile && joint == 1;
                const double peakSpeed = joint == 3 || assistedShoulder ? .012 : .022;
                // Recorded elbow openings required about 4 Nm (0.047 rad at
                // this gain). For a verified clearance route, also allow the
                // larger shoulder lead while retaining its 4.5 Nm feedback cap.
                const double lead = joint == 3 ? .049 : assistedShoulder ? .046 : .026;
                const int frames = std::max(60, int(std::ceil(1.875 * std::abs(delta) / (.020 * peakSpeed))));
                int waits = 0;
                for (int i = 1; i <= frames;) {
                    const double goal = from + delta * ease(double(i) / frames);
                    setpoint[joint] = std::clamp(goal, measured[joint] - lead, measured[joint] + lead);
                    step(1., false, -1, joint);
                    if (std::abs(bus.lastSpeed(joint)) > .10) {
                        bus.telemetry(std::chrono::duration<double>(Clock::now() - started).count(), setpoint);
                        throw std::runtime_error("Snapshot return exceeded the speed limit at " + jointName(joint));
                    }
                    if (std::abs(goal - measured[joint]) > lead) {
                        if (++waits > 90) {
                            throw std::runtime_error("Snapshot return stalled at " + jointName(joint));
                        }
                    } else {
                        waits = 0;
                        ++i;
                    }
                }
                int settled = 0;
                const int settleFrames = joint == 3 || assistedShoulder ? 400 : 200;
                for (int i = 0; i < settleFrames && settled < 25; ++i) {
                    const double error = destination - measured[joint];
                    if ((joint == 3 || assistedShoulder) && std::abs(error) <= .043 &&
                        std::abs(bus.lastSpeed(joint)) < .020 &&
                        std::abs(measured[joint] - from) >= .60 * std::abs(delta)) {
                        auto& assist = hold.feedForward[joint];
                        const double maxAssist = assistedShoulder ? 2.4 : 2.8;
                        assist = std::clamp(assist + std::clamp(.65 * error, -.014, .014), -maxAssist, maxAssist);
                    }
                    setpoint[joint] = std::clamp(destination, measured[joint] - lead, measured[joint] + lead);
                    settled = step(1., false, -1, joint) && std::abs(measured[joint] - destination) <= .009
                                  ? settled + 1 : 0;
                }
                if (settled < 25) throw std::runtime_error("Snapshot return did not settle at " + jointName(joint));
                std::cout << "SNAPSHOT_RETURN_REACHED " << jointName(joint) << std::endl;
            };
            moveTo(3, openElbow);
            if (clearanceFile) {
                std::cout << "SNAPSHOT_ELBOW_CLEAR " << measured[3] << std::endl;
            }
            moveTo(1, target[1]);
            shoulderReached = true;
            if (clearanceFile) {
                moveTo(3, target[3]);
                stationaryElbow = target[3];
            }
            for (int joint : {2, 4, 5, 6, 0}) moveTo(joint, target[joint]);
            for (int joint = 0; joint < kArmJointCount; ++joint) {
                hold.low[joint] = target[joint] - .020;
                hold.high[joint] = target[joint] + .020;
            }
        }
        const auto holdStarted = Clock::now();
        for (int sample = startFile ? 50 : 0; !interrupted; sample = std::min(sample + 1, 50)) {
            waitTick(next, tick);
            if (interrupted) break;
            bool still = false;
            const auto actual = bus.cycle(target, hold, 1, std::min(1., (sample + 1) / 30.),
                                          sample < 5, &still);
            const auto now = Clock::now();
            stableSamples = sample >= 30 && still && near(actual, target, .012) ? stableSamples + 1 : 0;
            if (!holding && stableSamples >= 50) {
                holding = true;
                std::cout << "SNAPSHOT_HOLD_READY" << std::endl;
            }
            if (!holding && now - holdStarted > 5s) throw std::runtime_error("Snapshot hold did not settle");
            if (now - lastReport >= 1s) {
                bus.telemetry(std::chrono::duration<double>(now - started).count(), target);
                lastReport = now;
            }
        }
    } catch (...) {
        const bool stopped = bus.stop();
        if (!stopped) std::cerr << "Motor stop could not be verified on all joints\n";
        if (!interrupted || !stopped) throw;
        std::cout << "SNAPSHOT_HOLD_STOPPED mode=0 on all joints" << std::endl;
        return;
    }
    if (!bus.stop()) throw std::runtime_error("Motor stop could not be verified on all joints");
    std::cout << "SNAPSHOT_HOLD_STOPPED mode=0 on all joints" << std::endl;
}

void openElbow(const char* port, const char* calibrationFile, const char* referenceFile) {
    std::ifstream reference(referenceFile);
    std::string digest, kind;
    if (!(reference >> digest) || digest.size() != 64) throw std::runtime_error("Invalid standby reference");
    const Pose observed = readPose(reference, "verified start pose");
    if (!(reference >> kind) || kind != "standby") throw std::runtime_error("Expected a taught standby");
    const auto calibration = readCalibration(calibrationFile, digest);
    PortLock lock;
    Bus bus(port, calibration);
    Pose raw{};
    const Pose start = sampleStandstill(bus, raw);
    auto stationaryAxes = start;
    stationaryAxes[3] = observed[3];
    stationaryAxes[6] = observed[6];
    if (!near(stationaryAxes, observed, 0.012) ||
        std::abs(start[3] - observed[3]) > 0.03 || std::abs(start[6] - observed[6]) > 0.03 ||
        start[1] < 1.24 || start[1] > 1.305 ||
        start[3] < -4.03 || start[3] > -3.54 || start[6] < -3.72 || start[6] > -3.49) {
        throw std::runtime_error("Elbow opening start differs from the surveyed clearance envelope");
    }
    constexpr auto interval = 20ms;
    constexpr double elbowGoal = -3.49;
    constexpr double minWrist = -4.065;
    Plan plan{};
    plan.passiveShoulder = true;
    plan.kp.fill(60);
    plan.kd.fill(1);
    plan.kp[3] = 85;
    plan.kd[3] = 2;
    plan.speed.fill(0.08);
    plan.torque = {8, 7.5, 8, 7.5, 5, 5, 2.6};
    for (int j = 0; j < kArmJointCount; ++j) {
        plan.low[j] = start[j] - 0.037;
        plan.high[j] = start[j] + 0.037;
    }
    plan.low[1] = start[1] - 0.012;
    plan.high[1] = std::min(1.305, start[1] + 0.012);
    plan.low[3] = -4.055;
    plan.high[3] = -3.43;
    plan.low[6] = minWrist;
    auto next = Clock::now();
    Pose measured = start;
    try {
        for (int sample = 0; sample < 25; ++sample) {
            waitTick(next, interval);
            measured = bus.cycle(start, plan, 1, std::min(1.0, (sample + 1) / 15.0), sample < 5);
        }
        if (!near(measured, start, 0.012)) throw std::runtime_error("Arm did not hold the surveyed start");
        std::cout << "OPENING_J4 J2_MODE_0" << std::endl;
        const auto opened = Clock::now();
        int stableTicks = 0;
        for (int sample = 0; sample < 1050; ++sample) {
            waitTick(next, interval);
            const double u = std::min(1.0, (sample + 1) / 700.0);
            const double blend = ease(u);
            Pose target = start;
            target[3] += blend * (elbowGoal - start[3]);
            target[6] = std::clamp(start[6] - 0.9 * (measured[3] - start[3]), minWrist, start[6]);
            Pose command = target;
            command[3] = std::clamp(target[3], measured[3] - 0.050, measured[3] + 0.050);
            command[6] = std::clamp(target[6], measured[6] - 0.025, measured[6] + 0.025);
            command[6] = std::clamp(command[6], minWrist, start[6]);
            bool settled = false;
            measured = bus.cycle(command, plan, 1, 1, false, &settled);
            if (sample % 10 == 0) {
                bus.telemetry(std::chrono::duration<double>(Clock::now() - opened).count(), target);
            }
            if (std::abs(measured[3] - target[3]) > 0.10 || std::abs(measured[6] - target[6]) > 0.06) {
                throw std::runtime_error("Excessive J4/J7 opening tracking error");
            }
            const bool clear = measured[3] >= -3.54 && std::abs(measured[6] - target[6]) < 0.035;
            stableTicks = u >= 1 && settled && clear ? stableTicks + 1 : 0;
            if (stableTicks == 250) break;
        }
        if (stableTicks < 250) throw std::runtime_error("Could not verify five seconds at the elbow opening");
        std::cout << "ELBOW_OPEN_HELD_5S " << measured[3] << " J7 " << measured[6] << std::endl;
    } catch (...) {
        if (!bus.stop()) std::cerr << "Motor stop could not be verified on all joints\n";
        throw;
    }
    if (!bus.stop()) throw std::runtime_error("Motor stop could not be verified on all joints");
    measured = sampleStandstill(bus, raw);
    std::cout << "OPEN_STOPPED ";
    printPose(measured);
    std::cout << '\n';
    if (measured[3] < -3.54) throw std::runtime_error("Elbow opening was not retained after stop");
}

void panSmoke(const char* port, const char* calibrationFile, const char* referenceFile,
              double swingDegrees, bool wide) {
    if (!std::isfinite(swingDegrees) || swingDegrees < 2 || swingDegrees > 6) {
        throw std::runtime_error("Pan angle must be from 2 to 6 degrees");
    }
    std::ifstream reference(referenceFile);
    std::string digest;
    if (!(reference >> digest) || digest.size() != 64) throw std::runtime_error("Invalid pan reference");
    const Pose observed = readPose(reference, "verified start pose");
    std::string referenceKind;
    reference >> referenceKind;
    if (referenceKind != "standby") throw std::runtime_error("Pan requires an approved standby reference");
    const auto calibration = readCalibration(calibrationFile, digest);
    PortLock lock;
    Bus bus(port, calibration);
    Pose raw{};
    const Pose measuredStart = sampleStandstill(bus, raw);
    const Pose start = observed;
    if (!near(measuredStart, observed, .008) ||
        std::max(start[1], measuredStart[1]) > 1.292 ||
        std::min(start[3], measuredStart[3]) < -4.04) {
        throw std::runtime_error("Start differs from the logged supported pose; capture a snapshot before moving");
    }
    if (wide && (std::max(start[1], measuredStart[1]) > 1.27 ||
                 std::min(start[3], measuredStart[3]) < -3.54)) {
        throw std::runtime_error("Wide pan needs J4 open and J2 clear of the lower boundary");
    }

    constexpr double turn = 6.283185307179586;
    const double swing = swingDegrees * turn / 360;
    const double left = wide ? 8 * turn / 360 : swing;
    const double right = wide ? 3 * turn / 360 : swing;
    if (std::abs(std::remainder(start[0], turn)) + std::max(left, right) + 0.06 > 2.70) {
        throw std::runtime_error("J1 sweep would approach its model limit");
    }
    if (wide && (start[0] - left < -7.771 || start[0] + right > -7.504)) {
        throw std::runtime_error("Wide pan target exceeds the observed J1 range");
    }
    Plan plan{};
    plan.panProbe = true;
    plan.holdStandby = true;
    plan.kp.fill(60);
    plan.kd.fill(1);
    // A stiction breakaway on the loaded J1 passed the speed guard at 60/1.
    plan.kp[0] = 40;
    plan.kd[0] = wide ? 5 : 3;
    plan.speed.fill(0.08);
    plan.torque = {8, 7.5, 8, 7.5, 5, 5, 2.6};
    for (int j = 0; j < kArmJointCount; ++j) {
        plan.low[j] = start[j] - 0.05;
        plan.high[j] = start[j] + 0.05;
    }
    plan.low[0] -= left;
    plan.high[0] += right;
    plan.high[1] = std::min(plan.high[1], 1.305);
    plan.low[3] = std::max(plan.low[3], -4.055);
    if (wide) {
        plan.low[0] = std::max(plan.low[0], -7.771);
        plan.high[0] = std::min(plan.high[0], -7.504);
        plan.high[1] = std::min(plan.high[1], 1.27);
        plan.low[3] = std::max(plan.low[3], -3.54);
    }
    constexpr auto interval = 20ms;
    constexpr int holdTicks = 40;
    plan.q.push_back(start);
    double from = 0;
    for (double to : {-left, 0.0, right, 0.0}) {
        const int travelTicks = wide ? (std::abs(to - from) > right + 0.001 ? 500 : 200) : 150;
        for (int k = 1; k <= travelTicks; ++k) {
            const double u = double(k) / travelTicks;
            const double blend = ease(u);
            Pose target = start;
            target[0] += from + blend * (to - from);
            plan.q.push_back(target);
        }
        const Pose held = plan.q.back();
        plan.q.insert(plan.q.end(), holdTicks, held);
        from = to;
    }
    plan.shotEnd = plan.q.size() - holdTicks;
    std::cout << "J1 pan: center / left / center / right / center; left "
              << left * 360 / turn << " deg, right " << right * 360 / turn << " deg; 50 Hz" << std::endl;
    runPlayback(bus, plan, interval, left / 2, right / 2, &observed);
}

enum class PoweredShot { Pan, PanTilt, Tabletop, LateralProbe, Lateral, StandbyRehearsal, LevelRetreat,
                         AmplitudeProbe, LevelDirector };
constexpr std::array<std::string_view, 4> directorRehearsalRoutes{
    "ConversationSweep", "GroupWide", "FollowLeft", "FollowRight",
};
constexpr std::array<int, 3> levelDirectorJoints{2, 3, 6};

struct LevelDirectorPlan {
    Pose levelDelta, retreatDelta;
    double left, right;
};

LevelDirectorPlan readLevelDirectorPlan(const char* path, const std::string& digest, const Pose& approved) {
    std::ifstream input(path);
    std::string magic, hash, trailing;
    if (!(input >> magic >> hash) || magic != "R1LEVEL1" || hash != digest) {
        throw std::runtime_error("Level rehearsal needs the approved motor calibration");
    }
    const Pose origin = readPose(input, "level rehearsal standby");
    LevelDirectorPlan result{};
    result.levelDelta = readPose(input, "level joint offsets");
    result.retreatDelta = readPose(input, "level retreat joint offsets");
    if (!near(approved, origin, .000001) || !(input >> result.left >> result.right) || (input >> trailing) ||
        !std::isfinite(result.left) || !std::isfinite(result.right) || result.left < -.202 || result.left >= 0 ||
        result.right <= 0 || result.right > .039) {
        throw std::runtime_error("Level rehearsal needs the recorded standby and surveyed yaw endpoints");
    }
    for (const auto& pose : {result.levelDelta, result.retreatDelta}) {
        for (int joint : {0, 1, 4, 5}) {
            if (pose[joint] != 0) throw std::runtime_error("Level rehearsal only compensates J3, J4, and J7");
        }
        if (pose[2] < -.0201 || pose[2] > 0 || pose[3] < 0 || pose[3] > .0245 || pose[6] <= 0 || pose[6] > .1053) {
            throw std::runtime_error("Level rehearsal exceeds an observed joint excursion");
        }
    }
    if (result.levelDelta[3] != 0 || result.retreatDelta[3] <= 0 || result.retreatDelta[6] >= result.levelDelta[6]) {
        throw std::runtime_error("Expected elbow retreat with opposite wrist compensation");
    }
    return result;
}

Pose trimFoldedPose(Bus& bus, const Pose& observed, const Pose& reference, Pose& staticAssist) {
    constexpr auto interval = 20ms;
    constexpr Pose maximumTrim{.055, .004, .12, .004, .11, .055, .34};
    // J3 has a larger measured range while J2 and J4 are supported at park.
    // Use only the local portion for the low-speed, low-torque return.
    constexpr Pose low{-7.74, 1.27, 3.50, -4.035, -5.12, 3.995, -4.075};
    constexpr Pose high{-7.505, 1.292, 3.77, -3.99, -4.97, 4.09, -3.55};
    for (int j = 0; j < kArmJointCount; ++j) {
        if (observed[j] < low[j] || observed[j] > high[j] || reference[j] < low[j] || reference[j] > high[j] ||
            std::abs(observed[j] - reference[j]) > maximumTrim[j]) {
            throw std::runtime_error("Folded correction is outside the checked range at " + jointName(j));
        }
    }
    Plan trim{};
    trim.passiveShoulder = true;
    trim.retryFeedback = true;
    trim.kp = {40, 85, 60, 85, 45, 50, 50};
    trim.kd = {5, 3.5, 1, 2, 4, 3, 3};
    trim.torque = {8, 7.5, 1.8, 7.5, 5, 5, 2.6};
    trim.speed.fill(.035);
    trim.low = low;
    trim.high = high;
    Pose measured = observed;
    Pose target = observed;
    const auto began = Clock::now();
    auto next = began;
    int ticks = 0;
    auto cycle = [&](int axis, double gain = 1., bool enabling = false) {
        if (parkRequested) throw std::runtime_error("Folded correction stopped by operator");
        waitTick(next, interval);
        Pose command = target;
        if (axis >= 0) command[axis] = std::clamp(target[axis], measured[axis] - .025, measured[axis] + .025);
        bool still = false;
        measured = bus.cycle(command, trim, 1, gain, enabling, &still);
        if ((axis >= 0 && (std::abs(measured[axis] - target[axis]) > .045 || std::abs(bus.lastSpeed(axis)) > .12)) ||
            std::abs(measured[1] - observed[1]) > .004 || std::abs(measured[3] - observed[3]) > .012) {
            throw std::runtime_error("Folded correction exceeded the tracking or support limit");
        }
        if (++ticks % 10 == 0) bus.telemetry(std::chrono::duration<double>(Clock::now() - began).count(), target);
        return still;
    };
    std::cout << "FOLDED_TRIM_START ";
    printPose(observed);
    std::cout << std::endl;
    for (int frame = 1; frame <= 50; ++frame) cycle(-1, std::min(1., frame / 30.), frame <= 8);
    if (!near(measured, observed, .010)) throw std::runtime_error("Arm did not hold the current folded pose");

    // Keep the base and both lifting axes stationary. The existing opening route centers J1 later.
    for (int j : {2, 4, 5, 6}) {
        const double from = target[j];
        const double delta = reference[j] - from;
        if (std::abs(measured[j] - reference[j]) <= .004) continue;
        std::cout << "FOLDED_TRIM_AXIS " << jointName(j) << ' ' << measured[j] << " -> " << reference[j] << std::endl;
        const double trimSpeed = j == 2 ? .014 : .024;
        const int frames = std::max(100, int(std::ceil(2.1875 * std::abs(delta) / (trimSpeed * .020))));
        for (int sample = 1; sample <= frames; ++sample) {
            const double u = double(sample) / frames;
            const double v = 1 - u;
            target[j] = from + delta * u*u*u*u*(35+u*(-84+u*(70-20*u)));
            trim.velocityReference[j] = delta * 140*u*u*u*v*v*v / (frames * .020);
            cycle(j);
        }
        trim.velocityReference[j] = 0;
        int stable = 0;
        for (int sample = 0; sample < 220 && stable < 25; ++sample) {
            const double error = reference[j] - measured[j];
            // Recover only the small steady-state residual, never push through a large lag.
            if (std::abs(error) > .004 && std::abs(error) <= .020 && std::abs(bus.lastSpeed(j)) < .018) {
                trim.feedForward[j] = std::clamp(trim.feedForward[j] + std::clamp(.65 * error, -.006, .006),
                                                 -.55, .55);
            }
            const bool still = cycle(j);
            stable = still && std::abs(measured[j] - reference[j]) <= .006 ? stable + 1 : 0;
        }
        if (stable < 25) throw std::runtime_error("Folded correction did not settle at " + jointName(j));
    }
    Pose correctedReference = reference;
    correctedReference[0] = observed[0];
    int held = 0, noisy = 0;
    for (int sample = 0; sample < 200 && held < 50; ++sample) {
        const bool still = cycle(-1);
        if (!near(measured, correctedReference, .008)) {
            held = noisy = 0;
        } else if (still) {
            ++held;
            noisy = 0;
        } else if (++noisy > 2) {
            held = 0;
        }
    }
    if (held != 50) throw std::runtime_error("Corrected folded pose did not remain at the reference");
    std::cout << "FOLDED_TRIM_HELD ";
    printPose(measured);
    std::cout << std::endl;
    staticAssist = trim.feedForward;
    return measured;
}

void poweredShot(const char* port, const char* calibrationFile, const char* referenceFile,
                 const char* standbyFile, PoweredShot shot, const char* lateralPlanFile = nullptr,
                 bool directorRehearsal = false, bool autoTrim = false,
                 std::optional<double> levelWrist = std::nullopt, bool framePeople = false) {
    std::ifstream reference(referenceFile);
    std::string digest, kind;
    if (!(reference >> digest) || digest.size() != 64) throw std::runtime_error("Invalid park reference");
    const Pose surveyed = readPose(reference, "surveyed park");
    if (!(reference >> kind) || kind != "park") throw std::runtime_error("Expected a surveyed park pose");
    std::ifstream standbyInput(standbyFile);
    std::string standbyDigest;
    if (!(standbyInput >> standbyDigest) || standbyDigest != digest) {
        throw std::runtime_error("Park and approved standby need the same calibration");
    }
    const Pose approved = readPose(standbyInput, "approved standby");
    if (!(standbyInput >> kind) || kind != "standby") throw std::runtime_error("Expected an approved standby pose");
    constexpr double rad = 3.141592653589793 / 180;
    constexpr double wristLow = -4.065;
    constexpr double wristFeedbackMargin = .002;
    constexpr double pitchLift = 4 * rad;
    constexpr double levelLift = 6 * rad;
    constexpr double retreatElbow = .5 * rad;
    constexpr double retreatWrist = -.509679 * rad;
    const bool levelRetreat = shot == PoweredShot::LevelRetreat;
    const bool amplitudeProbe = shot == PoweredShot::AmplitudeProbe;
    const bool levelDirector = shot == PoweredShot::LevelDirector;
    LevelDirectorPlan levelMotion{};
    if (levelDirector) levelMotion = readLevelDirectorPlan(lateralPlanFile, digest, approved);
    if (levelRetreat && (!levelWrist || !std::isfinite(*levelWrist) ||
        *levelWrist + retreatWrist < wristLow + .25 * rad ||
        *levelWrist <= approved[6] || *levelWrist > approved[6] + levelLift)) {
        throw std::runtime_error("The level reference must allow wrist compensation within the test pitch range");
    }
    Pose lateralDelta{};
    std::vector<std::pair<int, double>> amplitudeSteps;
    if (amplitudeProbe) {
        std::ifstream input(lateralPlanFile);
        std::string magic, calibrationHash;
        if (!(input >> magic >> calibrationHash) || magic != "R1AMPLITUDE1" || calibrationHash != digest) {
            throw std::runtime_error("Amplitude probe needs the approved motor calibration");
        }
        // These caps keep the first field increments within the powered opening
        // window. They are separate from the measured optical travel report.
        constexpr Pose deltaLimit{.210, .045, .035, .045, .035, .035, .110};
        int joint;
        double degrees;
        while (input >> joint) {
            if (!(input >> degrees)) throw std::runtime_error("Incomplete amplitude probe step");
            const double delta = degrees * rad;
            if (joint < 1 || joint > kArmJointCount || !std::isfinite(delta) ||
                std::abs(delta) < .004 || std::abs(delta) > deltaLimit[joint - 1] ||
                (joint == 7 && delta < 0) || amplitudeSteps.size() >= 28) {
                throw std::runtime_error("Amplitude probe step is outside the supported range");
            }
            const double endpoint = approved[joint - 1] + delta;
            if ((joint == 1 && (endpoint <= -7.768 || endpoint >= -7.507)) ||
                (joint == 2 && (endpoint <= 1.183 || endpoint >= 1.267)) ||
                (joint == 4 && (endpoint <= -3.537 || endpoint >= -3.433))) {
                throw std::runtime_error("Amplitude probe endpoint is outside the open position window");
            }
            amplitudeSteps.emplace_back(joint - 1, delta);
        }
        if (!input.eof() || amplitudeSteps.empty()) throw std::runtime_error("Invalid amplitude probe steps");
    }
    const bool lateral = shot == PoweredShot::Lateral;
    if (lateral) {
        std::ifstream motion(lateralPlanFile);
        std::string magic, motionDigest;
        if (!(motion >> magic >> motionDigest) || magic != "R1LATERAL1" || motionDigest != digest) {
            throw std::runtime_error("Lateral plan needs the approved motor calibration");
        }
        const Pose survey = readPose(motion, "measured lateral probe origin");
        lateralDelta = readPose(motion, "measured lateral joint displacement");
        constexpr Pose maxProbe{.024, .020, .015, .015, .015, .015, .024};
        Pose destination{};
        for (int joint = 0; joint < kArmJointCount; ++joint) {
            if (std::abs(lateralDelta[joint]) > maxProbe[joint]) {
                throw std::runtime_error("Lateral displacement exceeds sampled range at " + jointName(joint));
            }
            destination[joint] = approved[joint] + lateralDelta[joint];
        }
        if (!near(survey, approved, .008) || near(approved, destination, .001) ||
            destination[1] < 1.20 || destination[1] > 1.267 ||
            destination[3] < -3.537 || destination[3] > -3.49 ||
            destination[6] < -4.064 || destination[6] > -4.023) {
            throw std::runtime_error("Lateral plan does not fit the surveyed working pose");
        }
    }
    const auto calibration = readCalibration(calibrationFile, digest);
    PortLock lock;
    Bus bus(port, calibration);
    Pose raw{};
    Pose start = sampleStandstill(bus, raw);
    if (parkRequested) {
        std::cout << "Parked and verified mode 0 on all joints\n";
        return;
    }
    Pose staticAssist{};
    Pose foldedReference = surveyed;
    if (autoTrim) foldedReference[0] = start[0];
    // The surveyed J4 opening route already lowers J7 from this high folded
    // position. Use that coupled motion for a large wrist correction.
    const bool lowerWristDuringOpen = autoTrim && start[6] >= -3.72 && start[6] <= -3.55 &&
                                      start[6] - surveyed[6] > .34;
    if (lowerWristDuringOpen) {
        foldedReference[6] = start[6];
        std::cout << "FOLDED_TRIM_J7_DURING_OPEN " << start[6] << " -> " << approved[6] << std::endl;
    }
    const bool corrected = autoTrim && !near(start, foldedReference, .004);
    if (corrected) {
        try {
            start = trimFoldedPose(bus, start, foldedReference, staticAssist);
        } catch (...) {
            if (!bus.stop()) std::cerr << "Motor stop could not be verified on all joints\n";
            throw;
        }
    }
    constexpr double elbowClear = -3.54;
    constexpr double wristTargetMin = -4.060;
    constexpr double standbyTolerance = .008;
    constexpr int standbyStableSamples = 251;
    const bool includeTilt = shot == PoweredShot::PanTilt;
    const bool tabletop = shot == PoweredShot::Tabletop;
    const bool lateralProbe = shot == PoweredShot::LateralProbe;
    const bool rehearsal = shot == PoweredShot::StandbyRehearsal || levelDirector;
    constexpr double left = 11 * rad;
    constexpr double tabletopLift = 6 * rad;
    const double center = approved[0];
    const double right = std::min(3 * rad, -7.515 - center);
    if (!near(start, foldedReference, .008) || start[1] < 1.24 || start[1] > 1.292 ||
        start[3] < -4.03 || start[3] > -3.85 || start[6] < -4.065 || start[6] > -3.49 ||
        start[0] < -7.771 || start[0] > -7.504 ||
        std::abs(center - start[0]) > .055 ||
        (!tabletop && (center - left < -7.771 || right < rad)) ||
        (tabletop && (start[1] < 1.285 || start[3] > -3.985 || start[6] > -4.050))) {
        if (corrected) bus.stop();
        throw std::runtime_error("Current folded pose is outside the surveyed exit and sweep range");
    }
    if (approved[1] < 1.18 || approved[1] > 1.27 || approved[3] < elbowClear ||
        approved[3] > -3.43 || approved[6] < -4.065 || approved[6] > -3.49 ||
        std::abs(approved[2] - start[2]) > .025 || std::abs(approved[4] - start[4]) > .025 ||
        std::abs(approved[5] - start[5]) > .025) {
        if (corrected) bus.stop();
        throw std::runtime_error("Approved standby is outside the validated opening range");
    }
    constexpr auto interval = 20ms;
    Plan plan{};
    plan.passiveShoulder = true;
    plan.panProbe = true;
    plan.retryFeedback = true;
    plan.kp = {40, 85, 60, 85, 60, 60, 60};
    plan.kd = {5, 3.5, 1, 2, 1, 1, 2.8};
    plan.feedForward = staticAssist;
    plan.speed.fill(.08);
    plan.torque = {8, 7.5, 8, 7.5, 5, 5, 2.6};
    for (int joint = 0; joint < kArmJointCount; ++joint) {
        plan.low[joint] = start[joint] - .05;
        plan.high[joint] = start[joint] + .05;
    }
    plan.low[0] = -7.771;
    plan.high[0] = -7.504;
    plan.low[1] = 1.18;
    plan.high[1] = 1.292;
    plan.low[3] = -4.055;
    plan.high[3] = -3.43;
    // Allow a brief encoder excursion beyond the command floor. Keep the
    // commanded position above the floor even during recovery.
    plan.low[6] = wristLow - wristFeedbackMargin;
    if (includeTilt) plan.high[6] = std::min(-3.49, start[6] + pitchLift + .015);
    if (levelRetreat || amplitudeProbe || levelDirector) plan.high[6] = std::min(-3.49, approved[6] + levelLift + .015);
    if (lowerWristDuringOpen) plan.high[6] = std::max(plan.high[6], start[6] + .015);
    if (tabletop) plan.high[6] = -3.920;
    Pose measured = start;
    Pose target = start;
    const auto began = Clock::now();
    auto next = began;
    auto lastReport = began - 2s;
    auto reportInterval = Clock::duration(200ms);
    const Pose lead{.035, .048, .035, .05, .035, .035, .025};
    bool parking = false;
    bool sweepingYaw = false;
    bool followingLateral = false;
    bool followingLevel = false;
    bool followingAmplitude = false;
    auto controlsLevel = [&](int joint) {
        return joint == 3 || joint == 6 || (levelDirector && joint == 2);
    };
    Pose trackingStart{}, trackingTarget{}, trim{};
    int motionJoint = -1;
    double filteredYawSpeed = 0;
    double leftActual = 0, rightActual = 0;
    auto tick = [&](double gain = 1, bool enabling = false, int enablingJoint = -1, bool atIdleStandby = false) {
        waitTick(next, interval);
        if (sweepingYaw) {
            const double error = target[0] - measured[0];
            const double desiredSpeed = plan.velocityReference[0];
            filteredYawSpeed += .28 * (bus.lastSpeed(0) - filteredYawSpeed);
            const double direction = std::tanh(desiredSpeed / .004 + error / .022);
            const double speedAhead = direction * (filteredYawSpeed - desiredSpeed);
            const double attenuation = ease(std::clamp((.070 - speedAhead) / .055, 0., 1.));
            const double assistTarget = .85 * direction * attenuation;
            double& assist = plan.feedForward[0];
            assist += std::clamp(assistTarget - assist, -.038, .038);
        }
        Pose command{};
        for (int joint = 0; joint < kArmJointCount; ++joint) {
            double setpoint = target[joint];
            if (followingLateral || (followingLevel && controlsLevel(joint)) ||
                (followingAmplitude && joint == motionJoint)) {
                const double expected = trackingStart[joint] + target[joint] - trackingTarget[joint];
                const double correctionLimit = followingAmplitude ? (joint == 1 ? .018 : .012) :
                                               followingLevel ? .010 : (joint == 2 ? .008 : .005);
                const double correctionStep = followingLevel ? .00012 : .00025;
                trim[joint] = std::clamp(trim[joint] + std::clamp(.035 * (expected - measured[joint]),
                                                                -correctionStep, correctionStep),
                                         -correctionLimit, correctionLimit);
                setpoint = std::clamp(setpoint + trim[joint], plan.low[joint], plan.high[joint]);
            }
            command[joint] = std::clamp(setpoint, measured[joint] - lead[joint], measured[joint] + lead[joint]);
        }
        if (parking) {
            command[1] = std::min(command[1], measured[1] + .010);
            command[3] = std::max(command[3], measured[3] - .018);
        }
        command[6] = std::max(command[6], wristLow);
        bool still = false;
        measured = bus.cycle(command, plan, 1, gain, enabling, &still, nullptr, enablingJoint,
                             atIdleStandby ? &approved : nullptr);
        if (sweepingYaw) {
            leftActual = std::min(leftActual, measured[0] - center);
            rightActual = std::max(rightActual, measured[0] - center);
        }
        const auto now = Clock::now();
        if (now - lastReport >= reportInterval) {
            bus.telemetry(std::chrono::duration<double>(now - began).count(), target);
            lastReport = now;
        }
        return still;
    };
    auto move = [&](const Pose& destination, int ticks, bool smoothShot = false, bool levelCoupled = false) {
        const Pose from = target;
        for (int sample = 1; sample <= ticks; ++sample) {
            const double u = double(sample) / ticks;
            const double v = 1 - u;
            const double fraction = smoothShot ? u*u*u*u*(35+u*(-84+u*(70-20*u))) : ease(u);
            for (int joint = 0; joint < kArmJointCount; ++joint) {
                target[joint] = from[joint] + fraction * (destination[joint] - from[joint]);
                if (followingLateral || joint == motionJoint || (levelCoupled && controlsLevel(joint))) {
                    const double scale = smoothShot ? 140*u*u*u*v*v*v : 30*u*u*v*v;
                    plan.velocityReference[joint] = (destination[joint] - from[joint]) * scale /
                                                    std::chrono::duration<double>(ticks * interval).count();
                }
            }
            if (!plan.passiveShoulder && measured[3] < elbowClear &&
                (target[1] < start[1] - .015 || measured[1] < start[1] - .015)) {
                throw std::runtime_error("J4 lost clearance with the shoulder above park");
            }
            tick();
        }
    };
    auto settleYaw = [&](double tolerance = .012) {
        int stable = 0;
        for (int sample = 0; sample < 400 && stable < 25 && !parkRequested; ++sample) {
            const double error = target[0] - measured[0];
            double& assist = plan.feedForward[0];
            if (assist * error < 0 || std::abs(bus.lastSpeed(0)) > .08) {
                assist = 0;
            } else if (std::abs(error) > std::min(.008, tolerance)) {
                assist = std::clamp(assist + std::clamp(error * .8, -.016, .016), -1.25, 1.25);
            }
            stable = tick() && std::abs(measured[0] - target[0]) <= tolerance ? stable + 1 : 0;
        }
        return stable >= 25;
    };
    auto brakeYaw = [&] {
        sweepingYaw = false;
        motionJoint = -1;
        plan.velocityReference[0] = 0;
        plan.feedForward[0] = 0;
        target[0] = measured[0] = bus.lastPosition(0);
        for (int sample = 0; sample < 25; ++sample) tick();
    };
    Pose standby = start;
    bool scanned = false;
    bool recordTabletop = false;
    std::string standbyFailure;
    try {
        for (int sample = 0; sample < 25; ++sample) {
            tick(corrected ? 1. : std::min(1., (sample + 1) / 15.), !corrected && sample < 5);
        }
        if (!near(measured, start, .012)) throw std::runtime_error("Could not hold the surveyed park");
        std::cout << "OPENING_J4 J2_MODE_0" << std::endl;
        standby[3] = -3.49;
        const int openingSamples = directorRehearsal || amplitudeProbe ? 1100 : 700;
        const double elbowDamping = plan.kd[3];
        if (openingSamples == 1100) plan.kd[3] = 3.5;
        for (int sample = 1; sample <= openingSamples; ++sample) {
            if (corrected && sample <= 150) {
                for (int j : {0, 2, 4, 5, 6}) plan.feedForward[j] = staticAssist[j] * (1 - ease(sample / 150.));
            }
            target[3] = start[3] + ease(double(sample) / openingSamples) * (standby[3] - start[3]);
            target[6] = std::clamp(start[6] - .9 * (measured[3] - start[3]),
                                   std::min(wristTargetMin, start[6]), start[6]);
            tick();
        }
        standby[6] = target[6];
        for (int sample = 0; sample < 50; ++sample) tick();
        if (measured[3] < elbowClear) throw std::runtime_error("J4 did not reach the tested open position");
        plan.kd[3] = elbowDamping;
        plan.low[3] = elbowClear;
        if (!parkRequested) {
            plan.passiveShoulder = false;
            target[1] = measured[1];
            std::cout << "LIFTING_J2 J4_HELD" << std::endl;
            for (int sample = 1; sample <= 150; ++sample) {
                plan.feedForward[1] = -2.35 * ease(sample / 150.);
                tick(1, sample <= 5, 1);
            }
            standby[1] = 1.228;
            standby[6] = std::clamp(start[6] - .9 * (measured[3] - start[3]) -
                                     .30 * (standby[1] - start[1]), std::min(wristTargetMin, start[6]), start[6]);
            move(standby, 250);
            standby[0] = center;
            for (int joint : {2, 4, 5, 6}) standby[joint] = approved[joint];
            const bool centerAfterOpen = autoTrim && std::abs(measured[0] - center) > standbyTolerance;
            try {
                if (centerAfterOpen) {
                    std::cout << "CENTERING_J1_OPEN " << measured[0] << " -> " << center << std::endl;
                    sweepingYaw = true;
                    motionJoint = 0;
                    filteredYawSpeed = bus.lastSpeed(0);
                    plan.kd[0] = 8;
                }
                move(standby, centerAfterOpen ? 260 : 180, centerAfterOpen);
                sweepingYaw = false;
                motionJoint = -1;
                if (centerAfterOpen) plan.feedForward[0] = 0;
                if (!settleYaw() && !parkRequested) {
                    std::cerr << "J1 could not reach the taught center; holding the available open pose\n";
                }
            } catch (const PanSpeedExceeded& error) {
                sweepingYaw = false;
                motionJoint = -1;
                std::cerr << "J1 centering speed limit: " << error.what() << '\n';
                brakeYaw();
            }
            target[0] = measured[0];
            plan.feedForward[0] = 0;
            int stable = 0;
            int longestStable = 0;
            int noisy = 0;
            Pose first = measured;
            const bool tolerateIsolatedSpeedNoise = directorRehearsal || amplitudeProbe;
            const int standbyTimeoutSamples = lateral || tolerateIsolatedSpeedNoise ? 1000 : 450;
            for (int sample = 0; sample < standbyTimeoutSamples && !parkRequested && stable < standbyStableSamples; ++sample) {
                const bool still = tick();
                const bool atHome = near(measured, first, standbyTolerance) && near(measured, approved, standbyTolerance);
                if (atHome && still) {
                    noisy = 0;
                    ++stable;
                    longestStable = std::max(longestStable, stable);
                } else if (!atHome || !tolerateIsolatedSpeedNoise || ++noisy > 2) {
                    stable = 0;
                    if (!atHome) noisy = 0;
                    first = measured;
                }
            }
            if (!parkRequested) {
                const bool ready = stable >= standbyStableSamples;
                std::cout << (ready ? "STANDBY_HELD_5S " : "STANDBY_NOT_REACHED ");
                printPose(measured);
                std::cout << std::endl;
                if (ready) {
                    plan.high[1] = 1.27;
                } else {
                    std::cerr << "Longest continuous standby: " << longestStable << " of "
                              << standbyStableSamples << " samples\n";
                    standbyFailure = "Could not hold the approved standby for five seconds";
                    parkRequested = 1;
                }
            }
            reportInterval = 2s;
            const auto cameraDeadline = Clock::now() + 20s;
            while (!rehearsal && !levelRetreat && !recordingReady && !parkRequested) {
                tick();
                if (!near(measured, approved, standbyTolerance) || Clock::now() > cameraDeadline) {
                    standbyFailure = "Approved standby drifted or the camera did not start";
                    parkRequested = 1;
                }
            }
            if (!parkRequested && tabletop) {
                std::cout << "PLAYBACK_START\nTABLETOP_DESCENT" << std::endl;
                recordTabletop = true;
            }
            if (amplitudeProbe && !parkRequested) {
                std::cout << "AXIS_PROBE_START" << std::endl;
                reportInterval = 200ms;
                std::size_t completed = 0;
                bool atApproved = true;
                plan.kd[0] = 8;
                for (const auto& [joint, delta] : amplitudeSteps) {
                    if (parkRequested) break;
                    const Pose homeCommand = target;
                    const Pose homeMeasured = measured;
                    atApproved = near(homeMeasured, approved, standbyTolerance);
                    if (!atApproved) break;
                    Pose expected = homeMeasured;
                    expected[joint] = approved[joint] + delta;
                    Pose destination = homeCommand;
                    destination[joint] += expected[joint] - homeMeasured[joint];
                    if (expected[joint] <= plan.low[joint] || expected[joint] >= plan.high[joint] ||
                        destination[joint] <= plan.low[joint] || destination[joint] >= plan.high[joint]) {
                        std::cout << "AXIS_PROBE_OUTSIDE_WINDOW " << jointName(joint) << std::endl;
                        break;
                    }
                    auto mark = [&](const char* label) {
                        std::cout << label << " {\"joint\":" << joint + 1 << ",\"target_sdk_rad\":"
                                  << expected[joint] << ",\"wall_s\":" << wallSeconds() << ",\"q_rad\":";
                        printPose(measured);
                        std::cout << '}' << std::endl;
                    };
                    const double speed = joint == 0 ? .036 : .024;
                    const int ticks = std::max(200, int(std::ceil(
                        2.1875 * std::abs(destination[joint] - homeCommand[joint]) / (speed * .020))));
                    const int returnTicks = joint == 0 ? ticks + ticks / 2 : ticks;
                    motionJoint = joint;
                    trackingStart = homeMeasured;
                    trackingTarget = homeCommand;
                    trim = {};
                    followingAmplitude = true;
                    sweepingYaw = joint == 0;
                    filteredYawSpeed = bus.lastSpeed(0);
                    mark("AXIS_PROBE_BEGIN");
                    bool reached = false;
                    try {
                        move(destination, ticks, true);
                        int stable = 0;
                        for (int i = 0; i < 275 && stable < 25 && !parkRequested; ++i) {
                            const bool still = tick();
                            stable = still && near(measured, expected, .008) &&
                                std::abs(measured[joint] - expected[joint]) < .004 ? stable + 1 : 0;
                        }
                        reached = stable == 25;
                        mark(reached ? "AXIS_PROBE_MEASURED" : "AXIS_PROBE_TRACKING_INCOMPLETE");
                        move(homeCommand, returnTicks, true);
                    } catch (const PanSpeedExceeded& error) {
                        std::cerr << "Axis probe speed guard: " << error.what() << '\n';
                        followingAmplitude = false;
                        brakeYaw();
                        move(homeCommand, returnTicks, true);
                        reached = false;
                    }
                    sweepingYaw = false;
                    plan.feedForward[0] = 0;
                    if (joint == 0 && !parkRequested) {
                        if (followingAmplitude) {
                            target[0] = std::clamp(target[0] + trim[0], plan.low[0], plan.high[0]);
                        }
                        followingAmplitude = false;
                        Pose recentered = target;
                        recentered[0] = center;
                        try {
                            move(recentered, 180, true);
                            settleYaw(standbyTolerance * .9);
                        } catch (const PanSpeedExceeded& error) {
                            std::cerr << "Axis probe yaw recentering stopped: " << error.what() << '\n';
                            brakeYaw();
                        }
                    }
                    int stable = 0;
                    for (int i = 0; i < 400 && stable < standbyStableSamples && !parkRequested; ++i) {
                        tick();
                        stable = near(measured, approved, standbyTolerance) ? stable + 1 : 0;
                    }
                    if (followingAmplitude) {
                        target[joint] = std::clamp(target[joint] + trim[joint], plan.low[joint], plan.high[joint]);
                    }
                    followingAmplitude = false;
                    motionJoint = -1;
                    atApproved = stable == standbyStableSamples;
                    const bool returned = atApproved;
                    mark(returned ? "AXIS_PROBE_RETURNED_POWERED" : "AXIS_PROBE_RETURN_INCOMPLETE");
                    if (!returned) break;
                    if (reached) ++completed;
                }
                std::cout << (completed == amplitudeSteps.size() ? "AXIS_PROBE_COMPLETE" : "AXIS_PROBE_INCOMPLETE")
                          << std::endl;
                reportInterval = 2s;
                if (!directorRehearsal || !atApproved) {
                    while (!parkRequested) tick();
                }
            }
            if (!parkRequested && lateralProbe) {
                reportInterval = 200ms;
                std::cout << "PLAYBACK_START" << std::endl;
                const Pose probeCenter = target;
                constexpr Pose amplitudes{.022, .015, .012, .012, .012, .012, .025};
                auto logProbe = [&](int joint, int direction) {
                    std::cout << std::fixed << std::setprecision(7)
                              << "LATERAL_VIEW {\"joint\":" << joint << ",\"direction\":" << direction
                              << ",\"wall_s\":" << wallSeconds() << ",\"q_rad\":";
                    printPose(measured);
                    std::cout << '}' << std::endl;
                };
                for (int sample = 0; sample < 55 && !parkRequested; ++sample) tick();
                logProbe(0, 0);
                bool probeComplete = true;
                try {
                    for (int joint = 0; joint < kArmJointCount && !parkRequested; ++joint) {
                        motionJoint = joint;
                        sweepingYaw = joint == 0;
                        filteredYawSpeed = bus.lastSpeed(0);
                        for (int direction : {-1, 1}) {
                            if (parkRequested || (joint == 6 && direction < 0)) continue;
                            Pose endpoint = probeCenter;
                            endpoint[joint] += direction * amplitudes[joint];
                            move(endpoint, 140);
                            for (int sample = 0; sample < 55 && !parkRequested; ++sample) tick();
                            if (!parkRequested) logProbe(joint + 1, direction);
                            move(probeCenter, 140);
                            for (int sample = 0; sample < 26 && !parkRequested; ++sample) tick();
                        }
                    }
                } catch (const PanSpeedExceeded& error) {
                    std::cerr << "Lateral probe stopped at J1 speed guard: " << error.what() << '\n';
                    brakeYaw();
                    probeComplete = false;
                }
                sweepingYaw = false;
                motionJoint = -1;
                plan.velocityReference = {};
                plan.feedForward[0] = 0;
                std::cout << (probeComplete && !parkRequested ? "LATERAL_PROBE_COMPLETE" : "LATERAL_PROBE_INCOMPLETE")
                          << std::endl;
                parkRequested = 1;
            }
            if (!parkRequested && lateral) {
                reportInterval = 200ms;
                std::cout << "PLAYBACK_START" << std::endl;
                trackingStart = measured;
                trackingTarget = target;
                Pose destination = target;
                for (int joint = 0; joint < kArmJointCount; ++joint) destination[joint] += lateralDelta[joint];
                followingLateral = sweepingYaw = true;
                filteredYawSpeed = bus.lastSpeed(0);
                plan.kd[0] = 8;
                auto mark = [&](const char* name) {
                    std::cout << std::fixed << std::setprecision(7)
                              << "STRAIGHT_VIEW {\"name\":\"" << name << "\",\"wall_s\":"
                              << wallSeconds() << ",\"q_rad\":";
                    printPose(measured);
                    std::cout << '}' << std::endl;
                };
                for (int sample = 0; sample < 150 && !parkRequested; ++sample) tick();
                mark("begin");
                bool finished = false;
                try {
                    if (!parkRequested) {
                        move(destination, 350);
                        for (int sample = 0; sample < 80 && !parkRequested; ++sample) tick();
                        mark("end");
                    }
                    move(trackingTarget, 350);
                    for (int sample = 0; sample < 150 && !parkRequested; ++sample) tick();
                    mark("return");
                    finished = !parkRequested && near(measured, trackingStart, .008);
                } catch (const PanSpeedExceeded& error) {
                    std::cerr << "Lateral shot stopped at J1 speed guard: " << error.what() << '\n';
                    followingLateral = false;
                    plan.velocityReference = {};
                    target = measured;
                    brakeYaw();
                }
                followingLateral = sweepingYaw = false;
                plan.velocityReference = {};
                plan.feedForward[0] = 0;
                std::cout << (finished ? "LATERAL_PASS_RECORDED" : "LATERAL_PASS_INCOMPLETE") << std::endl;
                parkRequested = 1;
            }
            if (levelRetreat && !parkRequested) {
                const Pose homeTarget = target;
                auto mark = [&](const char* label) {
                    std::cout << label << ' ';
                    printPose(measured);
                    std::cout << std::endl;
                };
                auto awaitVisualCheck = [&](const char* prompt) {
                    recordingReady = 0;
                    mark(prompt);
                    while (!recordingReady && !parkRequested && !returnRequested) tick();
                    return !parkRequested && !returnRequested;
                };
                auto verify = [&](const Pose& expected, double tolerance, int required = 25) {
                    int count = 0;
                    for (int i = 0; i < required + 100 && count < required && !parkRequested; ++i) {
                        const bool still = tick();
                        count = still && near(measured, expected, tolerance) ? count + 1 : 0;
                    }
                    return count == required;
                };
                Pose levelTarget = homeTarget;
                levelTarget[6] = *levelWrist;
                auto runLevelTake = [&] {
                    if (!awaitVisualCheck("LEVEL_RETREAT_STANDBY")) return;
                    reportInterval = 200ms;
                    if (verify(approved, standbyTolerance, standbyStableSamples) && !returnRequested) {
                        const int levelingTicks = std::max(250, int(std::ceil(
                            2.1875 * std::abs(*levelWrist - homeTarget[6]) / (.025 * .020))));
                        move(levelTarget, levelingTicks, true, true);
                        Pose expectedLevel = approved;
                        expectedLevel[6] = *levelWrist;
                        if (verify(expectedLevel, standbyTolerance) &&
                            awaitVisualCheck("LEVEL_RETREAT_VERIFY_REFERENCE") && verify(expectedLevel, standbyTolerance)) {
                            const Pose actualLevel = measured;
                            Pose destination = levelTarget;
                            destination[3] += retreatElbow;
                            destination[6] += retreatWrist;
                            trackingStart = actualLevel;
                            trackingTarget = levelTarget;
                            trim = {};
                            followingLevel = true;
                            move(destination, 200, true, true);
                            Pose expected = actualLevel;
                            expected[3] += retreatElbow;
                            expected[6] += retreatWrist;
                            if (verify(expected, .002)) {
                                reportInterval = 2s;
                                awaitVisualCheck("LEVEL_RETREAT_HOLDING");
                            } else if (!parkRequested) {
                                mark("LEVEL_RETREAT_TRACKING_INCOMPLETE");
                            }
                            reportInterval = 200ms;
                            move(levelTarget, 200, true, true);
                            // Carry the small correction into the return segment so it
                            // fades with the same curve instead of dropping in one tick.
                            followingLevel = false;
                            for (int joint : {3, 6}) {
                                target[joint] = std::clamp(target[joint] + trim[joint], plan.low[joint], plan.high[joint]);
                            }
                        } else if (!parkRequested && !returnRequested) {
                            mark("LEVEL_RETREAT_REFERENCE_INCOMPLETE");
                        }
                        move(homeTarget, levelingTicks, true, true);
                        if (!parkRequested) {
                            mark(verify(approved, standbyTolerance) ? "LEVEL_RETREAT_RETURNED_POWERED"
                                                                   : "LEVEL_RETREAT_RETURN_INCOMPLETE");
                        }
                    } else if (!parkRequested && !returnRequested) {
                        mark("LEVEL_RETREAT_STANDBY_INCOMPLETE");
                    }
                };
                while (!parkRequested) {
                    runLevelTake();
                    if (returnRequested && !parkRequested) mark("LEVEL_RETREAT_CANCELED_POWERED");
                    reportInterval = 2s;
                    do {
                        returnRequested = 0;
                    } while (!parkRequested && !awaitVisualCheck("LEVEL_RETREAT_READY_FOR_TAKE"));
                }
            }
            if ((rehearsal || (amplitudeProbe && directorRehearsal)) && !parkRequested) {
                // Keep the exact command that proved stable under gravity in the opening sequence.
                const Pose holdTarget = target;
                const double shotLeft = levelDirector ? levelMotion.left : -left;
                const double shotRight = levelDirector ? levelMotion.right : right;
                const std::array<std::vector<double>, directorRehearsalRoutes.size()> routes{{
                    {shotLeft, shotRight}, {shotLeft, 0., shotRight}, {shotLeft}, {shotRight},
                }};
                const int readySamples = directorRehearsal ? standbyStableSamples : 25;
                auto updateQuietStandby = [&](int& stable, int& noisy, bool watchDisturbances = false) {
                    const bool still = tick(1, false, -1, watchDisturbances);
                    if (!near(measured, approved, standbyTolerance)) {
                        stable = noisy = 0;
                    } else if (still) {
                        noisy = 0;
                        stable = std::min(readySamples, stable + 1);
                    } else if (!directorRehearsal || ++noisy > 2) {
                        stable = 0;
                    }
                    return still;
                };
                auto settleDisturbedStandby = [&] {
                    int quiet = 0;
                    for (int i = 0; i < 30 && quiet < 10 && !parkRequested; ++i) {
                        quiet = tick() ? quiet + 1 : 0;
                    }
                    if (parkRequested) return false;
                    if (quiet != 10) throw std::runtime_error("Standby feedback did not settle after disturbance");
                    Pose remaining = measured;
                    remaining[2] = approved[2];
                    if (!near(remaining, approved, standbyTolerance) ||
                        std::abs(measured[2] - approved[2]) >= .016) return false;
                    if (!near(measured, approved, standbyTolerance)) {
                        Pose correctedHome = target;
                        correctedHome[2] = std::clamp(target[2] + approved[2] - measured[2], plan.low[2], plan.high[2]);
                        move(correctedHome, 150, true);
                    }
                    int stable = 0, noisy = 0;
                    for (int i = 0; i < 450 && stable < readySamples && !parkRequested; ++i) {
                        updateQuietStandby(stable, noisy);
                    }
                    return stable == readySamples;
                };
                plan.kd[0] = 8;
                auto panTo = [&](const Pose& destination) {
                    constexpr double sampleSeconds = .020;
                    constexpr double speed = .055;
                    const double distance = std::abs(destination[0] - target[0]);
                    const int ticks = std::max(100, int(std::ceil(2.1875 * distance / (speed * sampleSeconds))));
                    move(destination, ticks, true);
                };
                auto runLevelRoute = [&](std::size_t take, std::size_t requestedRoute) {
                    const bool preview = framePeople && requestedRoute == routes.size();
                    const Pose supportedHome = target;
                    const Pose measuredHome = measured;
                    auto goal = [&](const Pose& delta) {
                        Pose destination = supportedHome;
                        for (int joint : levelDirectorJoints) {
                            destination[joint] += approved[joint] + delta[joint] - measuredHome[joint];
                        }
                        destination[0] = center;
                        return destination;
                    };
                    const Pose level = goal(levelMotion.levelDelta);
                    Pose retreat = goal(levelMotion.retreatDelta);
                    for (int joint : levelDirectorJoints) {
                        if (std::min(level[joint], retreat[joint]) < plan.low[joint] ||
                            std::max(level[joint], retreat[joint]) > plan.high[joint]) {
                            std::cout << "LEVEL_DIRECTOR_OUTSIDE_WINDOW " << jointName(joint) << std::endl;
                            return false;
                        }
                    }
                    auto travel = [&](const Pose& destination, int minTicks, double panSpeed) {
                        double duration = std::abs(destination[0] - target[0]) / panSpeed;
                        for (int joint : levelDirectorJoints) {
                            duration = std::max(duration, std::abs(destination[joint] - target[joint]) / .024);
                        }
                        move(destination, std::max(minTicks, int(std::ceil(2.1875 * duration / .020))), true, true);
                    };
                    auto mark = [&](const char* stage, bool reached) {
                        std::cout << "LEVEL_DIRECTOR_PHASE {\"take\":" << take << ",\"stage\":\"" << stage
                                  << "\",\"wall_s\":" << wallSeconds() << ",\"reached\":" << (reached ? "true" : "false")
                                  << ",\"q_rad\":";
                        printPose(measured);
                        std::cout << '}' << std::endl;
                    };
                    auto checkPose = [&](const Pose& command, const char* stage, int hold) {
                        int stable = 0;
                        for (int i = 0; i < hold + 150 && stable < hold && !parkRequested; ++i) {
                            const bool still = tick();
                            const bool leveled = std::all_of(levelDirectorJoints.begin(), levelDirectorJoints.end(),
                                [&](int joint) {
                                    const double expected = measuredHome[joint] + command[joint] - supportedHome[joint];
                                    return std::abs(measured[joint] - expected) < .004;
                                });
                            const bool framed = std::abs(measured[0] - command[0]) < .009;
                            stable = still && leveled && framed ? stable + 1 : 0;
                        }
                        const bool reached = stable == hold;
                        mark(stage, reached);
                        return reached;
                    };
                    trackingStart = measuredHome;
                    trackingTarget = supportedHome;
                    trim = {};
                    followingLevel = true;
                    bool complete = false;
                    try {
                        travel(level, 250, .036);
                        if (checkPose(level, "level", 50)) {
                            travel(retreat, 200, .036);
                            complete = checkPose(retreat, "retreat", 50);
                            std::size_t route = requestedRoute;
                            if (preview && complete) {
                                std::cout << "DIRECTOR_FRAMING_READY " << take << std::endl;
                                const auto deadline = Clock::now() + 32s;
                                int correctionCount = 0;
                                while (route == routes.size() && !parkRequested && Clock::now() < deadline) {
                                    tick();
                                    const auto nextShot = selectedShot(take, routes.size() + 4);
                                    if (!nextShot) continue;
                                    if (*nextShot < routes.size() || *nextShot == routes.size() + 3) {
                                        route = *nextShot;
                                        std::cout << "DIRECTOR_ACCEPTED " << take << ' ' << route << std::endl;
                                        std::cout << "LEVEL_DIRECTOR_SWEEP_START " << take << std::endl;
                                    } else if (*nextShot > routes.size() && correctionCount < 2) {
                                        const double direction = *nextShot == routes.size() + 1 ? 1. : -1.;
                                        const double base = goal(levelMotion.retreatDelta)[6];
                                        const double cap = std::min(base + 1.9 * rad, goal(levelMotion.levelDelta)[6] + .6 * rad);
                                        const double adjusted = std::clamp(retreat[6] + direction * rad, base - 1.9 * rad, cap);
                                        if (std::abs(adjusted - retreat[6]) > .001 && adjusted < plan.high[6]) {
                                            retreat[6] = adjusted;
                                            travel(retreat, 125, .036);
                                            complete = checkPose(retreat, "framing_adjust", 25);
                                            if (!complete) break;
                                        }
                                        ++correctionCount;
                                        std::cout << "DIRECTOR_FRAMING_READY " << take << std::endl;
                                    }
                                }
                                if (route == routes.size()) std::cout << "DIRECTOR_FRAMING_INCOMPLETE " << take << std::endl;
                                complete = complete && route != routes.size();
                            }
                            if (complete) {
                                if (route == routes.size() + 3) {
                                    complete = checkPose(retreat, "portrait", 250);
                                } else {
                                    for (double offset : routes[route]) {
                                        if (!complete || parkRequested || (framePeople && returnRequested)) break;
                                        Pose framed = retreat;
                                        framed[0] += offset;
                                        travel(framed, 200, .036);
                                        const char* side = offset < 0 ? "left" : offset > 0 ? "right" : "center";
                                        const int hold = route >= 2 ? 100 : offset == 0 ? 75 : 50;
                                        complete = checkPose(framed, side, hold);
                                    }
                                }
                                complete = complete && !(framePeople && returnRequested);
                                if (preview) std::cout << "DIRECTOR_FRAMING_SWEEP_END " << take << std::endl;
                            }
                            travel(level, 250, .030);
                        }
                        travel(supportedHome, 250, .030);
                    } catch (const PanSpeedExceeded& error) {
                        std::cerr << "Level rehearsal speed guard: " << error.what() << '\n';
                        complete = false;
                        brakeYaw();
                        Pose recovered = supportedHome;
                        recovered[0] = target[0];
                        travel(recovered, 450, .030);
                        travel(supportedHome, 450, .025);
                    }
                    for (int joint : levelDirectorJoints) {
                        target[joint] = std::clamp(target[joint] + trim[joint], plan.low[joint], plan.high[joint]);
                    }
                    plan.velocityReference = {};
                    followingLevel = false;
                    return complete;
                };
                for (std::size_t take = 1; !parkRequested; ++take) {
                    recordingReady = 0;
                    if (framePeople) returnRequested = 0;
                    std::cout << "REHEARSAL_STANDBY " << take << std::endl;
                    if (directorRehearsal) std::cout << "DIRECTOR_STANDBY " << take << std::endl;
                    int stableAtHome = readySamples;
                    int noisySamples = 0;
                    std::optional<std::size_t> requested, pendingRoute;
                    bool recoveredStandby = false;
                    while (!parkRequested && !requested) {
                        bool still;
                        try {
                            still = updateQuietStandby(stableAtHome, noisySamples, framePeople);
                        } catch (const StandbyDisturbance& error) {
                            std::cerr << "DIRECTOR_STANDBY_DISTURBANCE " << take << ' ' << error.what() << std::endl;
                            pendingRoute.reset();
                            if (!settleDisturbedStandby()) {
                                if (!parkRequested) standbyFailure = "Could not recover the fixed standby after disturbance";
                                parkRequested = 1;
                                break;
                            }
                            std::cout << "DIRECTOR_STANDBY_RECOVERED " << take << std::endl;
                            recoveredStandby = true;
                            break;
                        }
                        if (directorRehearsal) {
                            // An isolated speed sample can be noisy while all seven
                            // positions stay at home. Launch on a fresh quiet sample.
                            if (const auto route = selectedShot(take, routes.size() + (framePeople ? 1 : 0))) pendingRoute = route;
                            if (pendingRoute && stableAtHome < readySamples) {
                                std::cout << "DIRECTOR_REJECTED " << take << " at_home="
                                          << near(measured, approved, standbyTolerance)
                                          << " stable_samples=" << stableAtHome << " noisy_samples=" << noisySamples
                                          << std::endl;
                                pendingRoute.reset();
                            } else if (pendingRoute && still) {
                                requested = pendingRoute;
                            }
                        } else {
                            if (recordingReady && stableAtHome == readySamples) requested = 0;
                        }
                    }
                    if (parkRequested) break;
                    if (recoveredStandby) continue;
                    const bool preview = framePeople && *requested == routes.size();
                    if (directorRehearsal && !preview) std::cout << "DIRECTOR_ACCEPTED " << take << ' ' << *requested << std::endl;
                    const auto& offsets = routes[preview ? 0 : *requested];
                    if (preview) std::cout << "DIRECTOR_FRAMING_BEGIN " << take << std::endl;
                    std::cout << "REHEARSAL_START " << take << std::endl;
                    leftActual = rightActual = 0;
                    filteredYawSpeed = bus.lastSpeed(0);
                    sweepingYaw = true;
                    motionJoint = 0;
                    reportInterval = 200ms;
                    bool completedLevelRoute = false;
                    try {
                        if (levelDirector) {
                            completedLevelRoute = runLevelRoute(take, *requested);
                        } else {
                            for (double offset : offsets) {
                                if (parkRequested) break;
                                Pose endpoint = holdTarget;
                                endpoint[0] = center + offset;
                                panTo(endpoint);
                                for (int sample = 0; sample < 40 && !parkRequested; ++sample) tick();
                            }
                            panTo(holdTarget);
                        }
                    } catch (const PanSpeedExceeded& error) {
                        std::cerr << "Rehearsal speed guard: " << error.what() << '\n';
                        followingLevel = false;
                        plan.velocityReference = {};
                        brakeYaw();
                        panTo(holdTarget);
                        parkRequested = 1;
                    }
                    sweepingYaw = false;
                    motionJoint = -1;
                    plan.feedForward[0] = 0;
                    const bool settledYaw = settleYaw();
                    int atHome = 0, noisyAtHome = 0;
                    const int returnSamples = readySamples + (directorRehearsal ? 500 : 75);
                    for (int sample = 0; sample < returnSamples && atHome < readySamples; ++sample) {
                        updateQuietStandby(atHome, noisyAtHome);
                    }
                    if (atHome < readySamples || (!parkRequested && !settledYaw)) {
                        std::cout << "REHEARSAL_RETURN_CHECK " << take << " stable_samples=" << atHome
                                  << " yaw_settled=" << settledYaw << std::endl;
                        standbyFailure = "Rehearsal did not return to the approved standby";
                        parkRequested = 1;
                    } else if (!parkRequested) {
                        const bool fullSweep = levelDirector ? completedLevelRoute : std::all_of(offsets.begin(), offsets.end(), [&](double offset) {
                            return offset < 0 ? leftActual <= offset / 2 : rightActual >= offset / 2;
                        });
                        std::cout << (fullSweep ? "REHEARSAL_RETURNED_POWERED " : "REHEARSAL_INCOMPLETE_POWERED ")
                                  << take << ' '
                                  << leftActual / rad << ' ' << rightActual / rad << std::endl;
                    }
                    reportInterval = 2s;
                }
            }
            if (!parkRequested && !tabletop && !lateralProbe && !lateral && !rehearsal && !levelRetreat && !amplitudeProbe) {
                reportInterval = 200ms;
                std::cout << "PLAYBACK_START" << std::endl;
                bool panSpeedFault = false;
                sweepingYaw = true;
                motionJoint = 0;
                filteredYawSpeed = bus.lastSpeed(0);
                plan.kd[0] = 8;
                try {
                    for (double offset : {-left, 0., right, 0.}) {
                        if (parkRequested) break;
                        Pose destination = standby;
                        destination[0] += offset;
                        move(destination, std::max(std::abs(target[0] - center), std::abs(offset)) > right + .001
                                              ? 700 : 200);
                        for (int sample = 0; sample < 40; ++sample) tick();
                    }
                    move(standby, std::abs(target[0] - center) > .05 ? 700 : 100);
                    sweepingYaw = false;
                    motionJoint = -1;
                    plan.feedForward[0] = 0;
                    scanned = settleYaw();
                } catch (const PanSpeedExceeded& error) {
                    panSpeedFault = true;
                    std::cerr << "J1 sweep aborted at the speed guard: " << error.what() << '\n';
                    brakeYaw();
                    try {
                        move(standby, 600);
                        settleYaw();
                    } catch (const PanSpeedExceeded&) {
                        brakeYaw();
                    }
                }
                scanned = scanned && !panSpeedFault && !parkRequested &&
                          leftActual <= -left / 2 && rightActual >= right / 2;
                target[0] = measured[0];
                plan.feedForward[0] = 0;
                std::cout << "Measured J1 offsets: " << leftActual / rad << " / " << rightActual / rad << " deg\n"
                          << "Return yaw error: " << (measured[0] - center) / rad << " deg" << std::endl;
                if (includeTilt && !parkRequested && std::abs(measured[0] - center) <= .012) {
                    Pose tiltStart = target;
                    tiltStart[6] = std::max(tiltStart[6], wristTargetMin);
                    const double initial = measured[6];
                    Pose raised = tiltStart;
                    raised[6] += pitchLift;
                    plan.kd[6] = 2.8;
                    motionJoint = 6;
                    std::cout << "PITCH_START" << std::endl;
                    move(raised, 250);
                    double highest = measured[6];
                    for (int sample = 0; sample < 35; ++sample) {
                        tick();
                        highest = std::max(highest, measured[6]);
                    }
                    move(tiltStart, 250);
                    motionJoint = -1;
                    int still = 0;
                    for (int sample = 0; sample < 100 && still < 25; ++sample) {
                        still = tick() && std::abs(measured[6] - initial) <= .015 ? still + 1 : 0;
                    }
                    const bool pitchComplete = highest - initial >= pitchLift * .7 && still >= 25;
                    std::cout << "PITCH_RESULT " << (pitchComplete ? "complete" : "incomplete")
                              << " travel_deg=" << (highest - initial) / rad
                              << " return_error_deg=" << (measured[6] - initial) / rad << std::endl;
                    scanned = scanned && pitchComplete;
                } else if (includeTilt) {
                    scanned = false;
                }
                std::cout << (scanned ? "PAN_COMPLETE_HOLDING_OPEN" : "PAN_INCOMPLETE_HOLDING_OPEN") << std::endl;
                reportInterval = 2s;
                while (!parkRequested) tick();
            }
            reportInterval = 200ms;
            plan.high[1] = 1.292;
            parking = true;
            Pose shoulderPark = standby;
            shoulderPark[0] = measured[0];
            shoulderPark[1] = std::min(start[1], 1.285);
            std::cout << "PARKING_J2 J4_HELD" << std::endl;
            move(shoulderPark, 350);
            for (int sample = 1; sample <= 250; ++sample) {
                plan.feedForward[1] = -2.35 * (1 - ease(sample / 250.));
                tick();
            }
            for (int sample = 0; sample < 30; ++sample) tick();
            if (std::abs(measured[1] - start[1]) > .012) {
                throw std::runtime_error("J2 did not settle at park with J4 still open");
            }
        }
        plan.low[3] = -4.055;
        parking = true;
        plan.kd[3] = 3.5;
        Pose folded = start;
        folded[0] = measured[0];
        if (lowerWristDuringOpen) folded[6] = surveyed[6];
        std::cout << "PARKING_J4_J7" << std::endl;
        move(folded, 1000);
        for (int sample = 0; sample < 50; ++sample) tick();
        if (!near(measured, folded, .02)) throw std::runtime_error("Could not verify the folded park pose");
        if (recordTabletop && !parkRequested) {
            plan.kd[6] = 2.8;
            motionJoint = 6;
            Pose framing = folded;
            framing[6] = std::max(folded[6], -4.061);
            Pose raised = framing;
            raised[6] += tabletopLift;
            std::cout << "WRIST_RAISE" << std::endl;
            move(raised, 300);
            for (int sample = 0; sample < 65 && !parkRequested; ++sample) tick();
            const double highWrist = measured[6];
            std::cout << "WRIST_ACQUIRE" << std::endl;
            move(framing, 300);
            motionJoint = -1;
            for (int sample = 0; sample < 125 && !parkRequested; ++sample) tick();
            const bool captured = highWrist - measured[6] >= tabletopLift * .7 &&
                                  std::abs(measured[6] - folded[6]) <= .012 && !parkRequested;
            std::cout << "WRIST_RESULT " << (captured ? "complete" : "incomplete")
                      << " down_deg=" << (highWrist - measured[6]) / rad
                      << " return_error_deg=" << (measured[6] - folded[6]) / rad << std::endl;
            std::cout << (captured ? "WRIST_COMPLETE" : "WRIST_INCOMPLETE") << std::endl;
        }
    } catch (...) {
        if (!bus.stop()) std::cerr << "Motor stop could not be verified on all joints\n";
        throw;
    }
    if (!bus.stop()) throw std::runtime_error("Motor stop could not be verified on all joints");
    std::cout << "Parked and verified mode 0 on all joints\n";
    if (!standbyFailure.empty()) throw std::runtime_error(standbyFailure);
}

}  // namespace

int main(int argc, char** argv) {
    if (argc == 2 && std::string_view(argv[1]) == "--director-rehearsal-routes") {
        for (const auto name : directorRehearsalRoutes) std::cout << name << '\n';
        return 0;
    }
    if (argc == 2 && std::string_view(argv[1]) == "--help") {
        std::cout << "Usage: r1_play --check PLAN.r1traj\n"
                  << "       r1_play --snapshot SERIAL_PATH CALIBRATION.txt\n"
                  << "       r1_play --hold-snapshot SERIAL_PATH CALIBRATION.txt REFERENCE.txt\n"
                  << "       r1_play --return-snapshot SERIAL_PATH CALIBRATION.txt START.txt TARGET.txt\n"
                  << "       r1_play --return-snapshot SERIAL_PATH CALIBRATION.txt START.txt TARGET.txt CLEARANCE.txt\n"
                  << "       r1_play --open-j4 SERIAL_PATH CALIBRATION.txt REFERENCE.txt\n"
                  << "       r1_play --pan-smoke SERIAL_PATH CALIBRATION.txt REFERENCE.txt [SWING_DEG]\n"
                  << "       r1_play --pan-wide SERIAL_PATH CALIBRATION.txt REFERENCE.txt\n"
                  << "       r1_play --pan-powered SERIAL_PATH CALIBRATION.txt PARK.txt STANDBY.txt\n"
                  << "       r1_play --pan-tilt-powered SERIAL_PATH CALIBRATION.txt PARK.txt STANDBY.txt\n"
                  << "       r1_play --standby-rehearsal SERIAL_PATH CALIBRATION.txt PARK.txt STANDBY.txt [--trim-park]\n"
                  << "       r1_play --level-retreat SERIAL_PATH CALIBRATION.txt PARK.txt STANDBY.txt LEVEL_J7_RAD\n"
                  << "       r1_play --amplitude-probe SERIAL_PATH CALIBRATION.txt PARK.txt STANDBY.txt PROBE.txt\n"
                  << "       r1_play --amplitude-probe-director SERIAL_PATH CALIBRATION.txt PARK.txt STANDBY.txt PROBE.txt\n"
                  << "       r1_play --level-director SERIAL_PATH CALIBRATION.txt PARK.txt STANDBY.txt LEVEL.txt [--frame-people]\n"
                  << "       r1_play --check-level-director CALIBRATION.txt STANDBY.txt LEVEL.txt\n"
                  << "       In standby-rehearsal, SIGUSR1 requests a sweep and SIGUSR2 requests a slow park.\n"
                  << "       r1_play --wrist-tabletop SERIAL_PATH CALIBRATION.txt PARK.txt STANDBY.txt\n"
                  << "       r1_play --lateral-probe SERIAL_PATH CALIBRATION.txt PARK.txt STANDBY.txt\n"
                  << "       r1_play --straight-lateral SERIAL_PATH CALIBRATION.txt PARK.txt STANDBY.txt LATERAL.txt\n"
                  << "       r1_play --execute PLAN.r1traj SERIAL_PATH CALIBRATION.txt STANDBY.txt\n"
                  << "       r1_play --standby-loop SERIAL_PATH CALIBRATION.txt STANDBY.txt PLAN.r1traj [PLAN.r1traj ...]\n"
                  << "       r1_play --check-standby CALIBRATION.txt STANDBY.txt PLAN.r1traj [PLAN.r1traj ...]\n"
                  << "       In standby-loop, send SIGUSR1 after POWERED_STANDBY to start the next shot.\n";
        return 0;
    }
    const bool check = argc == 3 && std::string_view(argv[1]) == "--check";
    const bool execute = argc == 6 && std::string_view(argv[1]) == "--execute";
    const bool standbyLoop = argc >= 6 && std::string_view(argv[1]) == "--standby-loop";
    const bool directorControl = argc >= 6 && std::string_view(argv[1]) == "--standby-director";
    const bool standbyCheck = argc >= 5 && std::string_view(argv[1]) == "--check-standby";
    const bool takeSnapshot = argc == 4 && std::string_view(argv[1]) == "--snapshot";
    const bool hold = argc == 5 && std::string_view(argv[1]) == "--hold-snapshot";
    const bool snapshotReturn = (argc == 6 || argc == 7) && std::string_view(argv[1]) == "--return-snapshot";
    const bool openJ4 = argc == 5 && std::string_view(argv[1]) == "--open-j4";
    const bool pan = (argc == 5 || argc == 6) && std::string_view(argv[1]) == "--pan-smoke";
    const bool wide = argc == 5 && std::string_view(argv[1]) == "--pan-wide";
    const bool pitch = argc == 6 && std::string_view(argv[1]) == "--pan-tilt-powered";
    const bool levelRetreat = argc == 7 && std::string_view(argv[1]) == "--level-retreat";
    const bool framePeople = argc == 8 && std::string_view(argv[1]) == "--level-director" &&
                             std::string_view(argv[7]) == "--frame-people";
    const bool levelDirector = framePeople || (argc == 7 && std::string_view(argv[1]) == "--level-director");
    const bool levelCheck = argc == 5 && std::string_view(argv[1]) == "--check-level-director";
    const bool directorAmplitude = argc == 7 && std::string_view(argv[1]) == "--amplitude-probe-director";
    const bool amplitudeProbe = directorAmplitude || (argc == 7 && std::string_view(argv[1]) == "--amplitude-probe");
    const bool directorRehearsal = directorAmplitude || levelDirector ||
        (argc == 6 && std::string_view(argv[1]) == "--standby-rehearsal-director");
    const bool trimPark = argc == 7 && std::string_view(argv[1]) == "--standby-rehearsal" &&
                          std::string_view(argv[6]) == "--trim-park";
    const bool rehearsal = directorRehearsal || trimPark || (argc == 6 && std::string_view(argv[1]) == "--standby-rehearsal");
    const bool tabletop = argc == 6 && std::string_view(argv[1]) == "--wrist-tabletop";
    const bool probe = argc == 6 && std::string_view(argv[1]) == "--lateral-probe";
    const bool lateral = argc == 7 && std::string_view(argv[1]) == "--straight-lateral";
    const bool powered = lateral || trimPark || levelRetreat || amplitudeProbe || levelDirector ||
                         (argc == 6 && (std::string_view(argv[1]) == "--pan-powered" || pitch || tabletop || probe || rehearsal));
    if (!check && !execute && !standbyLoop && !directorControl && !standbyCheck && !levelCheck &&
        !takeSnapshot && !hold && !snapshotReturn && !pan && !wide && !openJ4 && !powered) {
        std::cerr << "Use --help for supported modes\n";
        return 2;
    }
    try {
        std::signal(SIGINT, interrupt);
        std::signal(SIGTERM, interrupt);
        if (levelCheck) {
            std::ifstream standby(argv[3]);
            std::string digest;
            if (!(standby >> digest)) throw std::runtime_error("Invalid approved standby reference");
            const auto calibration = readCalibration(argv[2], digest);
            const Pose approved = readApprovedStandby(argv[3], calibration);
            readLevelDirectorPlan(argv[4], digest, approved);
            std::cout << "Level rehearsal validated against the fixed standby and surveyed ranges\n";
            return 0;
        }
        if (standbyLoop || directorControl || standbyCheck) {
            const int calibrationArg = standbyCheck ? 2 : 3;
            std::vector<Plan> plans;
            for (int arg = calibrationArg + 2; arg < argc; ++arg) {
                auto plan = loadPlan(argv[arg]);
                if (!plan.field) throw std::runtime_error("A simulation plan cannot enable motors");
                plan.holdStandby = true;
                plans.push_back(std::move(plan));
            }
            const auto& first = plans.front();
            const auto calibration = readCalibration(argv[calibrationArg], first.calibrationHash);
            const Pose approved = readApprovedStandby(argv[calibrationArg + 1], calibration);
            validateStandbySequence(plans, approved);
            if (standbyCheck) {
                std::cout << "Approved standby verified for " << plans.size() << " field trajectories\n";
                return 0;
            }
            if (directorControl) {
                requireDirectorSocket();
                std::signal(SIGUSR2, requestPark);
            } else {
                std::signal(SIGUSR1, requestShot);
            }
            PortLock lock;
            Bus bus(argv[2], calibration);
            runPlayback(bus, first, period, 0, 0, &approved, &plans, directorControl);
            return 0;
        }
        if (powered) {
            if (directorRehearsal) {
                requireDirectorSocket();
                // Service termination may reach both the wrapper and player.
                // Both must follow the same powered return and supported park path.
                std::signal(SIGTERM, requestPark);
            }
            std::optional<double> levelWrist;
            if (levelRetreat) {
                std::size_t parsed = 0;
                levelWrist = std::stod(argv[6], &parsed);
                if (parsed != std::string_view(argv[6]).size()) throw std::runtime_error("Invalid level J7 reference");
                std::signal(SIGHUP, requestReturn);
            }
            std::signal(SIGUSR1, beginRecording);
            std::signal(SIGUSR2, requestPark);
            if (framePeople) std::signal(SIGHUP, requestReturn);
            const auto shot = levelDirector ? PoweredShot::LevelDirector :
                              amplitudeProbe ? PoweredShot::AmplitudeProbe : levelRetreat ? PoweredShot::LevelRetreat :
                              lateral ? PoweredShot::Lateral : probe ? PoweredShot::LateralProbe :
                              tabletop ? PoweredShot::Tabletop :
                              rehearsal ? PoweredShot::StandbyRehearsal : pitch ? PoweredShot::PanTilt : PoweredShot::Pan;
            poweredShot(argv[2], argv[3], argv[4], argv[5], shot, (lateral || amplitudeProbe || levelDirector) ? argv[6] : nullptr,
                        directorRehearsal, directorRehearsal || trimPark || levelRetreat || amplitudeProbe, levelWrist, framePeople);
            return 0;
        }
        if (takeSnapshot) {
            snapshot(argv[2], argv[3]);
            return 0;
        }
        if (hold || snapshotReturn) {
            holdSnapshot(argv[2], argv[3], snapshotReturn ? argv[5] : argv[4],
                         snapshotReturn ? argv[4] : nullptr, snapshotReturn && argc == 7 ? argv[6] : nullptr);
            return 0;
        }
        if (openJ4) {
            openElbow(argv[2], argv[3], argv[4]);
            return 0;
        }
        if (pan || wide) {
            double swingDegrees = 6;
            if (argc == 6) {
                std::size_t used = 0;
                swingDegrees = std::stod(argv[5], &used);
                if (used != std::string_view(argv[5]).size()) {
                    throw std::runtime_error("Invalid pan angle");
                }
            }
            panSmoke(argv[2], argv[3], argv[4], swingDegrees, wide);
            return 0;
        }
        Plan plan = loadPlan(argv[2]);
        if (check) {
            std::cout << "Plan validated: " << plan.q.size() << " samples @ " << rate << " Hz\n";
            return 0;
        }
        if (!plan.field) throw std::runtime_error("A simulation plan cannot enable motors");
        const auto calibration = readCalibration(argv[4], plan.calibrationHash);
        const Pose approved = readApprovedStandby(argv[5], calibration);
        if (!near(plan.q.front(), approved, .008)) {
            throw std::runtime_error("Field trajectory does not start at the approved standby");
        }
        plan.holdStandby = true;
        PortLock lock;
        Bus bus(argv[3], calibration);
        runPlayback(bus, plan, period, 0, 0, &approved);
    } catch (const std::exception& error) {
        std::cerr << "Replay failed: " << error.what() << '\n';
        return 1;
    }
}
