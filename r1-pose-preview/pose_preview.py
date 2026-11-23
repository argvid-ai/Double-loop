#!/usr/bin/env python3
"""O6N YOLOv8s-Pose preview. The only network service is loopback HTTP."""

import argparse
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import signal
import threading
import time

import cv2
import numpy as np
from NOE_Engine import EngineInfer


HERE = Path(__file__).resolve().parent
CAMERA_SIZE = (3840, 2160)
MODEL_SIZE = 640
LEFT_SHOULDER, RIGHT_SHOULDER, RIGHT_ELBOW, RIGHT_WRIST = 5, 6, 8, 10
LEFT_ELBOW, LEFT_WRIST = 7, 9
ARM_POINTS = (LEFT_SHOULDER, RIGHT_SHOULDER, RIGHT_ELBOW, RIGHT_WRIST)
KEYPOINT_CONFIDENCE = 0.45
BOX_CONFIDENCE = 0.40
DEAD_ZONE = 0.04  # shoulder widths
ZERO_DELAY = 8  # seconds for the operator to walk into frame
ZERO_WINDOW = 15  # seconds to acquire a stable pose after the countdown


@dataclass
class Person:
    box: tuple[float, float, float, float]
    points: np.ndarray
    confidence: float


def right_hand_signal(person):
    left, right, left_wrist, right_wrist = person.points[[LEFT_SHOULDER, RIGHT_SHOULDER, LEFT_WRIST, RIGHT_WRIST]]
    left_hand = left_wrist if left_wrist[2] >= 0.50 else person.points[LEFT_ELBOW]
    points = np.stack([left, right, left_hand, right_wrist])
    if not np.isfinite(points).all() or np.min(points[:, 2]) < 0.50:
        return False
    shoulder_width = left[0] - right[0]
    return (shoulder_width > 20 and right_wrist[1] < right[1] - 0.16 * shoulder_width
            and left_hand[1] > left[1] + 0.12 * shoulder_width)


def box_distance(first, second):
    x1, y1, x2, y2 = first
    a1, b1, a2, b2 = second
    center_gap = np.hypot(x1 + x2 - a1 - a2, y1 + y2 - b1 - b2) / 2
    size_ratio = ((a2 - a1) * (b2 - b1)) / ((x2 - x1) * (y2 - y1))
    return float(center_gap), float(size_ratio)


def shoulder_signature(person, width, height):
    left, right = person.points[[LEFT_SHOULDER, RIGHT_SHOULDER]]
    if not np.isfinite([left, right]).all() or min(left[2], right[2]) < KEYPOINT_CONFIDENCE:
        return None
    separation = float(np.linalg.norm(left[:2] - right[:2]))
    if separation < width * 0.025:
        return None
    x, y = (left[:2] + right[:2]) / 2
    if not 0 <= x < width or not 0 <= y < height:
        return None
    return float(x / width), float(y / height), separation / width


