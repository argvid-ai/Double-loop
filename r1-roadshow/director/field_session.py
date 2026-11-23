#!/usr/bin/env python3
"""Dispatch fresh live-camera decisions to an approved, powered standby player."""
import argparse
import hashlib
import json
from pathlib import Path
import queue
import signal
import socket
import subprocess
import tempfile
import threading
import time

from logic import Director, RULES
from recording import FieldRecording
from standby import FIELD, approved_start, check_start_frames, joint_vector


HERE = Path(__file__).resolve().parent


def stage_catalog(folder, calibration, config_path, stage):
    manifest = json.loads((folder / 'manifest.json').read_text())
    if manifest.get('mode') != 'field' or manifest.get('required_start') != approved_start():
        raise ValueError('Expected field plans built for the recorded 21:06 standby')
    if manifest['config_sha256'] != hashlib.sha256(config_path.read_bytes()).hexdigest():
        raise ValueError('The camera configuration changed. Rebuild the field catalogue.')
    start = joint_vector(manifest['model_start_rad'])
    calibration_data = calibration.read_bytes()
    digest = hashlib.sha256(calibration_data).hexdigest()
    if manifest.get('calibration_sha256') != digest:
        raise ValueError('The field catalogue has a different calibration')
    names = sorted(manifest['plans'])
    allowed = {action for action, _ in RULES.values()} - {'Hold'}
    if not names or not set(names) <= allowed:
        raise ValueError('The field catalogue needs at least one recognized camera motion')
    for name in names:
        item = manifest['plans'][name]
        filename = name + '.r1traj'
        if item['file'] != filename:
            raise ValueError(f'{name}: unexpected trajectory filename')
        data = (folder / filename).read_bytes()
        if hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError(f'{name}: trajectory checksum mismatch')
        check_start_frames(data, name, start, manifest['required_start']['stable_s'], digest)
        (stage / filename).write_bytes(data)
    (stage / 'calibration.txt').write_bytes(calibration_data)
    reference = (FIELD / 'standby-reference.txt').read_bytes()
    if hashlib.sha256(reference).hexdigest() != manifest['required_start']['reference_sha256']:
        raise ValueError('The approved reference changed during preflight')
    (stage / 'standby.txt').write_bytes(reference)
    return names


class CameraTrigger:
    def __init__(self, config, names):
        self.config = config
        self.names = names
        self.confirmations = config['confirmations']
        self.max_age = config['max_observation_age_s']
        self.last_start = float('-inf')
        self.last_action = None
        self.ready_since = None
        self.detector = None
        self.last_frame = None
        self.empty_streak = 0

    def ready(self, now):
        self.ready_since = now
        self.last_frame = None
        self.detector = Director(confirmations=self.confirmations, max_age_s=self.max_age)
        self.empty_streak = 0

    def observe(self, event, now):
        if not isinstance(event, dict):
            return None
        stamp = event.get('captured_monotonic_s')
        if (event.get('source_kind') != 'camera' or type(stamp) not in (int, float)
                or self.ready_since is None or not self.ready_since <= stamp <= now):
            return None
        previous = self.last_frame
        if previous is not None and stamp <= previous:
            return None
        self.last_frame = stamp
        if previous is not None and stamp - previous > self.max_age:
            self.empty_streak = 0
        decision = event.get('decision', {})
        detected_people = event.get('people')
        value = decision.get('observation') if isinstance(decision, dict) and isinstance(detected_people, list) else None
        result = self.detector.decide(value, now, stamp,
                                      detector_count=len(detected_people) if value else None)
        self.empty_streak = self.empty_streak + 1 if result['scene'] == 'empty' else 0
        if self.empty_streak >= self.confirmations:
            self.last_action = None
        action = result['action']
        if (result['status'] != 'ready' or action not in self.names or action == self.last_action
                or now - self.last_start < self.config['cooldown_s']):
            return None
        return action

    def started(self, action, now):
        self.last_action = action
        self.last_start = now
        self.ready_since = None


