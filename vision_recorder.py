#!/usr/bin/env python3
"""Record a clip when a person enters the camera's detection zone."""

import argparse
from collections import deque
from contextlib import redirect_stdout
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sys
import time

import cv2


def zone_arg(value):
    try:
        x1, y1, x2, y2 = map(float, value.split(','))
    except ValueError as error:
        raise argparse.ArgumentTypeError('区域格式为 x1,y1,x2,y2') from error
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        raise argparse.ArgumentTypeError('区域坐标须从左上到右下，且在 0–1 内')
    return x1, y1, x2, y2


def people_in_zone(detections, shape, zone):
    height, width = shape[:2]
    left, top, right, bottom = zone
    people = []
    for item in detections:
        if item['label'] != 'person':
            continue
        x1, y1, x2, y2 = item['xyxy']
        foot_x, foot_y = (x1 + x2) / (2 * width), y2 / height
        if left <= foot_x <= right and top <= foot_y <= bottom:
            people.append(((x1 + x2) / (2 * width), (y1 + y2) / (2 * height),
                           (x2 - x1) * (y2 - y1)))
    return people


class Framing:
    def __init__(self, crop):
        self.crop = crop
        self.center = (0.5, 0.5)
        self.subject = None
        self.last_seen = None

    def render(self, frame, people, now, dt):
        if self.last_seen is not None and now - self.last_seen > 0.6:
            self.subject = None
        if people:
            if self.subject is not None:
                nearest = min(people, key=lambda person: math.dist(person[:2], self.subject))
                if math.dist(nearest[:2], self.subject) <= 0.15:
                    self.subject = nearest[:2]
                    self.last_seen = now
            if self.subject is None:
                self.subject = max(people, key=lambda person: person[2])[:2]
                self.last_seen = now
        if self.crop == 1:
            return frame

        height, width = frame.shape[:2]
        crop_w = max(2, int(width * self.crop) // 2 * 2)
        crop_h = max(2, int(height * self.crop) // 2 * 2)
        target = self.subject or self.center
        alpha = 1 - math.exp(-dt / 0.55)
        step = 0.18 * dt
        center = []
        for current, desired, extent in zip(self.center, target, (crop_w / width, crop_h / height)):
            move = max(-step, min(step, (desired - current) * alpha))
            center.append(max(extent / 2, min(1 - extent / 2, current + move)))
        self.center = tuple(center)
        x = round(self.center[0] * width - crop_w / 2)
        y = round(self.center[1] * height - crop_h / 2)
        return cv2.resize(frame[y:y + crop_h, x:x + crop_w], (width, height))


class Clip:
    def __init__(self, directory, fps, first_time, triggered, shape):
        height, width = shape[:2]
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        self.path = directory / f'person_{stamp}.mp4'
        self.partial = self.path.with_suffix('.part.mp4')
        self.writer = cv2.VideoWriter(str(self.partial), cv2.VideoWriter_fourcc(*'mp4v'),
                                      fps, (width, height))
        if not self.writer.isOpened():
            self.writer.release()
            self.partial.unlink(missing_ok=True)
            raise RuntimeError(f'无法创建 MP4：{self.path}')
        self.fps = fps
        self.first_time = first_time
        self.triggered = triggered
        self.frames = 0
        self.previous = None
        self.next_space_check = first_time

    def write(self, now, frame):
        if now >= self.next_space_check:
            self.next_space_check = now + 2
            if shutil.disk_usage(self.path.parent).free < 2 * 1024**3:
                raise RuntimeError('磁盘剩余不足 2 GiB，已停止录制')
        slot = round((now - self.first_time) * self.fps)
        if slot < self.frames:
            return
        while self.frames < slot:
            self.writer.write(self.previous)
            self.frames += 1
        self.writer.write(frame)
        self.frames += 1
        self.previous = frame

    def close(self, now, reason):
        self.writer.release()
        self.partial.replace(self.path)
        event = {'video': self.path.name, 'start_s': round(self.first_time, 3),
                 'trigger_s': round(self.triggered, 3), 'end_s': round(now, 3),
                 'reason': reason, 'frames': self.frames, 'fps': self.fps}
        self.path.with_suffix('.json').write_text(json.dumps(event, indent=2) + '\n')
        print(json.dumps(event), flush=True)


def record(args, capture, fps, detect):
    args.output.mkdir(parents=True, exist_ok=True)
    history = deque(maxlen=max(1, math.ceil((args.pre + args.enter) * fps) + 2))
    framing = Framing(args.crop)
    clip = None
    present_since = last_seen = None
    start = time.monotonic()
    now = previous_time = 0.0
    frame_number = 0
    reason = 'source_ended'
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                if args.camera is not None:
                    reason = 'camera_read_failed'
                    raise RuntimeError('摄像头取流中断')
                break
            now = frame_number / fps if args.video else time.monotonic() - start
            if frame_number and now - previous_time > 5:
                reason = 'input_gap'
                raise RuntimeError('连续 5 秒未取得下一帧')
            frame_number += 1
            people = people_in_zone(detect(frame), frame.shape, args.zone)
            frame = framing.render(frame, people, now, max(0, min(now - previous_time, 0.1)))
            previous_time = now
            if people:
                present_since = now if present_since is None else present_since
                last_seen = now
            else:
                present_since = None

            history.append((now, frame))
            while history and history[0][0] < now - args.pre - args.enter:
                history.popleft()
            if clip is None and present_since is not None and now - present_since >= args.enter:
                clip = Clip(args.output, fps, history[0][0], now, frame.shape)
                for timestamp, buffered in history:
                    clip.write(timestamp, buffered)
            elif clip is not None:
                clip.write(now, frame)
                if not people and now - last_seen >= args.post:
                    clip.close(now, 'person_left')
                    clip = None
    except KeyboardInterrupt:
        reason = 'stopped'
    except Exception:
        if reason == 'source_ended':
            reason = 'error'
        raise
    finally:
        if clip is not None and clip.frames:
            clip.close(now, reason)
        elif clip is not None:
            clip.writer.release()
            clip.partial.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--video', type=Path)
    source.add_argument('--camera', help='V4L2 设备路径，例如 /dev/v4l/by-id/...-video-index0')
    parser.add_argument('--yolo-dir', type=Path, default=Path.home() / 'yolo_test/p1-yolo-test')
    parser.add_argument('--output', type=Path, default=Path('recordings'))
    parser.add_argument('--zone', type=zone_arg, default=(0, 0, 1, 1))
    parser.add_argument('--enter', type=float, default=0.35, help='确认进入的秒数')
    parser.add_argument('--pre', type=float, default=2, help='确认进入前额外保留的秒数')
    parser.add_argument('--post', type=float, default=3, help='离开后继续录制的秒数')
    parser.add_argument('--crop', type=float, default=0.86, help='保留画面比例，1 表示全幅')
    parser.add_argument('--confidence', type=float, default=0.4)
    args = parser.parse_args()
    if not (0 <= args.enter <= 3 and 0 <= args.pre <= 5 and 0 <= args.post <= 30
            and 0.7 <= args.crop <= 1 and 0 <= args.confidence <= 1):
        parser.error('录制时长或比例超出允许范围')

    sys.path.insert(0, str(args.yolo_dir.resolve()))
    from benchmark_video import prepare
    from run_yolo import make_engine, postprocess

    capture = cv2.VideoCapture(str(args.video)) if args.video else cv2.VideoCapture(args.camera, cv2.CAP_V4L2)
    cleanup = None
    try:
        if not capture.isOpened():
            raise RuntimeError('无法打开视频源')
        if args.camera:
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
            capture.set(cv2.CAP_PROP_FPS, 30)
        fps = capture.get(cv2.CAP_PROP_FPS)
        if not math.isfinite(fps) or not 0 < fps <= 120:
            raise RuntimeError('视频源没有报告有效帧率')
        forward, cleanup = make_engine('npu', args.yolo_dir / 'models/yolov8n.cix')

        with open(os.devnull, 'w') as quiet:
            def detect(frame):
                with redirect_stdout(quiet):
                    raw = forward(prepare(frame))
                return postprocess(raw, frame.shape, args.confidence)

            record(args, capture, fps, detect)
    finally:
        capture.release()
        if cleanup is not None:
            cleanup()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    main()