class OperatorLock:
    def __init__(self):
        self.request = None
        self.candidate = None
        self.consecutive = 0
        self.box = None
        self.shoulders = None
        self.last_seen = 0.0

    def select(self, people, width, height, now, request, select_visible=False):
        if request != self.request:
            self.request = request
            self.candidate = self.box = None
            self.shoulders = None
            self.consecutive = 0
        if request is None or (self.box is None and now - request > 30):
            return (people[0] if len(people) == 1 else None), None, None

        boxes = [tuple(value / size for value, size in zip(person.box, (width, height, width, height)))
                 for person in people]
        shoulders = [shoulder_signature(person, width, height) for person in people]
        if self.box is None:
            if select_visible:
                signaled = [0] if len(people) == 1 and people[0].confidence >= 0.65 else []
            else:
                signaled = [index for index, person in enumerate(people) if right_hand_signal(person)]
            if len(signaled) != 1:
                self.candidate = None
                self.consecutive = 0
                return None, None, 'lock_waiting'
            index = signaled[0]
            current = boxes[index]
            if self.candidate is None or box_distance(self.candidate, current)[0] > 0.08:
                self.consecutive = 1
            else:
                self.consecutive += 1
            self.candidate = current
            if self.consecutive < 4:
                return None, None, 'lock_waiting'
            self.box, self.last_seen = current, now
            self.shoulders = shoulders[index]
            return people[index], request, None

        # Keep the raise-right-hand identity. A distant or substantially smaller box
        # cannot inherit it when the operator walks in front of the mirror.
        age = now - self.last_seen
        matches = []
        for index, current in enumerate(boxes):
            signature = shoulders[index]
            if self.shoulders is not None and signature is not None:
                x, y, scale = self.shoulders
                new_x, new_y, new_scale = signature
                gap = math.hypot(new_x - x, new_y - y)
                ratio = new_scale / scale
                allowed_gap = 0.10 if age < 0.45 else 0.06
            else:
                gap, ratio = box_distance(self.box, current)
                allowed_gap = 0.085 if age < 0.45 else 0.055
            if gap <= allowed_gap and 0.70 <= ratio <= 1.42:
                score = gap + 0.16 * abs(math.log(ratio))
                matches.append((score, index))
        matches.sort()
        if not matches or (len(matches) > 1 and matches[1][0] - matches[0][0] < 0.038):
            return None, request, 'operator_lost'
        index = matches[0][1]
        self.box, self.last_seen = boxes[index], now
        self.shoulders = shoulders[index]
        return people[index], request, None


def detect_pose(model, frame):
    height, width = frame.shape[:2]
    scale = MODEL_SIZE / max(width, height)
    resized_w, resized_h = round(width * scale), round(height * scale)
    left, top = (MODEL_SIZE - resized_w) // 2, (MODEL_SIZE - resized_h) // 2
    letterbox = np.full((MODEL_SIZE, MODEL_SIZE, 3), 114, dtype=np.uint8)
    letterbox[top:top + resized_h, left:left + resized_w] = cv2.resize(
        frame, (resized_w, resized_h), interpolation=cv2.INTER_LINEAR
    )
    data = np.ascontiguousarray(letterbox[:, :, ::-1].transpose(2, 0, 1)[None], dtype=np.float32)
    data /= 255.0
    started = time.monotonic()
    prediction = model.forward([data])[0].reshape((56, 8400)).T
    inference_ms = (time.monotonic() - started) * 1000

    candidates = prediction[prediction[:, 4] >= BOX_CONFIDENCE]
    if len(candidates) == 0:
        return [], inference_ms
    boxes = candidates[:, :4].copy()
    boxes[:, :2] -= boxes[:, 2:] / 2
    indices = cv2.dnn.NMSBoxes(boxes.tolist(), candidates[:, 4].tolist(), BOX_CONFIDENCE, 0.55)
    people = []
    for index in np.asarray(indices).reshape(-1):
        row = candidates[index]
        points = row[5:].reshape((17, 3)).copy()
        points[:, :2] = (points[:, :2] - (left, top)) / scale
        x, y, box_width, box_height = boxes[index]
        bounds = ((x - left) / scale, (y - top) / scale,
                  (x + box_width - left) / scale, (y + box_height - top) / scale)
        bounds = tuple(float(np.clip(value, 0, limit - 1))
                       for value, limit in zip(bounds, (width, height, width, height)))
        if bounds[2] > bounds[0] and bounds[3] > bounds[1]:
            people.append(Person(bounds, points, float(row[4])))
    return people, inference_ms


def wrist_position(points, frame_shape):
    height, width = frame_shape[:2]
    joints = points[list(ARM_POINTS)]
    if not np.isfinite(joints).all() or np.any(joints[:, 2] < KEYPOINT_CONFIDENCE):
        return None
    if np.any(joints[:, :2] < 2) or np.any(joints[:, :2] >= (width - 2, height - 2)):
        return None
    left, right, _, wrist = joints[:, :2]
    shoulder_width = np.linalg.norm(left - right)
    if shoulder_width < width * 0.025 or left[0] <= right[0]:
        return None
    # A front-facing operator's right is the raw camera image's left.
    return ((left + right) / 2 - wrist) / shoulder_width