def finish_recording(recording):
    if recording is not None and recording.key is not None:
        try:
            result = recording.finish()
        except OSError as error:
            print(f'CAMERA_RECORDING_ERROR {error}', flush=True)
            return
        if result:
            print('CAMERA_RECORDING_RESULT ' + json.dumps(result, ensure_ascii=False), flush=True)
        else:
            print('CAMERA_RECORDING_ERROR No final recording status', flush=True)


def dispatch(player, connection, vision_path, config, names, stop_requested, recording=None, startup_probe=False,
             frame_people=False):
    lines = queue.Queue()

    def read_player():
        with player.stdout:
            for item in player.stdout:
                lines.put(item.rstrip())
        lines.put(None)

    reader = threading.Thread(target=read_player, daemon=True)
    reader.start()
    # The three commands after the regular routes are preview, tilt up and tilt down.
    shot_names = [*names, None, None, None, 'PortraitStandingPreset'] if frame_people else names
    trigger = CameraTrigger(config, shot_names)
    generation = None
    pending = None
    stopping = False
    framing_stage = None
    framing_seen = None
    framing_since = None
    framing_streak = 0
    framing_key = None
    framed_count = 0
    framing_corrections = 0
    framing_cancelled = False
    while True:
        if stop_requested.is_set() and not stopping:
            player.send_signal(signal.SIGUSR2)
            stopping = True
        try:
            line = lines.get(timeout=.1)
        except queue.Empty:
            line = ''
        if line is None:
            return player.wait()
        if line:
            print(line, flush=True)
        if startup_probe and line.startswith('STANDBY_HELD_5S ') and not stopping:
            try:
                first_frame = recording.start(time.monotonic() + 3, stop_requested)
            except OSError as error:
                print(f'CAMERA_RECORDING_ERROR {error}', flush=True)
                first_frame = {}
            if first_frame.get('state') == 'recording' and not stop_requested.is_set():
                print('AXIS_PROBE_RECORDING_STARTED ' + json.dumps(first_frame, ensure_ascii=False), flush=True)
                player.send_signal(signal.SIGUSR1)
            else:
                finish_recording(recording)
        elif startup_probe and line == 'AXIS_PROBE_INCOMPLETE':
            finish_recording(recording)
        elif line.startswith('DIRECTOR_STANDBY_DISTURBANCE '):
            generation = None
            pending = None
            framing_stage = 'returning'
            framed_count = 0
            finish_recording(recording)
        elif line.startswith('DIRECTOR_STANDBY '):
            finish_recording(recording)
            generation = int(line.split()[1])
            trigger.ready(time.monotonic())
            if frame_people:
                framing_stage = 'standby'
                framing_since = time.monotonic()
                framing_corrections = framing_streak = 0
                framing_key = None
        elif line.startswith('DIRECTOR_FRAMING_READY '):
            generation = int(line.split()[1])
            framing_stage = 'ready'
            framing_since = time.monotonic()
            framing_streak = 0
            framing_key = None
            trigger.ready(framing_since)
        elif line.startswith(('DIRECTOR_FRAMING_INCOMPLETE ', 'DIRECTOR_FRAMING_SWEEP_END ')):
            generation = None
            pending = None
            framing_stage = 'returning'
            finish_recording(recording)
        elif line.startswith('DIRECTOR_ACCEPTED '):
            _, accepted_generation, index = line.split()
            if pending != shot_names[int(index)] or int(accepted_generation) != generation:
                raise RuntimeError('The player accepted a different shot than requested')
            trigger.started(pending, time.monotonic())
            pending = None
            generation = None
            if frame_people:
                framing_stage = 'recording'
                framing_cancelled = False
                framing_streak = 0
                framing_key = None
        elif line.startswith('DIRECTOR_REJECTED '):
            finish_recording(recording)
            pending = None
            trigger.ready(time.monotonic())
            if frame_people:
                generation = int(line.split()[1])
                framing_stage = 'standby'
                framing_since = time.monotonic()
        if (generation is None and framing_stage != 'recording') or pending is not None or stopping or not lines.empty():
            continue
        try:
            event = json.loads(vision_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        now = time.monotonic()
        if frame_people:
            stamp = event.get('captured_monotonic_s')
            if (event.get('source_kind') != 'camera' or type(stamp) not in (float, int)
                    or not max(framing_since, now - trigger.max_age) <= stamp <= now
                    or (framing_seen is not None and stamp <= framing_seen)):
                if (framing_stage == 'recording' and not framing_cancelled and framing_seen is not None
                        and now - framing_seen > trigger.max_age):
                    player.send_signal(signal.SIGHUP)
                    framing_cancelled = True
                    print('CAMERA_FRAMING_LOST stale', flush=True)
                    finish_recording(recording)
                continue
            framing_seen = stamp
            framing = event.get('framing') or {}
            status = framing.get('status')
            count = len(framing.get('people', []))
            key = count if framing_stage == 'standby' else ((status, count) if framing_stage == 'ready' else status == 'ready')
            framing_streak = framing_streak + 1 if key == framing_key else 1
            framing_key = key
            if framing_stage == 'standby':
                if framing_streak >= 2 and status == 'empty':
                    framed_count = 0
                    trigger.last_action = None
                elif framing_streak >= 2 and count and count != framed_count:
                    connection.send(f'{generation} {len(names)}'.encode('ascii'))
                    print(f'CAMERA_FRAMING_REQUEST {generation} {status} {count}', flush=True)
                    framed_count = count
                    generation = None
                    framing_stage = 'adjusting'
                continue
            if framing_stage == 'recording':
                if status != 'ready' and framing_streak >= 2 and not framing_cancelled:
                    player.send_signal(signal.SIGHUP)
                    framing_cancelled = True
                    print(f'CAMERA_FRAMING_LOST {status}', flush=True)
                    finish_recording(recording)
                continue
            if status != 'ready':
                trigger.ready(now)
                if status in ('tilt_up', 'tilt_down') and framing_streak >= 2 and framing_corrections < 2:
                    adjustment = 1 if status == 'tilt_up' else 2
                    connection.send(f'{generation} {len(names) + adjustment}'.encode('ascii'))
                    print(f'CAMERA_FRAMING_ADJUST {generation} {status}', flush=True)
                    framing_corrections += 1
                    generation = None
                    framing_stage = 'adjusting'
                continue
        action = trigger.observe(event, now)
        if action:
            if recording is not None:
                try:
                    first_frame = recording.start(event['captured_monotonic_s'] + trigger.max_age, stop_requested)
                except OSError as error:
                    print(f'CAMERA_RECORDING_ERROR {error}', flush=True)
                    first_frame = {}
                if first_frame.get('state') != 'recording' or stop_requested.is_set():
                    finish_recording(recording)
                    continue
                # Native phase markers use wall time. Keep the camera's first-frame clock anchor with the take.
                first_frame['first_frame_wall_s'] = time.time() + first_frame['first_frame_monotonic_s'] - time.monotonic()
                print('CAMERA_RECORDING_STARTED ' + json.dumps(first_frame, ensure_ascii=False), flush=True)
            connection.send(f'{generation} {shot_names.index(action)}'.encode('ascii'))
            pending = action
            print(f'CAMERA_TRIGGER {generation} {action}', flush=True)
            print('CAMERA_TRIGGER_EVIDENCE ' + json.dumps({
                'generation': generation, 'action': action,
                'scene': event['decision']['observation']['scene'], 'people': len(event['people']),
                'captured_monotonic_s': event['captured_monotonic_s'],
                'sequence': event.get('sequence'), 'session': event.get('session'), 'image': event.get('image'),
            }), flush=True)


def run(args):
    cfg = args.config.resolve()
    config = json.loads(cfg.read_text())
    with tempfile.TemporaryDirectory(prefix='r1-field-') as workspace:
        stage = Path(workspace)
        calibration = stage / 'calibration.txt'
        standby = stage / 'standby.txt'
        runner = args.runner.resolve()
        if args.rehearsal_park:
            required = approved_start()
            cal = args.calibration.read_bytes()
            ref = (FIELD / 'standby-reference.txt').read_bytes()
            park = args.rehearsal_park.read_bytes()
            if (hashlib.sha256(cal).hexdigest() != required['calibration_sha256']
                    or hashlib.sha256(ref).hexdigest() != required['reference_sha256']
                    or park.decode().splitlines()[:1] != [required['calibration_sha256']]):
                raise ValueError('Rehearsal requires the original calibration and surveyed references')
            calibration.write_bytes(cal)
            standby.write_bytes(ref)
            park_file = stage / 'park.txt'
            park_file.write_bytes(park)
            names = subprocess.run([runner, '--director-rehearsal-routes'], check=True,
                                   capture_output=True, text=True).stdout.splitlines()
            allowed = {action for action, _ in RULES.values()} - {'Hold'}
            if not names or len(names) != len(set(names)) or not set(names) <= allowed:
                raise ValueError('The rehearsal player advertised invalid camera motions')
            native = ['--standby-rehearsal-director', args.port, calibration, park_file, standby]
            if args.amplitude_plan:
                amplitude_file = stage / 'amplitude.txt'
                amplitude_file.write_bytes(args.amplitude_plan.read_bytes())
                native[0] = '--amplitude-probe-director'
                native.append(amplitude_file)
            elif args.level_plan:
                level_file = stage / 'level.txt'
                level_file.write_bytes(args.level_plan.read_bytes())
                subprocess.run([runner, '--check-level-director', calibration, standby, level_file], check=True)
                native[0] = '--level-director'
                native.append(level_file)
                if args.frame_people:
                    native.append('--frame-people')
        else:
            names = stage_catalog(args.plans, args.calibration, cfg, stage)
            trajectories = [stage / (name + '.r1traj') for name in names]
            subprocess.run([runner, '--check-standby', calibration, standby, *trajectories], check=True)
            native = ['--standby-director', args.port, calibration, standby, *trajectories]
        print('Approved camera routes:', ', '.join(names), flush=True)
        if not args.port:
            return 0
        stop_requested = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop_requested.set())
        local, remote = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        recording = FieldRecording(args.vision.resolve().parent) if args.record_shots else None
        with local, remote:
            with subprocess.Popen([runner, *native],
                                  stdin=remote, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  start_new_session=True, text=True, bufsize=1) as player:
                remote.close()
                try:
                    return dispatch(player, local, args.vision, config, names, stop_requested, recording,
                                    startup_probe=bool(args.amplitude_plan), frame_people=args.frame_people)
                finally:
                    finish_recording(recording)
                    local.close()
                    if player.poll() is None:
                        player.send_signal(signal.SIGINT)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plans', type=Path, nargs='?')
    parser.add_argument('--rehearsal-park', type=Path, help='Surveyed park for the baseline camera motions')
    parser.add_argument('--calibration', type=Path, required=True)
    parser.add_argument('--port', type=Path, help='Arm serial device. Omit for offline preflight.')
    parser.add_argument('--config', type=Path, default=HERE / 'config.json')
    parser.add_argument('--vision', type=Path, default=HERE / 'runs' / 'latest.json')
    parser.add_argument('--record-shots', action='store_true', help='Record the live camera from before motion through powered return')
    rehearsal = parser.add_mutually_exclusive_group()
    rehearsal.add_argument('--amplitude-plan', type=Path, help='Run a recorded amplitude probe before scene control')
    rehearsal.add_argument('--level-plan', type=Path, help='Use the checked level-retreat landmarks in recorded scene takes')
    parser.add_argument('--frame-people', action='store_true', help='Require live YOLO upper-body framing before and during recorded scene takes')
    parser.add_argument('--runner', type=Path, default=HERE.parent / 'build' / 'r1_play')
    args = parser.parse_args()
    if (args.plans is None) == (args.rehearsal_park is None):
        parser.error('Provide either a calibrated plan catalogue or --rehearsal-park')
    if args.amplitude_plan and (not args.rehearsal_park or not args.record_shots):
        parser.error('--amplitude-plan requires --rehearsal-park and --record-shots')
    if args.level_plan and (not args.rehearsal_park or not args.record_shots):
        parser.error('--level-plan requires --rehearsal-park and --record-shots')
    if args.frame_people and not args.level_plan:
        parser.error('--frame-people requires --level-plan and a pose-enabled vision stream')
    raise SystemExit(run(args))
