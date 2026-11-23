#!/usr/bin/env python3
"""Camera -> CIX YOLO + LQ50 VLM -> audited simulation-only shot proposals."""
import argparse
from collections import deque
from datetime import datetime, timezone
import glob
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import signal
import shutil
import threading
import time

import cv2

from logic import Director
from framing import PoseDetector, assess_framing
from recording import CaptureRecorder
from standby import approved_start, check_start_frames, joint_vector
from vision import PersonDetector, VLM, contact_sheet, movement_candidates

HERE = Path(__file__).resolve().parent


def atomic_json(path, value):
    pending = path.with_suffix('.pending')
    pending.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)+'\n')
    pending.replace(path)


class Capture:
    def __init__(self, source, config, video=False, recorder=None):
        self.source, self.config, self.video = source, config, video
        self.frames = deque(maxlen=3)
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.error = None
        self.recorder = recorder
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def loop(self):
        cap = cv2.VideoCapture(self.source) if self.video else cv2.VideoCapture(self.source, cv2.CAP_V4L2)
        try:
            if not cap.isOpened():
                raise RuntimeError('无法打开视频源')
            if not self.video:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                for prop, value in ((cv2.CAP_PROP_FRAME_WIDTH, self.config['camera_width']),
                                    (cv2.CAP_PROP_FRAME_HEIGHT, self.config['camera_height']),
                                    (cv2.CAP_PROP_FPS, self.config['camera_fps'])):
                    cap.set(prop, value)
            fps = cap.get(cv2.CAP_PROP_FPS)
            if not math.isfinite(fps) or fps <= 0:
                fps = 30
            first, next_sample, index = time.monotonic(), 0, 0
            while not self.stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    raise RuntimeError('视频源结束' if self.video else '摄像头取流中断')
                if self.video:
                    self.stop.wait(max(0, first + index/fps-time.monotonic()))
                now = time.monotonic()
                if self.recorder:
                    try:
                        self.recorder.frame(frame, now, fps)
                    except (OSError, cv2.error) as error:
                        print(f'Camera recording error: {error}', flush=True)
                        self.finish_recording(failed=True)
                if now >= next_sample:
                    with self.lock:
                        self.frames.append((now, frame))
                    next_sample = now + self.config['sample_interval_s']
                index += 1
        except Exception as error:
            self.error = str(error)
        finally:
            self.finish_recording(failed=self.error is not None)
            cap.release()

    def finish_recording(self, failed=False):
        if self.recorder is None:
            return
        try:
            self.recorder.finish(failed)
        except OSError as error:
            print(f'Could not finish camera recording: {error}', flush=True)
        finally:
            self.recorder = None

    def snapshot(self):
        with self.lock:
            return list(self.frames)


def load_manifest(config, config_path):
    folder = config_path.parent / config['plans_dir']
    manifest = json.loads((folder/'manifest.json').read_text())
    if manifest['mode'] != 'simulation' or manifest['serial_enabled'] is not False:
        raise ValueError('Only simulation plans are accepted')
    if manifest['config_sha256'] != hashlib.sha256(config_path.read_bytes()).hexdigest():
        raise ValueError('Config changed: rebuild the checked plans first')
    required_start = approved_start()
    profile_path = config_path.parent / config['motion']['profile']
    model_start = joint_vector(json.loads(profile_path.read_text())['start_rad'])
    if manifest.get('required_start') != required_start or manifest.get('simulation_start_rad') != model_start:
        raise ValueError('Standby reference or simulation start changed: rebuild the checked plans first')
    for item in manifest['plans'].values():
        path = folder / item['file']
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('Plan checksum mismatch: '+path.name)
        check_start_frames(data, path.name, model_start, required_start['stable_s'])
    return folder, manifest


