#!/usr/bin/env python3
"""Run a supervised C60E gesture trial after confirming mechanical clearance."""

import argparse
import json
import math
from pathlib import Path
import queue
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


HERE = Path(__file__).resolve().parent
PREVIEW = 'http://127.0.0.1:8796'
SDK = Path.home() / 'r1-tracker/r1-7a_sdk'
PORT = 9883
WAIT, CONFIRM, FOLLOW, FREEZE, RETURN, PREPARE, BODY_ONLY = range(7)


def preview(endpoint, *, post=False):
    request = urllib.request.Request(
        PREVIEW + endpoint, data=b'' if post else None, headers={'X-Pose-Preview': '1'}
    )
    with urllib.request.urlopen(request, timeout=0.15) as response:
        return json.load(response)


class PoseInput:
    def __init__(self):
        self.zero_request = None
        self.operator_id = None
        self.frame = None
        self.frame_at = 0.0

    def read(self, status, now):
        frame = status.get('frame')
        if status.get('state') != 'tracking' or frame is None or self.zero_request is None:
            return None
        if status.get('zero_request') != self.zero_request or not correct_operator(status, self.operator_id):
            return None
        box = status.get('person_box')
        if not isinstance(box, list) or len(box) != 4:
            return None
        if any(not isinstance(item, (int, float)) or not 0 <= item <= 1 for item in box):
            return None
        if box[0] >= box[2] or box[1] >= box[3]:
            return None
        if frame != self.frame:
            self.frame, self.frame_at = frame, now
        pair = status.get('target')
        if now - self.frame_at > 0.32 or not isinstance(pair, list) or len(pair) != 2:
            return None
        if any(not isinstance(item, (int, float)) or not math.isfinite(item) or abs(item) > 100 for item in pair):
            return None
        return tuple(item / 100 for item in pair)


class CameraInput:
    def __init__(self):
        self.frame = None
        self.frame_at = 0.0
        self.operator_id = None

    def position(self, status, now):
        box = status.get('person_box')
        frame = status.get('frame')
        if not correct_operator(status, self.operator_id) or not box or not frame:
            return None, None
        if frame != self.frame:
            self.frame, self.frame_at = frame, now
        if now - self.frame_at > 0.30:
            return None, None
        value = status.get('shoulder_y')
        shoulder_y = value if isinstance(value, (int, float)) and 0 <= value <= 1 else None
        if shoulder_y is None and upper_edge_crop(status) and (visible_right_arm(status) or self.operator_id is not None):
            # An upper-edge crop can hide one shoulder while leaving the right arm visible.
            shoulder_y = box[1] + min(0.08, 0.1 * (box[3] - box[1]))
        return (box[0] + box[2]) / 2 - 0.5, shoulder_y


def correct_operator(status, operator_id):
    if operator_id is not None:
        return status.get('operator_id') == operator_id
    return status.get('people') == 1 or status.get('operator_id') is not None


def visible_right_arm(status):
    confidence = status.get('point_confidence')
    return isinstance(confidence, list) and len(confidence) == 4 and min(confidence[1:]) >= 0.45


def upper_edge_crop(status):
    box = status.get('person_box')
    return box is not None and box[1] < 0.04 and box[3] - box[1] >= 0.25


def framing_height(observed):
    return min(0.46, max(0.23, observed))


def wait_for_person(camera):
    deadline = time.monotonic() + (29 if camera.operator_id is not None else 25)
    stable_since = None
    print('Waiting for one person and a usable upper-body view.', flush=True)
    while time.monotonic() < deadline:
        state = preview('/status')
        now = time.monotonic()
        horizontal, shoulder_y = camera.position(state, now)
        if horizontal is not None and shoulder_y is not None:
            stable_since = stable_since if stable_since is not None else now
            if now - stable_since >= 0.4:
                print(f'YOLO framed the operator. Horizontal error is {horizontal:+.3f}.', flush=True)
                return framing_height(shoulder_y)
        else:
            stable_since = None
        time.sleep(0.10)
    raise RuntimeError('The upper body did not enter the frame. No motor commands were sent.')


