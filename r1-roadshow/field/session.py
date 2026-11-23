#!/usr/bin/env python3
"""Save one field run, its diagnostics, and an optional camera recording."""
import argparse
from datetime import datetime, timezone
import glob
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from report import summarize
from calibration import inspect as inspect_calibration

POWERED_MODES = {
    'standby-rehearsal-record': '--standby-rehearsal',
    'level-retreat-record': '--level-retreat',
    'amplitude-probe-record': '--amplitude-probe',
    'pan-powered-record': '--pan-powered',
    'pan-tilt-powered-record': '--pan-tilt-powered',
    'wrist-tabletop-record': '--wrist-tabletop',
    'lateral-probe-record': '--lateral-probe',
    'straight-lateral-record': '--straight-lateral',
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_ffmpeg():
    configured = os.environ.get('R1_FFMPEG')
    if configured:
        return configured
    installed = shutil.which('ffmpeg')
    if installed:
        return installed
    # Reuse the imageio ffmpeg already used by the board's existing recorder.
    pattern = str(Path.home() / 'cix-loop-harness/.state/voice-stream-*/runtime/lib/'
                  'python*/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-aarch64*')
    candidates = sorted(glob.glob(pattern))
    if candidates:
        return candidates[-1]
    raise RuntimeError('Set R1_FFMPEG to the installed ffmpeg executable')


def stop_process(process, first_signal):
    if process is None or process.poll() is not None:
        return
    process.send_signal(first_signal)
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def finish_recording(process):
    if process is None or process.stdin.closed:
        return
    try:
        if process.poll() is None:
            process.stdin.write('q\n')
            process.stdin.flush()
            process.wait(timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        stop_process(process, signal.SIGINT)
    finally:
        process.stdin.close()


def start_recording(folder, camera, video_source, duration=40):
    if not video_source:
        check = subprocess.run(['fuser', str(camera)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if check.returncode == 0:
            raise RuntimeError('C60E is in use. End its current capture before starting pan-record.')
        if check.returncode != 1:
            raise RuntimeError('Could not determine whether the camera is already in use')
    inputs = (['-re', '-i', str(video_source)] if video_source else
              ['-f', 'v4l2', '-input_format', 'mjpeg', '-video_size', '3840x2160',
               '-framerate', '30', '-i', str(camera)])
    args = [find_ffmpeg(), '-hide_banner', '-loglevel', 'error', '-n', '-stats_period', '0.1',
            '-progress', 'pipe:1', *inputs, '-map', '0:v:0', '-an', '-c:v', 'copy',
            '-t', str(duration), '-f', 'matroska', str(folder / 'camera.mkv')]
    with (folder / 'camera-command.json').open('w') as file:
        json.dump(args, file, ensure_ascii=False)
    events = queue.Queue()
    errors = (folder / 'camera.log').open('w')
    try:
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
    except BaseException:
        errors.close()
        raise

    def read_progress():
        try:
            for line in process.stdout:
                events.put(line.strip())
        finally:
            process.stdout.close()
            events.put(None)

    thread = threading.Thread(target=read_progress, daemon=True)
    thread.start()
    deadline = time.monotonic() + 8
    stats = {}
    try:
        while time.monotonic() < deadline:
            try:
                line = events.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                raise RuntimeError('Camera recording ended before the first video frame')
            if '=' in line:
                key, value = line.split('=', 1)
                stats[key] = value.strip()
            # Recent ffmpeg versions omit frame= for stream-copy outputs.
            # This output maps only video, so a positive media time and output
            # byte count establish that a video packet has been muxed.
            if (line == 'progress=continue' and int(stats.get('out_time_us', '0')) > 0
                    and int(stats.get('total_size', '0')) > 0 and process.poll() is None):
                return process, errors, thread
        raise RuntimeError('Camera did not provide the first recorded frame within eight seconds')
    except BaseException:
        finish_recording(process)
        errors.close()
        thread.join(timeout=1)
        raise


def run(args):
    root = Path(__file__).resolve().parents[1]
    calibration = args.calibration.resolve()
    standby = root / 'field/standby-reference.txt'
    reference = args.reference.resolve() if args.reference else root / 'field/pan-reference.txt'
    powered = args.mode in POWERED_MODES
    rehearsal = args.mode == 'standby-rehearsal-record'
    level_retreat = args.mode == 'level-retreat-record'
    amplitude_probe = args.mode == 'amplitude-probe-record'
    if args.mode != 'snapshot':
        if not standby.is_file():
            raise ValueError('The approved standby reference is missing')
        if not powered:
            if args.reference and reference != standby:
                raise ValueError('Use the approved standby pose for playback')
            reference = standby
    action = {'snapshot': '--snapshot', 'pan': '--pan-smoke', 'pan-record': '--pan-smoke',
              'pan-wide-record': '--pan-wide', **POWERED_MODES}[args.mode]
    command = [str(args.runner.resolve()), action,
               str(args.port), str(calibration)]
    if args.mode != 'snapshot':
        command.append(str(reference))
        if powered:
            command.append(str(standby))
        if args.lateral_plan:
            command.append(str(args.lateral_plan.resolve()))
        if args.pan_deg is not None:
            command.append(str(args.pan_deg))
        if args.trim_park:
            command.append('--trim-park')
        if level_retreat:
            command.append(str(args.level_wrist_rad))
        if amplitude_probe:
            command.append(str(args.amplitude_plan.resolve()))
    args.output.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=datetime.now().strftime('%Y%m%d-%H%M%S-') + args.mode + '-',
                                  dir=args.output.resolve()))
    print(f'Run files: {folder}', flush=True)
    info = {
        'mode': args.mode, 'started_at_utc': datetime.now(timezone.utc).isoformat(),
        'runner_sha256': digest(args.runner), 'calibration_sha256': digest(calibration),
        'command': command,
    }
    if args.mode != 'snapshot':
        info['standby_reference_sha256'] = digest(standby)
    video = None
    video_errors = None
    reader = None
    arm = None
    result = 1
    failure = None
    park_requested = threading.Event()
    if powered:
        signal.signal(signal.SIGTERM, lambda *_: park_requested.set())
        signal.signal(signal.SIGUSR2, lambda *_: park_requested.set())
    advance_requested = threading.Event()
    return_requested = threading.Event()
    level_phase = None
    level_take = 0
    level_take_folder = folder
    if level_retreat:
        info['level_retreat_phases'] = {}
        info['level_retreat_takes'] = {}
        signal.signal(signal.SIGUSR1, lambda *_: advance_requested.set())
        signal.signal(signal.SIGHUP, lambda *_: return_requested.set())
    park_sent = False

    def save_session():
        pending = folder / 'session.json.pending'
        pending.write_text(json.dumps(info, indent=2, ensure_ascii=False) + '\n')
        pending.replace(folder / 'session.json')

    try:
        if args.mode in ('pan-record', 'pan-wide-record'):
            duration = 55 if args.mode == 'pan-wide-record' else 40
            video, video_errors, reader = start_recording(folder, args.camera, args.video_source, duration)
            info['first_video_observed_wall_s'] = time.time()
        arm_lines = queue.Queue()
        info['arm_command_wall_s'] = time.time()
        arm = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1)
        info['arm_pid'] = arm.pid
        save_session()

        def read_arm():
            try:
                for line in arm.stdout:
                    arm_lines.put(line)
            finally:
                arm.stdout.close()
                arm_lines.put(None)

        arm_reader = threading.Thread(target=read_arm, daemon=True)
        arm_reader.start()
        with (folder / 'arm.log').open('w') as output:
            while True:
                try:
                    line = arm_lines.get(timeout=0.1)
                    if line is None:
                        break
                    output.write(line)
                    output.flush()
                    if not line.startswith('TELEMETRY '):
                        print(line, end='', flush=True)
                    verified_standby = powered and line.startswith('STANDBY_HELD_5S ')
                    if verified_standby:
                        info['standby_actual_rad'] = json.loads(line.split(' ', 1)[1])
                    if level_retreat and line.startswith('LEVEL_RETREAT_'):
                        marker, pose = line.split(' ', 1)
                        if marker == 'LEVEL_RETREAT_STANDBY':
                            level_take += 1
                            level_phase = None
                            failure = None
                            if level_take > 1:
                                if reader:
                                    reader.join(timeout=1)
                                if video_errors:
                                    video_errors.close()
                                video = video_errors = reader = None
                                level_take_folder = folder / 'takes' / f'{level_take:04d}'
                                level_take_folder.mkdir(parents=True)
                            info['level_retreat_take'] = level_take
                            info['level_retreat_phases'] = {}
                            for key in ('first_video_observed_wall_s', 'level_retreat_complete', 'video_returncode'):
                                info.pop(key, None)
                        info['level_retreat_phases'][marker] = {'q_sdk_rad': json.loads(pose), 'wall_s': time.time()}
                        if marker in ('LEVEL_RETREAT_VERIFY_REFERENCE', 'LEVEL_RETREAT_HOLDING',
                                      'LEVEL_RETREAT_READY_FOR_TAKE'):
                            level_phase = marker
                        if marker in ('LEVEL_RETREAT_RETURNED_POWERED', 'LEVEL_RETREAT_RETURN_INCOMPLETE',
                                      'LEVEL_RETREAT_STANDBY_INCOMPLETE', 'LEVEL_RETREAT_CANCELED_POWERED'):
                            level_phase = None
                            finish_recording(video)
                            info['level_retreat_complete'] = (
                                marker == 'LEVEL_RETREAT_RETURNED_POWERED'
                                and 'LEVEL_RETREAT_HOLDING' in info['level_retreat_phases'])
                            info['powered_at_standby'] = 'LEVEL_RETREAT_RETURNED_POWERED' in info['level_retreat_phases']
                            info['video_returncode'] = video.returncode if video else None
                            info['level_retreat_takes'][str(level_take)] = {
                                'phases': info['level_retreat_phases'].copy(),
                                'video': str((level_take_folder / 'camera.mkv').relative_to(folder)) if video else None,
                                'first_video_observed_wall_s': info.get('first_video_observed_wall_s'),
                                'complete': info['level_retreat_complete'],
                                'camera_failure': failure,
                            }
                            (folder / 'report.json').write_text(json.dumps(summarize(folder / 'arm.log'), indent=2) + '\n')
                        save_session()
                    if level_retreat:
                        record_now = line.startswith('LEVEL_RETREAT_STANDBY ')
                    elif rehearsal:
                        record_now = line.strip() == 'REHEARSAL_STANDBY 1' and 'standby_actual_rad' in info
                    else:
                        record_now = verified_standby
                    if record_now and not park_requested.is_set():
                        try:
                            duration = {
                                'level-retreat-record': 180,
                                'amplitude-probe-record': 180,
                                'pan-tilt-powered-record': 90, 'wrist-tabletop-record': 75,
                                'lateral-probe-record': 115,
                                'straight-lateral-record': 35,
                            }.get(args.mode, 65)
                            video, video_errors, reader = start_recording(
                                level_take_folder if level_retreat else folder, args.camera, args.video_source, duration)
                            info['first_video_observed_wall_s'] = time.time()
                            if arm.poll() is None and not park_requested.is_set() and not return_requested.is_set():
                                arm.send_signal(signal.SIGUSR1)
                        except (OSError, RuntimeError) as error:
                            failure = str(error)
                            if level_retreat:
                                return_requested.set()
                            else:
                                park_requested.set()
                            print(f'Camera failed while the arm held the open position: {failure}',
                                  file=sys.stderr, flush=True)
                        save_session()
                    if powered and line.startswith(('PAN_COMPLETE_HOLDING_OPEN', 'PAN_INCOMPLETE_HOLDING_OPEN')):
                        finish_recording(video)
                        info['pan_complete'] = line.startswith('PAN_COMPLETE_HOLDING_OPEN')
                        info['video_returncode'] = video.returncode if video else None
                        (folder / 'report.json').write_text(json.dumps(summarize(folder / 'arm.log'), indent=2) + '\n')
                        save_session()
                    if rehearsal and line.startswith(('REHEARSAL_RETURNED_POWERED ', 'REHEARSAL_INCOMPLETE_POWERED ')):
                        marker, take, left, right = line.split()
                        if take == '1':
                            finish_recording(video)
                            info['pan_complete'] = marker == 'REHEARSAL_RETURNED_POWERED'
                            info['powered_at_standby'] = True
                            info['yaw_extremes_deg'] = [float(left), float(right)]
                            info['return_to_standby_wall_s'] = time.time()
                            info['video_returncode'] = video.returncode if video else None
                            (folder / 'report.json').write_text(json.dumps(summarize(folder / 'arm.log'), indent=2) + '\n')
                            save_session()
                    if args.mode == 'wrist-tabletop-record' and line.strip() in ('WRIST_COMPLETE', 'WRIST_INCOMPLETE'):
                        finish_recording(video)
                        info['wrist_complete'] = line.strip() == 'WRIST_COMPLETE'
                        save_session()
                    if args.mode == 'lateral-probe-record' and line.strip() in ('LATERAL_PROBE_COMPLETE', 'LATERAL_PROBE_INCOMPLETE'):
                        finish_recording(video)
                        info['probe_complete'] = line.strip() == 'LATERAL_PROBE_COMPLETE'
                        save_session()
                    if args.mode == 'straight-lateral-record' and line.strip() in ('LATERAL_PASS_RECORDED', 'LATERAL_PASS_INCOMPLETE'):
                        finish_recording(video)
                        info['lateral_recorded'] = line.strip() == 'LATERAL_PASS_RECORDED'
                        save_session()
                    if amplitude_probe and line.strip() in ('AXIS_PROBE_COMPLETE', 'AXIS_PROBE_INCOMPLETE'):
                        finish_recording(video)
                        info['amplitude_probe_complete'] = line.strip() == 'AXIS_PROBE_COMPLETE'
                        info['powered_at_standby'] = info['amplitude_probe_complete']
                        info['video_returncode'] = video.returncode if video else None
                        (folder / 'report.json').write_text(json.dumps(summarize(folder / 'arm.log'), indent=2) + '\n')
                        save_session()
                except queue.Empty:
                    pass
                except KeyboardInterrupt:
                    failure = 'Interrupted by operator'
                    stop_process(arm, signal.SIGINT)
                if powered and park_requested.is_set() and not park_sent and arm.poll() is None:
                    arm.send_signal(signal.SIGUSR2)
                    park_sent = True
                if level_retreat and arm.poll() is None and not park_sent:
                    if return_requested.is_set():
                        return_requested.clear()
                        level_phase = None
                        arm.send_signal(signal.SIGHUP)
                    if advance_requested.is_set():
                        advance_requested.clear()
                        if level_phase == 'LEVEL_RETREAT_READY_FOR_TAKE' or (
                                level_phase and video is not None and video.poll() is None):
                            level_phase = None
                            arm.send_signal(signal.SIGUSR1)
                if (video is not None and video.poll() is not None and arm.poll() is None
                        and not any(key in info for key in ('pan_complete', 'wrist_complete', 'product_complete',
                                                            'probe_complete', 'lateral_recorded', 'level_retreat_complete',
                                                            'amplitude_probe_complete'))
                        and not park_sent):
                    if level_retreat:
                        if not failure:
                            failure = 'Video ended during the level retreat'
                            return_requested.set()
                        continue
                    if powered:
                        failure = 'Video ended during arm playback'
                        park_requested.set()
                        continue
                    stop_process(arm, signal.SIGINT)
                    failure = 'Video ended during arm playback'
            result = arm.wait()
        arm_reader.join(timeout=1)
    except (KeyboardInterrupt, OSError, RuntimeError, ValueError) as error:
        failure = str(error) or 'Interrupted by operator'
    finally:
        stop_process(arm, signal.SIGINT)
        finish_recording(video)
        if reader:
            reader.join(timeout=1)
        if video_errors:
            video_errors.close()
        if 'powered_at_standby' in info:
            info['powered_at_standby'] = False
        info.update({
            'finished_at_utc': datetime.now(timezone.utc).isoformat(),
            'arm_returncode': arm.returncode if arm else None,
            'video_returncode': video.returncode if video else None,
            'failure': failure,
        })
        save_session()
        log = folder / 'arm.log'
        if log.exists() and args.mode != 'snapshot':
            report = summarize(log)
            (folder / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
        elif log.exists() and result == 0:
            report = inspect_calibration(log)
            (folder / 'model-check.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    if failure:
        print(f'Session failed: {failure}', file=sys.stderr)
        return 1
    return result


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('snapshot', 'pan', 'pan-record', 'pan-wide-record', *POWERED_MODES))
    parser.add_argument('--runner', type=Path, default=os.environ.get('R1_PLAYER', root / 'build/r1_play'))
    parser.add_argument('--port', type=Path, default=os.environ.get('R1_PORT', '/dev/ttyUSB0'))
    parser.add_argument('--calibration', type=Path, default=os.environ.get(
        'R1_CALIBRATION', Path.home() / 'r1-tracker/r1-7a_sdk/config/joint_calib_right.txt'))
    parser.add_argument('--output', type=Path, default=root / 'captures')
    parser.add_argument('--camera', type=Path)
    parser.add_argument('--reference', type=Path, help='Approved starting pose for this pan test')
    parser.add_argument('--lateral-plan', type=Path, help='Locally measured straight-lateral displacement')
    parser.add_argument('--pan-deg', type=float, choices=range(2, 7), help='Pan swing per side in degrees (default: 6)')
    parser.add_argument('--trim-park', action='store_true', help='Correct a nearby supported folded pose before rehearsal')
    parser.add_argument('--level-wrist-rad', type=float, help='Reference wrist angle in SDK radians for level-retreat-record')
    parser.add_argument('--amplitude-plan', type=Path, help='Field step plan for amplitude-probe-record')
    parser.add_argument('--video-source', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if (args.mode == 'pan-wide-record' or args.mode in POWERED_MODES) and args.pan_deg is not None:
        parser.error('--pan-deg applies to the symmetric pan test')
    if args.mode in POWERED_MODES and not args.reference:
        parser.error(f'{args.mode} requires a surveyed park reference')
    if (args.mode == 'straight-lateral-record') != bool(args.lateral_plan):
        parser.error('--lateral-plan is required for straight-lateral-record')
    if args.trim_park and args.mode != 'standby-rehearsal-record':
        parser.error('--trim-park applies only to standby-rehearsal-record')
    if (args.mode == 'level-retreat-record') != (args.level_wrist_rad is not None):
        parser.error('--level-wrist-rad is required for level-retreat-record')
    if args.level_wrist_rad is not None and not math.isfinite(args.level_wrist_rad):
        parser.error('--level-wrist-rad must be finite')
    if (args.mode == 'amplitude-probe-record') != (args.amplitude_plan is not None):
        parser.error('--amplitude-plan is required for amplitude-probe-record')
    cameras = sorted(glob.glob('/dev/v4l/by-id/*C60E*video-index0'))
    if args.camera is None:
        args.camera = Path(cameras[0]) if cameras else Path('/dev/video-C60E-not-connected')
    signal.signal(signal.SIGTERM, lambda *_: signal.default_int_handler(signal.SIGINT, None))
    try:
        return run(args)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Session failed: {error}\n')


if __name__ == '__main__':
    sys.exit(main())