def run(args):
    config_path = args.config.resolve()
    config = json.loads(config_path.read_text())
    if config['mode'] != 'dry-run':
        raise ValueError('Physical motor output is not implemented in this director')
    folder, manifest = load_manifest(config, config_path)
    source = args.video
    if not source:
        cameras = sorted(glob.glob('/dev/v4l/by-id/*C60E*video-index0'))
        source = args.camera or (cameras[0] if cameras else None)
    if source is None:
        raise RuntimeError('No C60E detected; use --camera or --video')
    args.output.mkdir(parents=True, exist_ok=True)
    session = args.output / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    session.mkdir()
    detector = pose_detector = capture = None
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    director = Director(confirmations=config['confirmations'], cooldown_s=config['cooldown_s'],
                        max_age_s=config['max_observation_age_s'])
    vlm = VLM(config)
    started, counter, last_stamp = time.monotonic(), 0, None
    meta = {'mode': 'dry-run', 'serial_enabled': False, 'source': source, 'session': str(session),
            'config_sha256': manifest['config_sha256'], 'model': config['model'],
            'detector': 'CIX NPU YOLOv8n', 'saved_image_limit': 120,
            'required_start': manifest['required_start'],
            'code_sha256': {name: hashlib.sha256((HERE/name).read_bytes()).hexdigest()
                            for name in ('logic.py', 'run.py', 'recording.py', 'standby.py', 'vision.py', 'framing.py')}}
    if args.pose_model:
        meta['pose_model'] = str(args.pose_model.expanduser().resolve())
    atomic_json(session/'session.json', meta)
    print(json.dumps(meta, ensure_ascii=False), flush=True)
    try:
        detector = PersonDetector(config)
        if args.pose_model:
            pose_detector = PoseDetector(args.pose_model)
        recorder = None if args.video else CaptureRecorder(args.output, session)
        capture = Capture(source, config, bool(args.video), recorder)
        capture.thread.start()
        handler = RotatingFileHandler(session/'decisions.jsonl', maxBytes=4*1024**2, backupCount=2)
        logger = logging.getLogger(str(session))
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(handler)
        try:
            while not stop.is_set():
                if args.duration and time.monotonic()-started >= args.duration:
                    break
                samples = capture.snapshot()
                if capture.error:
                    event = dict(action='Hold', status='source_error', reason=capture.error,
                                 mode='dry-run', serial_enabled=False, at_utc=datetime.now(timezone.utc).isoformat())
                    logger.info(json.dumps(event, ensure_ascii=False))
                    atomic_json(args.output/'latest.json', event)
                    if args.video and capture.error == '视频源结束':
                        break
                    raise RuntimeError(capture.error)
                if time.monotonic()-(samples[-1][0] if samples else started) > config['max_observation_age_s']:
                    raise RuntimeError('摄像头画面超时，保持并停止判断')
                if shutil.disk_usage(session).free < 2*1024**3:
                    raise RuntimeError('可用磁盘不足2GiB，停止保存判断')
                if len(samples) < 3 or samples[-1][0] == last_stamp:
                    stop.wait(.1)
                    continue
                last_stamp = samples[-1][0]
                frames = [frame for _, frame in samples]
                audit, encoded, people, framing = {}, None, None, None
                framing_blocked = False
                try:
                    people = detector.detect(frames[-1])
                    if pose_detector is not None:
                        try:
                            poses, latency = pose_detector.detect(frames[-1])
                            framing = assess_framing(people, poses, frames[-1].shape)
                            framing['inference_ms'] = latency
                        except Exception as error:
                            framing = {'status': 'blocked', 'people': [], 'error': str(error)}
                    framing_blocked = framing is not None and (framing['status'] not in ('ready', 'empty')
                                                                or len(framing['people']) != len(people))
                    if framing_blocked:
                        audit = {'framing': framing['status']}
                        encoded = contact_sheet(frames)
                    else:
                        first_people = detector.detect(frames[0])
                        candidates = movement_candidates(first_people, people, frames[0].shape, frames[-1].shape)
                        audit, encoded = vlm.classify(frames, len(people), candidates)
                    result = director.decide(audit.get('observation', {}), time.monotonic(), last_stamp,
                                             len(people) if people is not None else None)
                    if framing_blocked:
                        result['reason'] = '上半身构图未确认'
                except Exception as error:
                    audit['error'] = f'{type(error).__name__}: {error}'
                    result = director.decide({}, time.monotonic(), last_stamp)
                counter += 1
                event = {'sequence': counter, 'session': str(session), 'at_utc': datetime.now(timezone.utc).isoformat(),
                         'elapsed_s': time.monotonic()-started, 'mode': 'dry-run', 'serial_enabled': False,
                         'source_kind': 'video' if args.video else 'camera', 'captured_monotonic_s': last_stamp,
                         'capture_age_s': time.monotonic()-last_stamp,
                         'frame_times_s': [stamp-started for stamp, _ in samples],
                         'decision': result, 'vision': audit, 'people': people}
                if framing is not None:
                    event['framing'] = framing
                if encoded:
                    name = f'observation-{counter:05d}.jpg'
                    (session/name).write_bytes(encoded)
                    event['image'] = name
                    if counter > 120:
                        (session/f'observation-{counter-120:05d}.jpg').unlink(missing_ok=True)
                if result['status'] == 'ready' and result['action'] != 'Hold':
                    plan = manifest['plans'][result['action']]
                    path = folder/plan['file']
                    if hashlib.sha256(path.read_bytes()).hexdigest() != plan['sha256']:
                        raise ValueError('Plan changed after startup')
                    event['proposal'] = dict(plan, path=str(path), requires_field_calibration=True,
                                             required_start=manifest['required_start'])
                    director.started(time.monotonic(), plan['duration_s'])
                logger.info(json.dumps(event, ensure_ascii=False, allow_nan=False))
                atomic_json(args.output/'latest.json', event)
                print(json.dumps({'sequence': counter, **result, 'latency_s': audit.get('latency_s'),
                                  'error': audit.get('error')}, ensure_ascii=False), flush=True)
                if args.max_decisions and counter >= args.max_decisions:
                    break
                stop.wait(config['decision_interval_s'])
        finally:
            handler.close()
            logger.removeHandler(handler)
    finally:
        if capture:
            capture.stop.set()
            capture.thread.join(timeout=3)
        if detector:
            detector.close()
        if pose_detector:
            pose_detector.close()
        finished = {'status': 'stopped', 'mode': 'dry-run', 'decisions': counter, 'serial_enabled': False,
                    'session': str(session), 'elapsed_s': time.monotonic()-started,
                    'stopped_at_utc': datetime.now(timezone.utc).isoformat()}
        atomic_json(session/'finished.json', finished)
        atomic_json(args.output/'latest.json', finished)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=HERE/'config.json')
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument('--camera')
    sources.add_argument('--video')
    parser.add_argument('--output', type=Path, default=HERE/'runs')
    parser.add_argument('--pose-model', type=Path, help='Enable upper-body checks with a CIX YOLOv8s-Pose model')
    parser.add_argument('--duration', type=float, default=0)
    parser.add_argument('--max-decisions', type=int, default=0)
    args = parser.parse_args()
    if args.duration < 0 or args.max_decisions < 0:
        parser.error('duration and max-decisions must be nonnegative')
    run(args)
