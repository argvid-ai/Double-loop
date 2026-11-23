#!/usr/bin/env python3
"""Build the gesture controller beside the preview, using the installed R1 SDK."""

import argparse
from pathlib import Path
import subprocess


HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', type=Path, default=Path.home() / 'r1-tracker/r1-7a_sdk')
    args = parser.parse_args()
    sdk = args.sdk.resolve()
    vendor = sdk / 'thirdparty/motor_controller'
    lib = vendor / 'lib/aarch64'
    original = (sdk / 'src/arm_server.cpp').read_text()
    markers = ('    State state = State::Armed;', 'int main(int argc, char** argv) {',
               '    if (!enableFocAt(bus, q_base, opt)) {', 'const JointArray& q, float kp, float kd) {',
               '        cmds[i].kd = kd;', '        cmds[i].tau = 0.f;')
    if any(original.count(marker) != 1 for marker in markers):
        raise RuntimeError('The installed arm server does not have the expected startup code')
    original = original.split(markers[0], 1)[0]
    original = original.replace(markers[3], 'const JointArray& q, float kp, float kd, float j2_kd = -1.f, float j2_torque = 0.f) {')
    original = original.replace(markers[4], '        cmds[i].kd = i == 1 && j2_kd >= 0.f ? j2_kd : kd;')
    original = original.replace(markers[5], '        cmds[i].tau = i == 1 ? j2_torque : 0.f;')
    original = original.replace('int main(int argc, char** argv) {', '''#include "gesture_arm_loop.h"

int main(int argc, char** argv) {
    if (argc == 2 && std::string(argv[1]) == "--self-test") return gesture::self_test();
    const bool wide = argc > 1 && std::string(argv[1]) == "--wide";
    if (wide) { --argc; ++argv; }
    const auto& gesture_profile = wide ? gesture::kWide : gesture::kStandard;''')
    original = original.replace('    if (!enableFocAt(bus, q_base, opt)) {', '''    if (!gesture::trial_start_ok(q_base)) {
        std::cerr << "Starting pose outside the gesture trial envelope\\n";
        shutdownChain(bus, opt.rate_hz);
        return 1;
    }
    q_base[1] = std::min(q_base[1], gesture::kJ2Down);
    if (!enableFocAt(bus, q_base, opt)) {''')
    build = HERE / 'diagnostics'
    build.mkdir(exist_ok=True)
    source = build / 'gesture_arm.cpp'
    source.write_text(original + '''    const int result = gesture::run(bus, opt, udp, q_base, gesture_profile);
    shutdownChain(bus, opt.rate_hz);
    close(udp);
    return result;
}
''')
    binary = build / 'gesture_arm'
    subprocess.run([
        'g++', '-std=c++17', '-O2', '-pthread', f'-I{HERE}', f'-I{sdk / "include"}',
        f'-I{vendor / "include"}', str(source), str(sdk / 'src/arm_motor_bus.cpp'),
        f'-L{lib}', f'-Wl,-rpath,{lib}', '-lmotor_controller', '-o', str(binary),
    ], check=True)
    subprocess.run([str(binary), '--self-test'], check=True)


if __name__ == '__main__':
    main()
