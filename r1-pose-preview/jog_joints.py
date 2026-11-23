#!/usr/bin/env python3
"""Run a supervised, one-axis-at-a-time jog on the O6N using the existing arm server."""

import argparse
import math
from pathlib import Path
import queue
import re
import signal
import socket
import struct
import subprocess
import threading
import time


SDK = Path.home() / 'r1-tracker/r1-7a_sdk'
PORT = 9881
WINDOW = 0.18  # rad
J2_MANUAL_RANGE = (0.935, 1.292)  # calibrated positions observed with the motors stopped
# Same encoder branch used by arm_server. These axes were moved by hand with the motors stopped.
MANUAL_JOG_RANGES = {2: (1.695, 3.934), 3: (-4.016, -3.386), 4: (-5.361, -4.951)}
J7_CAMERA_RANGE = (-3.603, -3.538)  # stop-mode measurements after the manual repositioning
URDF_LIMITS = ((-2.723, 2.723), (0, 3.1416), (-0.349, 3.491), (0, 3.1415),
               (-2.269, 2.269), (-0.785, 0.785), (-1.571, 1.571))
FEEDBACK = re.compile(r'\bj(\d) des=(-?\d+\.\d+) meas=(-?\d+\.\d+) m=(\d+) to=(\d+)')


def interrupted(signum, _frame):
    raise KeyboardInterrupt(f'signal {signum}')


