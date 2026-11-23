// Included by the isolated gesture build of the board's arm server.
#include <algorithm>

namespace gesture {

constexpr float kJ2Down = 1.29154f;  // 74 degrees, physical lower edge
constexpr float kJ2Neutral = 1.25664f;  // 72 degrees during tracking
constexpr float kJ2Up = 1.04720f;  // 60 degrees for the first gesture trial
constexpr float kJ4OpenTarget = -3.43f;  // encoder branch observed on this arm
constexpr float kJ4Clearance = -3.54f;
constexpr float kPanTravel = 0.12f;
constexpr float kPanAbsoluteTravel = 0.27f;
constexpr float kJ7PitchTravel = 0.50f;
constexpr float kJ7TestedMinimum = -4.065f;
constexpr float kNoFrame = 2.f;
constexpr uint32_t kPacketMagic = 0x47315231;

struct Profile {
    float pan_travel;
    float j2_up;
    float seconds;
    bool follow_walking;
};
constexpr Profile kStandard{kPanTravel, kJ2Up, 20.f, false};
constexpr Profile kWide{0.22f, 1.00356f, 60.f, true};  // 12.6-degree hand yaw, 57.5-degree J2 target

enum class Phase { Centering, Opening, Awaiting, Calibrating, Following, RestoreJ2, RestoreJ4, Holding, Done };
enum class Action : uint32_t { Wait = 0, Confirm = 1, Follow = 2, Freeze = 3, Return = 4, Prepare = 5, BodyOnly = 6 };

struct Packet {
    uint32_t magic, sequence;
    Action action;
    float x, y, vertical, horizontal;
};
static_assert(sizeof(Packet) == 28);

const char* phase_name(Phase phase) {
    switch (phase) {
        case Phase::Centering: return "centering_person";
        case Phase::Opening: return "opening_j4";
        case Phase::Awaiting: return "verify_clearance";
        case Phase::Calibrating: return "calibrating";
        case Phase::Following: return "following";
        case Phase::RestoreJ2: return "returning_j2";
        case Phase::RestoreJ4: return "returning_j4";
        case Phase::Holding: return "hold_for_operator";
        case Phase::Done: return "done";
    }
    return "unknown";
}

JointArray gesture_target(const JointArray& base, float x, float y, const Profile& profile = kStandard) {
    JointArray result = base;
    result[0] += std::clamp(x, -1.f, 1.f) * profile.pan_travel;
    const float neutral = profile.follow_walking ? base[1] : kJ2Neutral;
    const float lift = std::clamp(profile.follow_walking ? (y - 0.04f) * 1.85f : y, 0.f, 1.f);
    result[1] = neutral - lift * (neutral - profile.j2_up);
    result[3] = kJ4OpenTarget;
    return result;
}

float j2_damping_gain(float measured_speed, float& filtered_speed, float base_damping) {
    filtered_speed += 0.16f * (measured_speed - filtered_speed);
    const float blend = std::clamp((std::abs(filtered_speed) - 0.07f) / 0.19f, 0.f, 1.f);
    return base_damping + (4.5f - base_damping) * blend;
}

float j2_lift_assist(float previous, float command_error, float goal_gap, float filtered_speed,
                     float raw_speed, float kp, float kd, float dt) {
    const float room = std::max(0.f, 6.f + kp * command_error - kd * raw_speed);
    const float slow = 1.f - std::clamp((-filtered_speed - 0.035f) / 0.14f, 0.f, 1.f);
    const float available = std::min({2.5f, 90.f * std::max(0.f, -command_error - 0.004f),
                                      70.f * std::max(0.f, goal_gap - 0.015f)}) * slow;
    const float requested = -std::min(available, room);
    return std::clamp(previous + std::clamp(requested - previous, -1.10f * dt, 4.f * dt), -room, 0.f);
}

struct Schedule {
    explicit Schedule(JointArray initial, Profile trial_profile = kStandard)
        : base(initial), profile(trial_profile), target(initial), opening_target(initial[3]), pan_center(initial[0]) {}

    const JointArray base;
    const Profile profile;
    JointArray target;
    Phase phase = Phase::Centering;
    double entered = 0;
    bool frozen = false;
    float hand_x = 0.f;
    float hand_y = 0.f;
    float opening_target;
    float pan_center;
    float horizontal_error = kNoFrame;
    double horizontal_at = -1;
    double last_center_step = -1;
    double centered_since = -1;
    float pitch_trim = 0.f;
    float camera_error = kNoFrame;
    double camera_at = -1;
    double prior_update = 0;
    double j2_settled_since = -1;

