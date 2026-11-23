#include <array>
#include <chrono>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <thread>

#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

#include "MotorProtocol.h"
#include "SerialPort.h"

struct Sample {
    float position = 0;
    int output_count = -1;
    unsigned mode = 0;
    unsigned error = 0;
};

int main(int argc, char** argv) {
    const int seconds = argc > 1 ? std::atoi(argv[1]) : 2;
    if (seconds < 1 || seconds > 60) return 2;
    const int lock = open("/tmp/r1-arm-server.lock", O_CREAT | O_RDWR, 0644);
    if (lock < 0 || flock(lock, LOCK_EX | LOCK_NB) != 0) {
        std::cerr << "Arm controller is busy\n";
        return 1;
    }

    MotorProtocol protocol;
    SerialPort serial;
    if (!serial.open("/dev/ttyUSB0", 6000000, 0.05)) return 1;
    std::array<Sample, 7> latest{};
    std::mutex mutex;
    serial.startAsyncRead([&](const std::vector<uint8_t>& packet) {
        const auto feedback = protocol.parseFeedbackPacket(packet);
        if (!feedback || feedback->motor_id >= latest.size() || packet.size() != 26) return;
        // The feedback extension contains an unsigned 13-bit output-side count.
        const unsigned extended = packet[18] | (unsigned(packet[19]) << 8);
        std::lock_guard<std::mutex> guard(mutex);
        latest[feedback->motor_id] = {feedback->position, int(extended & 0x1fff),
                                     feedback->mode, feedback->error_code};
    });

    std::array<std::vector<uint8_t>, 7> stop_packets;
    for (size_t i = 0; i < stop_packets.size(); ++i) {
        stop_packets[i] = protocol.buildControlPacket(i, 0, false, 0, 0, 0, 0, 0);
    }
    using Clock = std::chrono::steady_clock;
    const auto started = Clock::now();
    auto last_report = started;
    bool all_ready = false;
    while (Clock::now() - started < std::chrono::seconds(seconds)) {
        for (const auto& packet : stop_packets) {
            serial.write(packet);
            std::this_thread::sleep_for(std::chrono::microseconds(300));
        }
        const auto now = Clock::now();
        if (now - last_report >= std::chrono::milliseconds(250)) {
            std::lock_guard<std::mutex> guard(mutex);
            all_ready = true;
            std::cout << std::fixed << std::setprecision(3)
                      << std::chrono::duration<double>(now - started).count();
            for (size_t i = 0; i < latest.size(); ++i) {
                const auto& sample = latest[i];
                if (sample.output_count < 0 || sample.mode != 0 || sample.error != 0) all_ready = false;
                std::cout << " J" << i + 1 << '=' << sample.position << ':' << sample.output_count
                          << ':' << sample.mode << ':' << sample.error;
            }
            std::cout << std::endl;
            last_report = now;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(15));
    }
    serial.stopAsyncRead();
    serial.close();
    close(lock);
    return all_ready ? 0 : 1;
}