class WristTargets:
    def __init__(self):
        self.neutral = None
        self.filtered = None
        self.samples = deque(maxlen=6)
        self.zero_request = None
        self.zero_consumed = False
        self.last_valid = None

    def update(self, position, now, zero_request, reason):
        if zero_request != self.zero_request:
            self.zero_request = zero_request
            self.zero_consumed = False
            self.neutral = self.filtered = None
            self.samples.clear()
        pending = (zero_request is not None and not self.zero_consumed
                   and now - zero_request < ZERO_DELAY + ZERO_WINDOW)
        if pending and now - zero_request < ZERO_DELAY:
            return 'zero_countdown', None
        if position is None:
            self.filtered = None
            self.samples.clear()
            if reason == 'multiple' or self.last_valid is None or now - self.last_valid > 2:
                self.neutral = None
            return ('zero_waiting' if pending else reason), None

        self.last_valid = now
        if self.neutral is None:
            if not pending:
                return 'needs_zero', None
            if position[1] > -0.55:
                self.samples.clear()
                return 'zero_lower_hand', None
            self.samples.append(position)
            if len(self.samples) < self.samples.maxlen or np.max(np.ptp(self.samples, axis=0)) > 0.12:
                return 'zeroing', None
            self.neutral = np.median(self.samples, axis=0)
            self.zero_consumed = True
            self.samples.clear()
            return 'tracking', [0, 0]

        delta = position - self.neutral
        if self.filtered is None:
            self.filtered = delta
        else:
            self.filtered = 0.45 * delta + 0.55 * self.filtered
        target = np.sign(self.filtered) * np.maximum(np.abs(self.filtered) - DEAD_ZONE, 0)
        return 'tracking', np.rint(np.clip(target, -1, 1) * 100).astype(int).tolist()