    void change(Phase next, double now) {
        phase = next;
        entered = now;
        if (next == Phase::RestoreJ2) j2_settled_since = -1;
    }

    void update(double now, bool alive, Action action, float x, float y, const JointArray& measured,
                float vertical = kNoFrame, bool new_packet = false, float j2_speed = 0.f,
                float horizontal = kNoFrame) {
        const float dt = std::clamp(float(now - prior_update), 0.f, 0.05f);
        prior_update = now;
        if (new_packet && vertical != kNoFrame) {
            camera_at = now;
            camera_error = vertical;
        }
        if (new_packet && horizontal != kNoFrame) {
            horizontal_at = now;
            horizontal_error = horizontal;
        }
        const bool framed = now - camera_at < 0.30 && camera_error != kNoFrame;
        const bool person_visible = now - horizontal_at < 0.30;
        const bool centering = phase == Phase::Centering || phase == Phase::Opening ||
                               phase == Phase::Awaiting || phase == Phase::Calibrating;
        if (centering && person_visible && now - last_center_step >= 0.4) {
            // Same direction and step gain as the original precenter.py.
            if (std::abs(horizontal_error) > 0.04f) {
                pan_center = std::clamp(pan_center + 0.35f * horizontal_error,
                                        base[0] - 0.20f, base[0] + 0.20f);
            }
            target[0] = pan_center;
            last_center_step = now;
        }
        const bool clear = measured[3] >= kJ4Clearance;
        const bool j2_at_rest = std::abs(measured[1] - base[1]) <= 0.027f;
        if ((action == Action::Return || !alive) &&
            (phase == Phase::Centering || phase == Phase::Opening || phase == Phase::Awaiting ||
             phase == Phase::Calibrating || phase == Phase::Following)) {
            change(phase == Phase::Following ? Phase::RestoreJ2 : Phase::RestoreJ4, now);
        }
        const auto elapsed = now - entered;
        switch (phase) {
            case Phase::Centering:
                if (person_visible && framed && std::abs(horizontal_error) < 0.095f &&
                    std::abs(measured[0] - pan_center) < 0.026f) {
                    if (centered_since < 0) centered_since = now;
                    if (now - centered_since > 0.40) change(Phase::Opening, now);
                } else {
                    centered_since = -1;
                }
                if (elapsed > 15 || (elapsed > 2 && now - horizontal_at > 0.7)) change(Phase::RestoreJ4, now);
                break;
            case Phase::Opening:
                if (framed && person_visible && std::abs(horizontal_error) < 0.15f && camera_error < 0.105f &&
                    std::abs(measured[6] - target[6]) < 0.045f) {
                    opening_target = std::min(kJ4OpenTarget, opening_target + 0.085f * dt);
                }
                target[3] = opening_target;
                if (clear && framed && person_visible && std::abs(horizontal_error) < 0.12f &&
                    std::abs(camera_error) < 0.13f && elapsed > 4) {
                    change(Phase::Awaiting, now);
                } else if (elapsed > 26 || (elapsed > 2 && now - camera_at > 0.7)) {
                    change(Phase::RestoreJ4, now);
                }
                break;
            case Phase::Awaiting:
                if (!clear || elapsed > 40) change(Phase::RestoreJ4, now);
                else if (action == Action::Prepare) change(Phase::Calibrating, now);
                break;
            case Phase::Calibrating:
                if (!clear || elapsed > 25) change(Phase::RestoreJ4, now);
                else if (action == Action::Confirm && person_visible && std::abs(horizontal_error) < 0.15f) {
                    pan_center = measured[0];
                    change(Phase::Following, now);
                }
                break;
            case Phase::Following:
                if (!clear || elapsed > profile.seconds) {
                    change(Phase::RestoreJ2, now);
                    target[0] = base[0];
                    target[1] = base[1];
                    target[3] = opening_target;
                    break;
                }
                if (profile.follow_walking && person_visible && action != Action::Freeze) {
                    // Map the box bearing into the robot's yaw frame. A hand-driven pan does
                    // not look like the person walking in the other direction.
                    const float body_bearing = measured[0] + std::atan(1.45f * horizontal_error);
                    const float yaw_step = std::clamp(2.1f * (body_bearing - pan_center), -0.17f, 0.17f) * dt;
                    pan_center = std::clamp(pan_center + yaw_step,
                                            base[0] - kPanAbsoluteTravel, base[0] + kPanAbsoluteTravel);
                }
                if (action == Action::Follow) {
                    hand_x = x;
                    hand_y = y;
                    target = gesture_target(base, x, y, profile);
                    target[0] = std::clamp(pan_center + x * profile.pan_travel,
                                           base[0] - kPanAbsoluteTravel, base[0] + kPanAbsoluteTravel);
                    target[3] = opening_target;
                    frozen = false;
                } else if (profile.follow_walking && action == Action::BodyOnly && person_visible) {
                    target[0] = pan_center;
                    if (!frozen) target[1] = measured[1];
                    frozen = true;
                } else if (action == Action::Freeze && !frozen) {
                    target[0] = measured[0];
                    target[1] = measured[1];
                    frozen = true;
                }
                break;
            case Phase::RestoreJ2:
                {
                const float held_pitch = target[6];
                target = base;
                target[3] = opening_target;
                target[6] = held_pitch;
                if (j2_at_rest && std::abs(j2_speed) < 0.07f) {
                    if (j2_settled_since < 0) j2_settled_since = now;
                } else {
                    j2_settled_since = -1;
                }
                if (j2_settled_since >= 0 && now - j2_settled_since > 0.4 && elapsed > 2) {
                    change(Phase::RestoreJ4, now);
                }
                else if (elapsed > (profile.follow_walking ? 16 : 10)) change(Phase::Holding, now);
                break;
                }
            case Phase::RestoreJ4:
                if (!j2_at_rest) {
                    change(Phase::RestoreJ2, now);
                    target[3] = opening_target;
                    break;
                }
                target = base;
                if (elapsed > 2 && std::abs(measured[3] - base[3]) <= 0.035f &&
                    std::abs(measured[6] - base[6]) <= 0.025f) change(Phase::Done, now);
                else if (elapsed > 16) change(Phase::Holding, now);
                break;
            case Phase::Holding:
                // Keep supporting the last return setpoint until the on-site operator can take over.
                break;
            case Phase::Done:
                break;
        }
        if (phase == Phase::Opening || phase == Phase::Awaiting || phase == Phase::Calibrating || phase == Phase::Following) {
            if (framed && std::abs(camera_error) > 0.015f) {
                pitch_trim = std::clamp(pitch_trim - 0.85f * camera_error * dt, -0.22f, 0.12f);
            }
            const float elbow_change = std::max(0.f, measured[3] - base[3]);
            const float lift_change = measured[1] - base[1];
            const float pitch = base[6] - 0.9f * elbow_change - 0.30f * lift_change + pitch_trim;
            target[6] = std::clamp(pitch, std::max(kJ7TestedMinimum, base[6] - kJ7PitchTravel), base[6]);
        }
    }
};

bool trial_start_ok(const JointArray& base) {
    for (const float angle : base) if (!std::isfinite(angle)) return false;
    return base[1] >= 1.24f && base[1] <= kJ2Down + 0.010f &&
           base[3] >= -4.02f && base[3] <= -3.42f && base[6] >= kJ7TestedMinimum &&
           std::abs(std::remainder(base[0], 6.28318531f)) + kPanAbsoluteTravel < 2.70f;
}

int self_test() {
    const JointArray base = {-7.466f, 1.290f, 3.742f, -4.010f, -5.102f, 3.908f, -3.544f};
    auto measured = base;
    Schedule schedule(base);
    schedule.update(0.2, true, Action::Wait, 0, 0, measured, kNoFrame, true, 0.f, -0.26f);
    if (schedule.target[3] != base[3] || schedule.target[0] >= base[0]) return 1;
    measured[0] = schedule.target[0];
    schedule.update(0.5, true, Action::Wait, 0, 0, measured, 0.f, true, 0.f, 0.f);
    schedule.update(1.0, true, Action::Confirm, 0, 0, measured, 0.f, true, 0.f, 0.f);
    if (!trial_start_ok(base) || schedule.phase != Phase::Opening || schedule.target[1] != base[1]) return 1;
    measured[3] = -3.50f;
    schedule.opening_target = measured[3];
    schedule.update(6, true, Action::Wait, 0, 0, measured, 0.f, true, 0.f, 0.f);
    if (schedule.phase != Phase::Awaiting || schedule.target[1] != base[1] || schedule.target[6] >= base[6]) return 1;
    schedule.update(7, true, Action::Follow, -1, 1, measured);
    if (schedule.phase != Phase::Awaiting || schedule.target[1] != base[1]) return 1;
    schedule.update(8, true, Action::Prepare, 0, 0, measured);
    if (schedule.phase != Phase::Calibrating || schedule.target[1] != base[1]) return 1;
    schedule.update(9, true, Action::Confirm, 0, 0, measured, 0.f, true, 0.f, 0.f);
    schedule.update(10, true, Action::Follow, -1, 1, measured);
    if (schedule.phase != Phase::Following || schedule.target[0] >= base[0] ||
        std::abs(schedule.target[1] - kJ2Up) > 1e-5f) return 1;
    const auto right_down = gesture_target(base, 1.f, -1.f);
    if (right_down[0] <= base[0] || right_down[1] != kJ2Neutral) return 1;
    measured[1] = 1.10f;
    schedule.update(11, true, Action::Freeze, 0, 0, measured);
    if (!schedule.frozen || schedule.target[1] != measured[1]) return 1;
    schedule.update(12, false, Action::Wait, 0, 0, measured);
    if (schedule.phase != Phase::RestoreJ2 || schedule.target[3] != schedule.opening_target) return 1;
    measured[1] = base[1];
    schedule.update(14, false, Action::Wait, 0, 0, measured, kNoFrame, false, 0.18f);
    if (schedule.phase != Phase::RestoreJ2) return 1;
    schedule.update(14.1, false, Action::Wait, 0, 0, measured);
    schedule.update(15, false, Action::Wait, 0, 0, measured);
    if (schedule.phase != Phase::RestoreJ4) return 1;
    measured[3] = base[3];
    schedule.update(18, false, Action::Wait, 0, 0, measured);
    if (schedule.phase != Phase::Done) return 1;
    Schedule wide(base, kWide);
    wide.change(Phase::Following, 0);
    measured = base;
    measured[3] = -3.50f;
    wide.update(21, true, Action::Follow, 1.f, 0.60f, measured, 0.f, true, 0.f, 0.1f);
    if (wide.phase != Phase::Following || wide.target[0] - base[0] < 0.20f ||
        std::abs(wide.target[1] - kWide.j2_up) > 1e-5f) return 1;
    const float prior_pan = wide.pan_center;
    wide.update(21.05, true, Action::BodyOnly, 0, 0, measured, 0.f, true, 0.f, 0.20f);
    if (!wide.frozen || wide.target[0] <= prior_pan || wide.target[1] != measured[1]) return 1;
    wide.update(61, true, Action::Return, 0, 0, measured);
    if (wide.phase != Phase::RestoreJ2) return 1;
    if (gesture_target(base, 0.f, 0.03f, kWide)[1] != base[1]) return 1;
    float filtered_speed = 0.f;
    if (j2_damping_gain(-0.414f, filtered_speed, 2.f) > 2.01f) return 1;
    float damping = 0.f;
    for (int i = 0; i < 12; ++i) damping = j2_damping_gain(-0.414f, filtered_speed, 2.f);
    if (damping <= 2.f || damping > 4.5f) return 1;
    const float first_assist = j2_lift_assist(0.f, -0.065f, 0.21f, 0.f, 0.f, 60.f, 2.f, 0.01f);
    if (first_assist >= 0.f || first_assist < -0.012f) return 1;
    float assist = first_assist;
    for (int i = 0; i < 200; ++i) assist = j2_lift_assist(assist, -0.065f, 0.21f, 0.f, 0.f, 60.f, 2.f, 0.01f);
    if (assist >= -1.f || 60.f * -0.065f + assist < -6.001f) return 1;
    std::cout << "Gesture self-test passed: clearance gate, directions, limits, loss hold, ordered return.\n";
    return 0;
}

int run(ArmMotorBus& bus, const Options& options, int udp, const JointArray& base, const Profile& profile) {
    Schedule schedule(base, profile);
    JointArray smoothed = base;
    JointArray velocity{};
    const auto start = Clock::now();
    auto next = start;
    auto last_packet = start;
    auto last_log = start;
    uint32_t sequence = 0;
    bool freeze_applied = false;
    float filtered_j2_speed = 0.f;
    float lift_assist = 0.f;
    const float pitch_minimum = std::max(kJ7TestedMinimum, base[6] - kJ7PitchTravel);
    float wrist_position = base[6];
    double wrist_progress_at = 0;
    const auto period = std::chrono::microseconds(1000000 / options.rate_hz);
    while (g_running && schedule.phase != Phase::Done) {
        auto now = Clock::now();
        Packet packet{};
        Action action = Action::Wait;
        float x = 0.f, y = 0.f, vertical = kNoFrame, horizontal = kNoFrame;
        bool received = false;
        while (true) {
            char buffer[64];
            const auto length = recvfrom(udp, buffer, sizeof buffer, 0, nullptr, nullptr);
            if (length < 0) break;
            if (length != sizeof(Packet)) continue;
            std::memcpy(&packet, buffer, sizeof packet);
            if (packet.magic != kPacketMagic || packet.sequence <= sequence ||
                packet.action > Action::BodyOnly || !std::isfinite(packet.x) || !std::isfinite(packet.y) ||
                std::abs(packet.x) > 1.f || std::abs(packet.y) > 1.f || !std::isfinite(packet.vertical) ||
                (std::abs(packet.vertical) > 1.f && packet.vertical != kNoFrame) || !std::isfinite(packet.horizontal) ||
                (std::abs(packet.horizontal) > 0.5f && packet.horizontal != kNoFrame)) continue;
            sequence = packet.sequence;
            received = true;
            last_packet = now;
            if (action != Action::Return) {
                action = packet.action;
                x = packet.x;
                y = packet.y;
                vertical = packet.vertical;
                horizontal = packet.horizontal;
            }
        }
        const auto feedback = bus.states();
        JointArray measured;
        for (int index = 0; index < kArmJointCount; ++index) {
            const auto& state = feedback[index];
            if (state.mode != 1 || state.error_code || state.timeout || !std::isfinite(state.q) ||
                !std::isfinite(state.dq) || !std::isfinite(state.tau_est)) {
                std::cerr << "Invalid motor feedback at J" << index + 1 << '\n';
                return 1;
            }
            measured[index] = state.q;
        }
        const bool live = now - last_packet < std::chrono::milliseconds(400);
        const double elapsed = std::chrono::duration<double>(now - start).count();
        const auto previous_phase = schedule.phase;
        if (std::abs(measured[6] - wrist_position) >= 0.010f ||
            std::abs(measured[6] - schedule.target[6]) < 0.045f) {
            wrist_position = measured[6];
            wrist_progress_at = elapsed;
        } else if (elapsed - wrist_progress_at > 1.6 &&
                   (previous_phase == Phase::Opening || previous_phase == Phase::Following)) {
            std::cout << "J7 stopped advancing. Returning without more force.\n";
            action = Action::Return;
        }
        schedule.update(elapsed, live, action, x, y, measured, vertical, received, feedback[1].dq, horizontal);
        if ((schedule.frozen && !freeze_applied && schedule.phase == Phase::Following) ||
            (previous_phase == Phase::Following && schedule.phase == Phase::RestoreJ2)) {
            for (int joint : {0, 1}) {
                smoothed[joint] = measured[joint];
                velocity[joint] = 0;
            }
        }
        if (previous_phase == Phase::RestoreJ4 && schedule.phase == Phase::RestoreJ2) {
            smoothed[3] = measured[3];
            velocity[3] = 0;
        }
        freeze_applied = schedule.frozen;

        const bool restoring_j2 = schedule.phase == Phase::RestoreJ2;
        const bool wide_following = profile.follow_walking && schedule.phase == Phase::Following && !schedule.frozen;
        const bool lowering_gesture = wide_following && schedule.target[1] > measured[1] + 0.012f;
        const bool damping_j2_down = restoring_j2 || lowering_gesture;
        if (measured[1] < profile.j2_up - 0.040f || measured[1] > kJ2Down + 0.015f ||
            measured[3] < -4.055f || measured[3] > -3.385f ||
            std::abs(feedback[3].dq) > 0.40f ||
            std::abs(feedback[1].tau_est) > 7.5f || std::abs(feedback[3].tau_est) > 7.5f) {
            std::cerr << "J2/J4 motion envelope exceeded in " << phase_name(schedule.phase)
                      << " j2=" << measured[1] << " dq2=" << feedback[1].dq << " tau2=" << feedback[1].tau_est
                      << " j4=" << measured[3] << " dq4=" << feedback[3].dq << " tau4=" << feedback[3].tau_est << '\n';
            return 1;
        }
        if (measured[6] < pitch_minimum - 0.025f || measured[6] > base[6] + 0.035f ||
            std::abs(feedback[6].dq) > 0.32f || std::abs(feedback[6].tau_est) > 2.6f) {
            std::cerr << "J7 camera motion envelope exceeded\n";
            return 1;
        }
        JointArray command;
        const float dt = 1.f / options.rate_hz;
        for (int index = 0; index < kArmJointCount; ++index) {
            float speed = 0.075f;
            if (index == 6) speed = 0.12f;
            else if (index == 3 || (index == 0 && profile.follow_walking)) speed = 0.10f;
            else if (index == 1 && wide_following) speed = lowering_gesture ? 0.065f : 0.105f;
            if (index == 1 && restoring_j2) speed = 0.035f;
            const float desired_velocity = std::clamp(6.f * (schedule.target[index] - smoothed[index]),
                                                        -speed, speed);
            velocity[index] += std::clamp(desired_velocity - velocity[index], -0.25f * dt, 0.25f * dt);
            smoothed[index] += velocity[index] * dt;
            const float lead = index == 1 ? 0.10f : index == 3 ? 0.09f : index == 6 ? 0.025f : 0.03f;
            command[index] = measured[index] + std::clamp(smoothed[index] - measured[index], -lead, lead);
        }
        if (damping_j2_down) {
            // Command only a small downward nudge on the approach to the stop.
            command[1] = std::min(command[1], measured[1] + (lowering_gesture ? 0.030f : 0.018f));
        }
        command[0] = std::clamp(command[0], base[0] - kPanAbsoluteTravel, base[0] + kPanAbsoluteTravel);
        command[1] = std::clamp(command[1], profile.j2_up, kJ2Down);
        command[3] = std::clamp(command[3], std::min(base[3], kJ4OpenTarget), std::max(base[3], kJ4OpenTarget));
        command[6] = std::clamp(command[6], pitch_minimum, base[6]);
        const float base_damping = damping_j2_down ? 3.5f : wide_following ? 2.0f : options.kd;
        const float j2_kd = j2_damping_gain(feedback[1].dq, filtered_j2_speed, base_damping);
        if (wide_following && !lowering_gesture && schedule.target[1] < base[1] - 0.05f &&
            schedule.target[1] < measured[1] && measured[3] >= kJ4Clearance) {
            lift_assist = j2_lift_assist(lift_assist, command[1] - measured[1],
                                         measured[1] - schedule.target[1], filtered_j2_speed,
                                         feedback[1].dq, options.kp, j2_kd, dt);
        } else {
            lift_assist = 0.f;
        }
        sendFocPosition(bus, command, options.kp, options.kd, j2_kd, lift_assist);
        if (now - last_log >= std::chrono::milliseconds(200)) {
            std::cout << "GESTURE " << phase_name(schedule.phase)
                      << " j1=" << measured[0] << " j2=" << measured[1] << " j4=" << measured[3]
                      << " t1=" << schedule.target[0] << " t2=" << schedule.target[1]
                      << " t4=" << schedule.target[3] << " j7=" << measured[6]
                      << " t7=" << schedule.target[6] << " screen_x_error=" << schedule.horizontal_error
                      << " screen_y_error=" << schedule.camera_error
                      << " body_pan=" << schedule.pan_center << " action=" << static_cast<uint32_t>(action)
                      << " hand_x=" << schedule.hand_x << " hand_y=" << schedule.hand_y
                      << " assist=" << lift_assist << " kd2=" << j2_kd
                      << " dq2=" << feedback[1].dq << " tau2=" << feedback[1].tau_est << '\n';
            last_log = now;
        }
        next += period;
        std::this_thread::sleep_until(next);
    }
    return schedule.phase == Phase::Done ? 0 : 1;
}

}  // namespace gesture