def dry_run(binary):
    subprocess.run([str(binary), '--self-test'], check=True)
    pose = PoseInput()
    pose.zero_request = 4.0
    example = {'frame': 10, 'state': 'tracking', 'people': 1, 'zero_request': 4.0,
               'person_box': [0.20, 0.10, 0.61, 0.85], 'target': [-60, 80]}
    if (pose.read(example, 10.0) != (-0.6, 0.8) or pose.read(example, 10.4) is not None or
            pose.read({**example, 'frame': 11, 'people': 2}, 10.5) is not None or
            pose.read({**example, 'frame': 11, 'zero_request': 7.0}, 10.6) is not None or
            pose.read({**example, 'frame': 11, 'person_box': None}, 10.6) is not None):
        raise RuntimeError('Pose freshness test failed')
    frame = CameraInput()
    frame_sample = {**example, 'shoulder_y': 0.34}
    screen_x, screen_y = frame.position(frame_sample, 10.0)
    if abs(screen_x + 0.095) > 1e-6 or screen_y != 0.34 or frame.position(frame_sample, 10.4) != (None, None):
        raise RuntimeError('Camera freshness test failed')
    pose.operator_id = frame.operator_id = 5.0
    locked = {**frame_sample, 'frame': 12, 'people': 2, 'operator_id': 5.0}
    if pose.read(locked, 11.0) != (-0.6, 0.8) or frame.position(locked, 11.0)[1] != 0.34:
        raise RuntimeError('The selected operator was lost when a second box appeared')
    if pose.read({**locked, 'operator_id': 6.0}, 11.1) is not None:
        raise RuntimeError('A different operator inherited the tracking session')
    camera = preview('/status')
    print(f'O6N preview reachable, camera frame {camera.get("frame", "unavailable")}. Motor port untouched.')


def read_lines(process, destination, updates):
    for line in process.stdout:
        destination.write(line)
        destination.flush()
        updates.put(line.rstrip())