def render_frame(frame, people, selected=None):
    height, width = frame.shape[:2]
    view_width = 960
    scale = view_width / width
    view = cv2.flip(cv2.resize(frame, (view_width, round(height * scale))), 1)
    for person in people:
        color = (103, 240, 94) if person is selected else (115, 195, 242)
        x1, y1, x2, y2 = person.box
        left, top = view_width - 1 - round(x2 * scale), round(y1 * scale)
        right, bottom = view_width - 1 - round(x1 * scale), round(y2 * scale)
        cv2.rectangle(view, (left, top), (right, bottom), color, 2, cv2.LINE_AA)
        label = 'TARGET' if person is selected else 'PERSON'
        cv2.putText(view, f'{label} {person.confidence:.2f}', (left + 4, min(bottom, top + 20)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.49, color, 2, cv2.LINE_AA)
    if selected is None:
        return view
    points = selected.points
    markers = {}
    for index in ARM_POINTS:
        x, y, confidence = points[index]
        if confidence >= KEYPOINT_CONFIDENCE and 0 <= x < width and 0 <= y < height:
            markers[index] = (view_width - 1 - round(x * scale), round(y * scale))
    for a, b in ((LEFT_SHOULDER, RIGHT_SHOULDER), (RIGHT_SHOULDER, RIGHT_ELBOW),
                 (RIGHT_ELBOW, RIGHT_WRIST)):
        if a in markers and b in markers:
            cv2.line(view, markers[a], markers[b], (255, 223, 64), 3, cv2.LINE_AA)
    for index, name in ((LEFT_SHOULDER, 'L shoulder'), (RIGHT_SHOULDER, 'R shoulder'),
                        (RIGHT_ELBOW, 'R elbow'), (RIGHT_WRIST, 'R wrist')):
        if index in markers:
            x, y = markers[index]
            cv2.circle(view, (x, y), 6, (255, 223, 64), -1, cv2.LINE_AA)
            cv2.putText(view, name, (min(x + 9, view_width - 110), max(15, y - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 255, 255), 3, cv2.LINE_AA)
            cv2.putText(view, name, (min(x + 9, view_width - 110), max(15, y - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.48, (34, 30, 17), 1, cv2.LINE_AA)
    return view


class Preview:
    def __init__(self):
        self.condition = threading.Condition()
        self.sequence = 0
        self.jpeg = b''
        self.status = {'state': 'starting', 'target': None}
        self.updated = time.monotonic()
        self.zero_request = None
        self.operator_request = None
        self.select_visible_operator = False

    def publish(self, jpeg, status):
        with self.condition:
            if jpeg:
                self.sequence += 1
                self.jpeg = jpeg
                self.condition.notify_all()
            self.status = status
            self.updated = time.monotonic()

    def read_status(self):
        with self.condition:
            if time.monotonic() - self.updated > 2:
                return {'state': 'stale', 'target': None}
            return self.status

    def request_zero(self):
        with self.condition:
            self.zero_request = time.monotonic()
            return self.zero_request

    def read_zero_request(self):
        with self.condition:
            return self.zero_request

    def request_operator(self, select_visible):
        with self.condition:
            self.operator_request = time.monotonic()
            self.select_visible_operator = select_visible
            self.zero_request = None
            return self.operator_request

    def read_operator_request(self):
        with self.condition:
            return self.operator_request, self.select_visible_operator


class PreviewServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port, preview, stopping):
        self.preview = preview
        self.stopping = stopping
        self.page = (HERE / 'preview.html').read_bytes()
        super().__init__(('127.0.0.1', port), PreviewHandler)


class PreviewHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_content(self, media_type, content):
        self.send_response(200)
        self.send_header('Content-Type', media_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        preview = self.server.preview
        if self.path == '/':
            self.send_content('text/html; charset=utf-8', self.server.page)
        elif self.path == '/status':
            self.send_content('application/json', json.dumps(preview.read_status()).encode())
        elif self.path == '/frame.jpg':
            with preview.condition:
                jpeg = preview.jpeg
            if jpeg:
                self.send_content('image/jpeg', jpeg)
            else:
                self.send_error(503, 'Waiting for a frame')
        elif self.path == '/stream':
            self.stream(preview)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.headers.get('X-Pose-Preview') != '1':
            self.send_error(404)
            return
        if self.path == '/zero':
            payload = {'zero_request': self.server.preview.request_zero()}
        elif self.path in ('/lock', '/lock-visible'):
            payload = {'operator_id': self.server.preview.request_operator(self.path == '/lock-visible')}
        else:
            self.send_error(404)
            return
        self.send_content('application/json', json.dumps(payload).encode())

    def stream(self, preview):
        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=poseframe')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        sequence = 0
        self.connection.settimeout(3)
        try:
            while not self.server.stopping.is_set():
                with preview.condition:
                    if not preview.condition.wait_for(lambda: preview.sequence != sequence, timeout=1):
                        continue
                    sequence, jpeg = preview.sequence, preview.jpeg
                header = f'--poseframe\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n'
                self.wfile.write(header.encode() + jpeg + b'\r\n')
        except (ConnectionError, TimeoutError):
            return


def open_camera():
    from calib_axes import open_cam

    os.environ['R1CAM_W'], os.environ['R1CAM_H'] = map(str, CAMERA_SIZE)
    capture = open_cam()
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError('Could not open the C60E through the original capture path')
    return capture


def run_camera(model, preview, stopping):
    capture = open_camera()
    wrist = WristTargets()
    operator = OperatorLock()
    timestamps = deque(maxlen=25)
    frame_count = 0
    errors = 0
    try:
        while not stopping.is_set():
            ok, frame = capture.read()
            now = time.monotonic()
            if not ok:
                errors += 1
                wrist.update(None, now, preview.read_zero_request(), 'camera_error')
                preview.publish(None, {'state': 'camera_error', 'target': None})
                if stopping.wait(0.2) or errors >= 10:
                    break
                continue
            errors = 0
            height, width = frame.shape[:2]
            if (width, height) != CAMERA_SIZE:
                raise RuntimeError(f'C60E captured {width}x{height}, expected 3840x2160')
            if frame_count == 0:
                print(f'C60E capture {width}x{height} MJPEG', flush=True)

            people, inference_ms = detect_pose(model, frame)
            request, select_visible = preview.read_operator_request()
            person, operator_id, lock_state = operator.select(people, width, height, now, request, select_visible)
            points = person.points if person is not None else None
            shoulders = points[[LEFT_SHOULDER, RIGHT_SHOULDER]] if points is not None else None
            shoulder_y = None
            if shoulders is not None and np.isfinite(shoulders).all() and np.min(shoulders[:, 2]) >= KEYPOINT_CONFIDENCE:
                center = float(np.mean(shoulders[:, 1])) / height
                if 0 <= center <= 1:
                    shoulder_y = round(center, 4)
            position = wrist_position(points, frame.shape) if points is not None else None
            reason = lock_state or ('weak_points' if person is not None else 'multiple' if people else 'no_person')
            zero_request = preview.read_zero_request()
            state, target = wrist.update(position, now, zero_request, reason)
            view = render_frame(frame, people, person)
            encoded, jpeg = cv2.imencode('.jpg', view, [cv2.IMWRITE_JPEG_QUALITY, 77])
            timestamps.append(time.monotonic())
            fps = (len(timestamps) - 1) / (timestamps[-1] - timestamps[0]) if len(timestamps) > 1 else 0
            frame_count += 1
            preview.publish(jpeg.tobytes() if encoded else None, {
                'state': state, 'target': target, 'people': len(people), 'frame': frame_count,
                'operator_id': operator_id,
                'person_box': [round(value / size, 4) for value, size in zip(
                    person.box, (width, height, width, height))] if person is not None else None,
                'box_confidence': round(person.confidence, 3) if person is not None else None,
                'shoulder_y': shoulder_y,
                'raw_wrist_y': round(float(position[1]), 3) if position is not None else None,
                'zero_request': zero_request,
                'countdown': math.ceil(ZERO_DELAY - max(0, now - zero_request))
                if state == 'zero_countdown' else None,
                'fps': round(fps, 1), 'npu_ms': round(inference_ms, 1),
                'process_ms': round((timestamps[-1] - now) * 1000, 1),
                'point_confidence': [round(float(points[i, 2]), 2) for i in ARM_POINTS]
                if points is not None else None,
            })
        if errors >= 10:
            raise RuntimeError('C60E stopped delivering frames')
    finally:
        capture.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, default=HERE.parent / 'models/yolov8s-pose.cix')
    parser.add_argument('--port', type=int, default=8796)
    parser.add_argument('--image', type=Path, help='Run a single NPU inference without opening the camera')
    args = parser.parse_args()
    cv2.setNumThreads(2)
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    model = EngineInfer(str(args.model))
    try:
        if args.image:
            frame = cv2.imread(str(args.image))
            if frame is None:
                parser.error(f'Cannot read {args.image}')
            people, milliseconds = detect_pose(model, frame)
            print(json.dumps({'npu_ms': round(milliseconds, 1), 'people': len(people), 'detections': [
                {'box': [round(value, 1) for value in person.box],
                 'right_arm': person.points[list(ARM_POINTS)].round(2).tolist()}
                for person in people
            ]}), flush=True)
            return

        preview = Preview()
        with PreviewServer(args.port, preview, stopping) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            print(f'Preview listening on 127.0.0.1:{args.port}', flush=True)
            try:
                run_camera(model, preview, stopping)
            finally:
                stopping.set()
                server.shutdown()
                thread.join()
    finally:
        model.clean()


if __name__ == '__main__':
    main()