class ArmSession:
    def __init__(self):
        self.lines = queue.Queue()
        self.seq = 0
        self.feedback = {}
        self.base = None
        self.armed = False
        self.j1_hold = None

    def read_log(self):
        for line in self.proc.stdout:
            self.lines.put(line.rstrip())

    def read_feedback(self):
        while True:
            try:
                line = self.lines.get_nowait()
            except queue.Empty:
                break
            print(line, flush=True)
            if line.startswith('base pose q:'):
                self.base = [float(value) for value in line.split(':', 1)[1].split()]
            elif line.startswith('armed.'):
                self.armed = True
            for index, _desired, measured, mode, timeout in FEEDBACK.findall(line):
                if mode != '1' or timeout != '0':
                    raise RuntimeError(f'J{int(index) + 1}: invalid motor feedback')
                self.feedback[int(index)] = float(measured)
        if self.proc.poll() is not None:
            raise RuntimeError(f'arm_server exited ({self.proc.returncode})')

    def send(self, socket_handle, joint, offset):
        self.seq += 1
        hold_j1 = joint != 0 and self.j1_hold is not None
        flags = 3 if hold_j1 else 1
        second_offset = self.j1_hold if hold_j1 else 0.0
        packet = struct.pack('<IHHIffBBHI', 0x52544B31, 1, 1, self.seq, offset, second_offset,
                             joint, 0, flags, 0)
        socket_handle.sendto(packet, ('127.0.0.1', PORT))

    def hold(self, socket_handle, joint, offset, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.read_feedback()
            self.send(socket_handle, joint, offset)
            time.sleep(0.05)

    def limit_baseline(self, joint):
        baseline = self.base[joint]
        lower, upper = URDF_LIMITS[joint]
        if joint == 0 and not lower <= baseline <= upper:
            # J1 is a rotary axis with unit scale and a physical range under one turn.
            # Use the equivalent angle for the limit check only. The server retains its
            # original encoder branch and receives just the small relative offset.
            equivalent = math.remainder(baseline, math.tau)
            if lower <= equivalent <= upper:
                print(f'J1 limit check: {baseline:.3f} rad is equivalent to {equivalent:.3f} rad', flush=True)
                baseline = equivalent
        if not lower <= baseline <= upper:
            raise RuntimeError(f'J{joint + 1} starts at {baseline:.3f} rad, outside [{lower}, {upper}]')
        return baseline

    def move_j2_to_54(self, socket_handle, baseline):
        target = math.radians(54)
        if not 1.26 <= baseline <= J2_MANUAL_RANGE[1]:
            raise RuntimeError(f'J2 is no longer at the approximately 74° starting pose ({baseline:.3f} rad)')
        print('J2 54-degree test starts in 4 seconds', flush=True)
        self.hold(socket_handle, 1, 0, 4.0)
        if abs(self.feedback[1] - baseline) > 0.025:
            raise RuntimeError('J2 is not stable at the starting pose')

        print('J2 breakaway check', flush=True)
        self.hold(socket_handle, 1, -0.105, 4.0)
        self.read_feedback()
        probe_angle = self.feedback[1]
        moved = baseline - probe_angle >= 0.025
        reached = False
        if moved:
            print('J2 moving to 54 degrees', flush=True)
            self.hold(socket_handle, 1, target - baseline, 11.0)
            self.read_feedback()
            reached = abs(self.feedback[1] - target) <= 0.025
        print(f'J2 measured {math.degrees(self.feedback[1]):.2f} degrees before return', flush=True)
        self.hold(socket_handle, 1, 0, 10.0 if moved else 3.5)
        self.read_feedback()
        print(f'J2 returned to {math.degrees(self.feedback[1]):.2f} degrees', flush=True)
        if abs(self.feedback[1] - baseline) > 0.025:
            raise RuntimeError('J2 did not return to the starting pose')
        if not reached:
            reason = 'insufficient breakaway motion' if not moved else 'final angle was outside tolerance'
            raise RuntimeError(f'J2 did not reach 54 degrees: {reason}')

    def run(self, args):
        axes = [joint - 1 for joint in args.joints]
        if args.camera_pitch_probe:
            window, lead, velocity = 0.038, '0.025', '0.045'
        elif args.manual_range:
            window, lead, velocity = 0.12, '0.08', '0.075'
        elif args.j2_to_54:
            window, lead, velocity = 0.36, '0.10', '0.05'
        elif args.j2_free_side:
            window, lead, velocity = 0.10, '0.06', '0.075'
        elif args.retry_j2:
            window, lead, velocity = WINDOW, '0.06', '0.10'
        else:
            window, lead, velocity = (0.27 if args.visible_j1 else WINDOW), '0.03', '0.12'
        server = args.telemetry or SDK / 'bin/arm_server'
        self.proc = subprocess.Popen(
            [str(server), '-r', '--port', '/dev/ttyUSB0',
             '--calib', str(SDK / 'config/joint_calib_right.txt'),
             '--udp-port', str(PORT), '--window', str(window), '--v-max', velocity,
             '--a-max', '0.32', '--lead', lead, '--max-err', '0.12'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        reader = threading.Thread(target=self.read_log, daemon=True)
        reader.start()
        try:
            deadline = time.monotonic() + 5
            while not self.armed and time.monotonic() < deadline:
                self.read_feedback()
                time.sleep(0.05)
            if not self.armed or self.base is None or len(self.base) != 7:
                raise RuntimeError('Could not establish a seven-joint starting pose')

            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as socket_handle:
                if args.j2_to_54:
                    self.move_j2_to_54(socket_handle, self.base[1])
                    return
                unmoved = []
                for joint in axes:
                    baseline = self.base[joint]
                    if args.j2_free_side and not J2_MANUAL_RANGE[0] + window <= baseline <= J2_MANUAL_RANGE[1]:
                        raise RuntimeError(f'J2 jog would leave the manually verified range ({baseline:.3f} rad)')
                    if args.camera_pitch_probe:
                        lower, upper = J7_CAMERA_RANGE
                        limit_baseline = baseline
                        if not lower <= baseline <= upper:
                            raise RuntimeError(f'J7 is outside its observed branch ({baseline:.3f} rad)')
                    elif args.manual_range:
                        lower, upper = MANUAL_JOG_RANGES[joint]
                        limit_baseline = baseline
                        if not lower - 0.003 <= baseline <= upper + 0.003:
                            raise RuntimeError(f'J{joint + 1} is outside the observed manual range ({baseline:.3f} rad)')
                    else:
                        limit_baseline = self.limit_baseline(joint)
                        lower, upper = URDF_LIMITS[joint]
                    destination = args.j1_reference if joint == 0 and args.j1_reference is not None else baseline
                    return_offset = destination - baseline
                    if return_offset and not (abs(return_offset) <= window and
                                              lower <= limit_baseline + return_offset <= upper):
                        raise RuntimeError('The J1 reference is outside the permitted jog window')
                    if args.camera_pitch_probe:
                        offsets = (-window,)
                    elif args.manual_range:
                        preferred = math.copysign(window, (lower + upper) / 2 - baseline)
                        offsets = (preferred, -preferred)
                    elif args.j2_free_side:
                        offsets = (-window,)
                    elif args.reverse:
                        offsets = (-window,) if args.retry_j2 else (-0.10,)
                    elif joint == 0 and args.j1_reference is not None:
                        offsets = (-window, window)
                    else:
                        offsets = (window, -window)
                    margin = 0.006 if args.camera_pitch_probe else 0.02
                    offset = next((step for step in offsets
                                   if lower + margin <= limit_baseline + step <= upper - margin), None)
                    if offset is None:
                        raise RuntimeError(f'J{joint + 1} has insufficient travel at the starting pose')
                    print(f'J{joint + 1} starts in 3 seconds ({offset:+.3f} rad)', flush=True)
                    self.hold(socket_handle, joint, 0, 3.0)
                    if joint not in self.feedback or abs(self.feedback[joint] - baseline) > 0.04:
                        raise RuntimeError(f'J{joint + 1} did not hold its starting pose')
                    print(f'J{joint + 1} outbound', flush=True)
                    if args.visible_j1 or args.camera_pitch_probe:
                        outbound_seconds = 7.0
                    elif args.retry_j2 or args.manual_range:
                        outbound_seconds = 5.0
                    else:
                        outbound_seconds = 3.4
                    self.hold(socket_handle, joint, offset, outbound_seconds)
                    displacement = self.feedback[joint] - baseline
                    print(f'J{joint + 1} returning to {destination:.3f} rad', flush=True)
                    self.hold(socket_handle, joint, return_offset, 5.0 if args.visible_j1 or args.retry_j2 else 3.4)
                    residual = self.feedback[joint] - destination
                    print(f'J{joint + 1} measured outbound {displacement:+.3f} rad, '
                          f'return residual {residual:+.3f} rad', flush=True)
                    if abs(residual) > 0.04:
                        raise RuntimeError(f'J{joint + 1} did not return to its starting pose')
                    if displacement * math.copysign(1.0, offset) < (0.008 if args.camera_pitch_probe else 0.025):
                        if not args.manual_range:
                            raise RuntimeError(f'J{joint + 1} feedback did not confirm the jog')
                        unmoved.append(f'J{joint + 1}')
                    if joint == 0:
                        self.j1_hold = return_offset
                if unmoved:
                    raise RuntimeError(f'Insufficient motion on {", ".join(unmoved)}')
        finally:
            if self.proc.poll() is None:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            reader.join(timeout=1)
            while not self.lines.empty():
                print(self.lines.get_nowait(), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('joints', type=int, nargs='+', choices=range(1, 8))
    parser.add_argument('--j1-reference', type=float, help='Previously observed J1 starting angle')
    parser.add_argument('--reverse', action='store_true', help='Try a single negative diagnostic jog')
    parser.add_argument('--visible-j1', action='store_true', help='Use 0.27 rad and a longer hold for J1 only')
    parser.add_argument('--retry-j2', action='store_true', help='Use a larger tracking lead at a lower speed for J2 only')
    parser.add_argument('--telemetry', type=Path, help='Path to the instrumented arm server')
    parser.add_argument('--j2-free-side', action='store_true', help='Jog J2 -0.10 rad within its manually verified range')
    parser.add_argument('--j2-to-54', action='store_true', help='Check breakaway, then try J2 at 54 degrees before returning')
    parser.add_argument('--manual-range', action='store_true', help='Jog J3–J5 inside their observed manual range')
    parser.add_argument('--camera-pitch-probe', action='store_true', help='Make a small negative J7 camera calibration jog')
    args = parser.parse_args()
    if args.j1_reference is not None and not math.isfinite(args.j1_reference):
        parser.error('--j1-reference must be finite')
    if args.reverse and len(args.joints) != 1:
        parser.error('--reverse requires exactly one joint')
    if args.visible_j1 and (args.joints != [1] or args.reverse):
        parser.error('--visible-j1 requires J1 alone and cannot be combined with --reverse')
    if args.retry_j2 and args.joints != [2]:
        parser.error('--retry-j2 requires J2 alone')
    if args.j2_free_side and not (args.retry_j2 and args.telemetry):
        parser.error('--j2-free-side requires --retry-j2 and --telemetry')
    if args.j2_to_54 and (args.joints != [2] or not args.telemetry or args.j2_free_side or args.reverse):
        parser.error('--j2-to-54 requires J2 alone and the instrumented controller')
    if args.manual_range and (not {joint - 1 for joint in args.joints} <= MANUAL_JOG_RANGES.keys()
                              or not args.telemetry or args.reverse):
        parser.error('--manual-range requires J3, J4, or J5 and the instrumented controller')
    if args.camera_pitch_probe and (args.joints != [7] or not args.telemetry or args.manual_range or args.reverse):
        parser.error('--camera-pitch-probe requires J7 alone and the instrumented controller')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        ArmSession().run(args)
    except (KeyboardInterrupt, RuntimeError) as error:
        parser.exit(1, f'Jog stopped: {error}\n')


if __name__ == '__main__':
    main()