def run_trial(binary, assume_open, wide, visible_operator):
    if not assume_open and input('Hands clear and support stable? Type open to begin J4 only: ').strip() != 'open':
        return 1
    camera = CameraInput()
    if wide:
        endpoint = '/lock-visible' if visible_operator else '/lock'
        camera.operator_id = preview(endpoint, post=True)['operator_id']
        if not visible_operator:
            print('Face the camera and raise your right hand while keeping the left down to lock the real person.', flush=True)
    shoulder_reference = wait_for_person(camera)
    log = HERE / 'diagnostics' / f'gesture-trial-{time.strftime("%Y%m%d-%H%M%S")}.log'
    updates = queue.Queue()
    pose = PoseInput()
    pose.operator_id = camera.operator_id
    with log.open('w') as destination, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        profile_options = ['--wide'] if wide else []
        process = subprocess.Popen([
            str(binary), *profile_options, '-r', '--port', '/dev/ttyUSB0', '--udp-port', str(PORT),
            '--calib', str(SDK / 'config/joint_calib_right.txt'),
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        reader = threading.Thread(target=read_lines, args=(process, destination, updates), daemon=True)
        reader.start()
        sequence = 0
        phase = ''
        stopping = False
        neutral_since = None
        last_valid = time.monotonic()
        loss_logged = False

        def send(action, pair=(0.0, 0.0), vertical=2.0, horizontal=2.0):
            nonlocal sequence
            sequence += 1
            udp.sendto(struct.pack('<IIIffff', 0x47315231, sequence, action, *pair, vertical, horizontal),
                       ('127.0.0.1', PORT))

        try:
            while process.poll() is None:
                phase_changed = False
                while not updates.empty():
                    line = updates.get_nowait()
                    if not line.startswith('GESTURE '):
                        print(line, flush=True)
                        continue
                    current = line.split()[1]
                    if current != phase:
                        phase = current
                        phase_changed = True
                        print(line, flush=True)
                        if phase == 'verify_clearance':
                            print('Confirm the visible gap above J2. Type go to calibrate the resting hand.', flush=True)
                        if phase == 'following':
                            print(f'Following for {60 if wide else 20} seconds. Video recording is off.', flush=True)
                        if phase == 'hold_for_operator':
                            print('Return could not be verified. The controller is still supporting the arm.', flush=True)
                now = time.monotonic()
                if not stopping and select.select([sys.stdin], [], [], 0)[0]:
                    command = sys.stdin.readline()
                    if not command or command.strip() == 'stop':
                        stopping = True
                    elif command.strip() == 'go' and phase == 'verify_clearance' and pose.zero_request is None:
                        pose.zero_request = preview('/zero', post=True)['zero_request']
                        print('Keep the right hand naturally lowered for the 8-second calibration.', flush=True)
                state = {}
                vertical = 2.0
                horizontal = 2.0
                if not stopping and phase in ('', 'centering_person', 'opening_j4', 'verify_clearance',
                                              'calibrating', 'following'):
                    try:
                        state = preview('/status')
                        screen_x, shoulder_y = camera.position(state, time.monotonic())
                        if screen_x is not None:
                            horizontal = screen_x
                        if shoulder_y is not None:
                            arm_visible = visible_right_arm(state) or (visible_operator and upper_edge_crop(state))
                            if shoulder_reference is None or (phase == 'opening_j4' and phase_changed):
                                shoulder_reference = framing_height(shoulder_y)
                            if phase != 'centering_person' or arm_visible:
                                vertical = shoulder_y - shoulder_reference
                    except (OSError, ValueError):
                        pass
                if stopping:
                    send(RETURN)
                elif phase == 'verify_clearance' and pose.zero_request is not None:
                    send(PREPARE, vertical=vertical, horizontal=horizontal)
                elif phase in ('calibrating', 'following') and pose.zero_request is not None:
                    pair = pose.read(state, time.monotonic())
                    if phase == 'calibrating':
                        if pair is not None and max(map(abs, pair)) <= 0.08:
                            neutral_since = neutral_since if neutral_since is not None else now
                            send(CONFIRM if now - neutral_since >= 0.4 else WAIT,
                                 vertical=vertical, horizontal=horizontal)
                        else:
                            neutral_since = None
                            send(WAIT, vertical=vertical, horizontal=horizontal)
                    elif pair is not None:
                        last_valid = now
                        if loss_logged:
                            print('Operator reacquired. Resuming hand tracking.', flush=True)
                            loss_logged = False
                        send(FOLLOW, pair, vertical, horizontal)
                    elif wide and horizontal != 2.0:
                        last_valid = now
                        if loss_logged:
                            print('Person box reacquired. Resuming body yaw; holding the lift axis.', flush=True)
                            loss_logged = False
                        send(BODY_ONLY, vertical=vertical, horizontal=horizontal)
                    else:
                        if phase_changed:
                            last_valid = now
                        if not loss_logged:
                            print('Person or pose lost. Holding the measured pose.', flush=True)
                            loss_logged = True
                        stopping = now - last_valid > (4.0 if wide else 1.1)
                        send(RETURN if stopping else FREEZE)
                else:
                    send(WAIT, vertical=vertical, horizontal=horizontal)
                time.sleep(0.05)
        except (KeyboardInterrupt, OSError, ValueError):
            send(RETURN)
            # The server completes the return locally. Loss of UDP also requests a return.
        finally:
            if process.poll() is None:
                try:
                    process.wait(timeout=32)
                except subprocess.TimeoutExpired:
                    print('Arm controller still active. Check the supported pose before switching it off.', flush=True)
                    return 1
            reader.join(timeout=1)
            while not updates.empty():
                print(updates.get_nowait(), flush=True)
        print(f'Trial log {log}', flush=True)
        return process.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Enable the supervised hardware trial')
    parser.add_argument('--open-j4', action='store_true', help='Start the opening phase after site readiness is confirmed')
    parser.add_argument('--wide', action='store_true', help='Use the 60-second larger-range walking and gesture trial')
    parser.add_argument('--visible-operator', action='store_true', help='Lock the single operator already confirmed in view')
    args = parser.parse_args()
    binary = HERE / 'diagnostics/gesture_arm'
    if args.live:
        try:
            return run_trial(binary, args.open_j4, args.wide, args.visible_operator)
        except (OSError, RuntimeError) as error:
            parser.exit(1, f'Trial stopped: {error}\n')
    dry_run(binary)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
