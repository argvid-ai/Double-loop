#!/usr/bin/env python3
"""录像关键帧筛选：NPU 姿态模型找动作，本机 MiniCPM-V 确认动作/微笑。

只读取已录好的 MP4，不打开相机、不连接机械臂。

流程:
    1. 按固定步长抽帧，用 yolov8s-pose 关键点计算动作分和运动量；
    2. 挑出动作候选与「人脸可见」候选，数量受 --vlm-max 限制；
    3. 本机 MiniCPM-V 对候选帧输出两位数字（动作 / 微笑），
       模型服务不可用时退回纯姿态动作判定；
    4. 按动作、微笑、清晰度、构图排序，去重后保存关键帧，
       写出 manifest.json 与 contact.jpg。

运行环境:
    LD_LIBRARY_PATH=/usr/share/cix/lib \
    ~/yolo_test/p1-yolo-test/.venv/bin/python keyframes.py \
        --video captures/.../clips/group_001.mp4 --output captures/.../keyframes/group_001
"""

import argparse
import base64
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DIRECTOR_DIR = PROJECT_ROOT / "director"
if not DIRECTOR_DIR.is_dir():
    DIRECTOR_DIR = PROJECT_ROOT / "r1-roadshow" / "director"
sys.path.insert(0, str(DIRECTOR_DIR))

from vision import VLM  # noqa: E402

DEFAULT_CONFIG = DIRECTOR_DIR / "config.json"
MODEL_SIZE = 640
BOX_CONFIDENCE = 0.40
KEYPOINT_CONFIDENCE = 0.45
DEFAULT_COUNT = 6
DEFAULT_STEP_S = 0.4
DEFAULT_MIN_GAP_S = 1.0
DEFAULT_ACTION_THRESHOLD = 0.5
DEFAULT_VLM_MAX = 12
DEFAULT_MIN_SHARPNESS = 100.0
DEFAULT_MAX_HAND_SPEED = 1.2
DEFAULT_JPEG_QUALITY = 95
DEFAULT_DUP_AHASH = 5
VLM_TIMEOUT_S = 15.0

NAME_TAGS = {
    "动作": "action", "微笑": "smile", "抬手": "raise", "展臂": "extend",
    "屈臂": "bend", "双手动作": "hands", "叉腰": "hips", "抬腿": "leg",
    "开立": "stance", "倾斜": "lean", "举手": "hand_up", "清晰": "sharp",
}

SMILE_PROMPT = (
    "只看这张人物上半身特写：图中人物是否正在微笑（嘴角上扬或露牙）？"
    "只回答：是 或 否。"
)

MODEL_CANDIDATES = (
    PROJECT_ROOT / "models" / "yolov8s-pose.cix",
    Path.home() / "r1-followcam" / "models" / "yolov8s-pose.cix",
    Path.home() / "r1-pose-preview" / "models" / "yolov8s-pose.cix",
)


def resolve_model(explicit=None) -> Path:
    """按显式路径 → 本工程 → 板上已有工程顺序找姿态模型。"""
    for candidate in ([Path(explicit).expanduser()] if explicit else []) + list(MODEL_CANDIDATES):
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "找不到 yolov8s-pose.cix，请用 --model 指定；板上默认位于 "
        "~/r1-followcam/models/yolov8s-pose.cix"
    )


def ahash(frame) -> int:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (8, 8), interpolation=cv2.INTER_AREA)
    mean = float(small.mean())
    value = 0
    for pixel in small.flatten():
        value = (value << 1) | (1 if pixel >= mean else 0)
    return value


def hash_distance(first: int, second: int) -> int:
    return bin(first ^ second).count("1")


def region_sharpness(frame, box) -> float:
    """人体区域清晰度：背景再锐利也不能掩盖人物运动模糊。"""
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = (int(round(value)) for value in box)
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(width, x2), min(height, y2)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return 0.0
    gray = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    if gray.shape[1] > 480:
        scale = 480 / gray.shape[1]
        gray = cv2.resize(gray, (480, max(1, round(gray.shape[0] * scale))),
                          interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def atomic_json(path: Path, value: dict) -> None:
    pending = path.with_name(path.name + ".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(pending, path)


def write_contact_sheet(out_dir: Path, images, tile_width: int = 480) -> None:
    tiles = []
    for item in images:
        image = cv2.imread(str(out_dir / item["file"]))
        if image is None:
            continue
        scale = tile_width / image.shape[1]
        image = cv2.resize(image, (tile_width, max(1, round(image.shape[0] * scale))))
        label = item["file"]
        cv2.putText(image, label, (6, min(22, image.shape[0] - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.putText(image, label, (6, min(22, image.shape[0] - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 200, 0), 1, cv2.LINE_AA)
        tiles.append(image)
    if not tiles:
        return
    # 单行排列，避免数量不足时出现黑色补位块。
    tile_height = max(tile.shape[0] for tile in tiles)
    tiles = [cv2.copyMakeBorder(tile, 0, tile_height - tile.shape[0], 0, 0,
                                cv2.BORDER_CONSTANT, value=(255, 255, 255))
             for tile in tiles]
    sheet = np.hstack(tiles)
    ok, encoded = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if ok:
        (out_dir / "contact.jpg").write_bytes(encoded.tobytes())


class Person:
    __slots__ = ("box", "points", "confidence")

    def __init__(self, box, points, confidence):
        self.box = box
        self.points = points
        self.confidence = confidence


class PoseDetector:
    """yolov8s-pose（CIX NPU）封装，输出人体框与 17 个关键点。"""

    def __init__(self, model_path: Path, box_confidence=BOX_CONFIDENCE,
                 keypoint_confidence=KEYPOINT_CONFIDENCE):
        from contextlib import redirect_stdout
        from NOE_Engine import EngineInfer
        self.box_confidence = box_confidence
        self.keypoint_confidence = keypoint_confidence
        self.quiet = open(os.devnull, "w")
        with redirect_stdout(self.quiet):
            self.engine = EngineInfer(str(model_path))

    def detect(self, frame):
        from contextlib import redirect_stdout
        height, width = frame.shape[:2]
        scale = MODEL_SIZE / max(width, height)
        resized_w, resized_h = round(width * scale), round(height * scale)
        left, top = (MODEL_SIZE - resized_w) // 2, (MODEL_SIZE - resized_h) // 2
        letterbox = np.full((MODEL_SIZE, MODEL_SIZE, 3), 114, dtype=np.uint8)
        letterbox[top:top + resized_h, left:left + resized_w] = cv2.resize(
            frame, (resized_w, resized_h), interpolation=cv2.INTER_LINEAR)
        data = np.ascontiguousarray(
            letterbox[:, :, ::-1].transpose(2, 0, 1)[None], dtype=np.float32) / 255.0
        with redirect_stdout(self.quiet):
            raw = self.engine.forward([data])[0]
        prediction = raw.reshape((56, 8400)).T
        candidates = prediction[prediction[:, 4] >= self.box_confidence]
        if len(candidates) == 0:
            return []
        boxes = candidates[:, :4].copy()
        boxes[:, :2] -= boxes[:, 2:] / 2
        indices = cv2.dnn.NMSBoxes(boxes.tolist(), candidates[:, 4].tolist(),
                                   self.box_confidence, 0.55)
        people = []
        for index in np.asarray(indices).reshape(-1):
            row = candidates[index]
            points = row[5:].reshape((17, 3)).copy()
            points[:, :2] = (points[:, :2] - (left, top)) / scale
            x, y, box_width, box_height = boxes[index]
            bounds = ((x - left) / scale, (y - top) / scale,
                      (x + box_width - left) / scale, (y + box_height - top) / scale)
            bounds = tuple(float(np.clip(value, 0, limit))
                           for value, limit in zip(bounds, (width, height, width, height)))
            if bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                people.append(Person(bounds, points, float(row[4])))
        return people

    def close(self):
        try:
            self.engine.clean()
        finally:
            self.quiet.close()


def _point(points, index, threshold=KEYPOINT_CONFIDENCE):
    x, y, confidence = points[index]
    if confidence < threshold or not np.isfinite(x) or not np.isfinite(y):
        return None
    return float(x), float(y)


def _angle(first, vertex, last):
    a = np.asarray(first) - np.asarray(vertex)
    b = np.asarray(last) - np.asarray(vertex)
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator < 1e-6:
        return 180.0
    return float(np.degrees(np.arccos(np.clip(np.dot(a, b) / denominator, -1.0, 1.0))))


def action_features(person, shape):
    """按关键点给出动作分和标签；自然站立接近 0。"""
    points = person.points
    x1, y1, x2, y2 = person.box
    left_shoulder = _point(points, 5)
    right_shoulder = _point(points, 6)
    if left_shoulder and right_shoulder:
        shoulder_width = math.dist(left_shoulder, right_shoulder)
    else:
        shoulder_width = max((x2 - x1) * 0.35, shape[1] * 0.04)
    if shoulder_width < 1e-3:
        return 0.0, []
    hip_left = _point(points, 11)
    hip_right = _point(points, 12)
    hip_y = ((hip_left[1] + hip_right[1]) / 2 if hip_left and hip_right
             else (left_shoulder[1] if left_shoulder else y1) + 1.2 * shoulder_width)
    nose = _point(points, 0)

    score, tags = 0.0, []

    def add(value, tag):
        nonlocal score
        score = max(score, value)
        if tag not in tags:
            tags.append(tag)

    for shoulder_index, elbow_index, wrist_index in ((5, 7, 9), (6, 8, 10)):
        shoulder = _point(points, shoulder_index)
        elbow = _point(points, elbow_index)
        wrist = _point(points, wrist_index)
        if not (shoulder and wrist):
            continue
        rise = (shoulder[1] - wrist[1]) / shoulder_width
        if rise > 0.35:
            add(min(1.0, 0.6 + 0.4 * rise), "抬手")
        lateral = abs(wrist[0] - shoulder[0]) / shoulder_width
        if lateral > 1.1 and wrist[1] < hip_y:
            add(min(1.0, 0.55 + lateral * 0.2), "展臂")
        if elbow:
            bend = _angle(shoulder, elbow, wrist)
            if bend < 100 and wrist[1] < shoulder[1] + 0.8 * shoulder_width:
                add(0.55 + (100 - bend) / 200, "屈臂")
        if nose and wrist[1] < nose[1] + 0.5 * shoulder_width \
                and abs(wrist[0] - nose[0]) < 1.3 * shoulder_width:
            add(0.8, "举手")

    left_wrist = _point(points, 9)
    right_wrist = _point(points, 10)
    if left_wrist and right_wrist:
        if math.dist(left_wrist, right_wrist) < 0.8 * shoulder_width \
                and max(left_wrist[1], right_wrist[1]) < hip_y - 0.5 * shoulder_width:
            add(0.6, "双手动作")
        if hip_left and hip_right:
            if (math.dist(left_wrist, hip_left) < 0.55 * shoulder_width
                    and math.dist(right_wrist, hip_right) < 0.55 * shoulder_width):
                add(0.46, "叉腰")

    left_ankle = _point(points, 15)
    right_ankle = _point(points, 16)
    if left_ankle and right_ankle:
        if abs(left_ankle[1] - right_ankle[1]) > 0.45 * shoulder_width:
            add(0.72, "抬腿")
        if abs(left_ankle[0] - right_ankle[0]) > 1.8 * shoulder_width:
            add(0.42, "开立")

    shoulder_center = None
    if left_shoulder and right_shoulder:
        shoulder_center = ((left_shoulder[0] + right_shoulder[0]) / 2,
                           (left_shoulder[1] + right_shoulder[1]) / 2)
    hip_center = None
    if hip_left and hip_right:
        hip_center = ((hip_left[0] + hip_right[0]) / 2,
                      (hip_left[1] + hip_right[1]) / 2)
    if shoulder_center and hip_center:
        lean = abs(math.degrees(math.atan2(shoulder_center[0] - hip_center[0],
                                           max(hip_center[1] - shoulder_center[1], 1e-3))))
        if lean > 18:
            add(min(0.7, 0.42 + lean / 120), "倾斜")

    return min(1.0, score), tags


def head_visible(person, shape):
    """鼻子/眼睛/耳朵至少两个可见且不出画，用来判断是否值得送 VLM 看表情。"""
    height, width = shape[:2]
    face = person.points[:5]
    visible = face[face[:, 2] >= KEYPOINT_CONFIDENCE]
    if len(visible) < 2:
        return False
    xs, ys = visible[:, 0], visible[:, 1]
    return bool(xs.min() > 2 and ys.min() > 2 and xs.max() < width - 2
                and ys.max() < height - 2)


def face_area(person):
    """可见面部关键点围成的面积，用来挑最值得看表情的人。"""
    face = person.points[:5]
    visible = face[face[:, 2] >= KEYPOINT_CONFIDENCE]
    if len(visible) < 2:
        return 0.0
    width = visible[:, 0].max() - visible[:, 0].min()
    height = visible[:, 1].max() - visible[:, 1].min()
    return float(max(width, 1.0) * max(height, 1.0))


def portrait_box(person, shape):
    """取人物头部到肩部的方形裁切，用来让 VLM 看清表情。"""
    points = person.points
    face = [point for point in (_point(points, index) for index in range(5)) if point]
    shoulders = [point for point in (_point(points, index) for index in (5, 6)) if point]
    if not face:
        return None
    height, width = shape[:2]
    fx = [point[0] for point in face]
    fy = [point[1] for point in face]
    face_width = max(max(fx) - min(fx), 24.0)
    face_height = max(max(fy) - min(fy), 24.0)
    if len(shoulders) == 2:
        shoulder_width = math.dist(shoulders[0], shoulders[1])
    else:
        shoulder_width = face_width * 2.5
    x1 = min(fx) - 0.9 * face_width
    x2 = max(fx) + 0.9 * face_width
    if shoulders:
        x1 = min(x1, min(point[0] for point in shoulders) - 0.2 * shoulder_width)
        x2 = max(x2, max(point[0] for point in shoulders) + 0.2 * shoulder_width)
    y1 = min(fy) - 0.8 * face_height
    y2 = (max(point[1] for point in shoulders) + 0.9 * shoulder_width
          if shoulders else max(fy) + 2.0 * face_height)
    x1, y1 = max(0, int(round(x1))), max(0, int(round(y1)))
    x2, y2 = min(width, int(round(x2))), min(height, int(round(y2)))
    if x2 - x1 < 32 or y2 - y1 < 32:
        return None
    return x1, y1, x2, y2


def composition_ok(people, shape, margin=0.005):
    """至少有一人头部完整且左右不出画；其他被裁的人物不拖累整帧。"""
    if not people:
        return False
    height, width = shape[:2]
    for person in people:
        x1, _, x2, _ = person.box
        if x1 > margin * width and x2 < (1 - margin) * width \
                and head_visible(person, shape):
            return True
    return False


def normalized_center(person, shape):
    x1, y1, x2, y2 = person.box
    height, width = shape[:2]
    return ((x1 + x2) / 2 / width, (y1 + y2) / 2 / height)


def _relative_hand_speed(previous, people, shape, dt):
    """手腕/肘相对肩部的速度（肩宽/秒），用来剔除挥手拖影帧。"""
    if not previous or not people or dt <= 0:
        return 0.0
    used, speeds = set(), []
    for person in people:
        center = normalized_center(person, shape)
        best, best_distance = None, 0.15
        for index, old in enumerate(previous):
            if index in used:
                continue
            old_center = normalized_center(old, shape)
            distance = math.hypot(center[0] - old_center[0], center[1] - old_center[1])
            if distance < best_distance:
                best, best_distance = index, distance
        if best is None:
            continue
        used.add(best)
        old = previous[best]
        left_shoulder, right_shoulder = _point(person.points, 5), _point(person.points, 6)
        old_left, old_right = _point(old.points, 5), _point(old.points, 6)
        if not (left_shoulder and right_shoulder and old_left and old_right):
            continue
        shoulder_width = math.dist(left_shoulder, right_shoulder)
        if shoulder_width < 1:
            continue
        shoulder = ((left_shoulder[0] + right_shoulder[0]) / 2,
                    (left_shoulder[1] + right_shoulder[1]) / 2)
        old_shoulder = ((old_left[0] + old_right[0]) / 2,
                        (old_left[1] + old_right[1]) / 2)
        for index in (7, 8, 9, 10):
            current = _point(person.points, index, 0.4)
            earlier = _point(old.points, index, 0.4)
            if current and earlier:
                delta = ((current[0] - shoulder[0]) - (earlier[0] - old_shoulder[0]),
                         (current[1] - shoulder[1]) - (earlier[1] - old_shoulder[1]))
                speeds.append(math.hypot(*delta) / shoulder_width / dt)
    return min(max(speeds, default=0.0), 10.0)


def _motion(previous, people, shape, dt):
    """与上一采样帧匹配后，用关键点速度和人体中心速度衡量运动量。"""
    if not previous or not people or dt <= 0:
        return 0.0
    height, width = shape[:2]
    used, joint_speeds, body_speeds = set(), [], []
    for person in people:
        center = normalized_center(person, shape)
        best, best_distance = None, 0.15
        for index, old in enumerate(previous):
            if index in used:
                continue
            old_center = normalized_center(old, shape)
            distance = math.hypot(center[0] - old_center[0], center[1] - old_center[1])
            if distance < best_distance:
                best, best_distance = index, distance
        if best is None:
            continue
        used.add(best)
        old = previous[best]
        shoulder = None
        for indices in ((5, 6), (11, 12)):
            first, second = _point(old.points, indices[0]), _point(old.points, indices[1])
            if first and second:
                shoulder = math.dist(first, second)
                break
        if not shoulder:
            old_height = max(old.box[3] - old.box[1], 1.0)
            shoulder = max(old_height * 0.22, 1.0)
        for index in (5, 6, 7, 8, 9, 10, 11, 12):
            current_point = _point(person.points, index, 0.4)
            old_point = _point(old.points, index, 0.4)
            if current_point and old_point:
                joint_speeds.append(math.dist(current_point, old_point) / shoulder / dt)
        body_height = max(person.box[3] - person.box[1], 1.0)
        body_speeds.append(math.dist(center,
                                     normalized_center(old, shape)) * height / body_height / dt)
    if not joint_speeds and not body_speeds:
        return 0.0
    joint_speed = float(np.mean(sorted(joint_speeds)[-3:])) if joint_speeds else 0.0
    body_speed = max(body_speeds) if body_speeds else 0.0
    return min(1.0, max(joint_speed / 1.0, body_speed / 0.45))


class HighlightJudge:
    """复用本机 MiniCPM-V 服务，只对人物特写判断微笑；动作交给姿态模型。"""

    def __init__(self, config):
        self.vlm = VLM({
            "endpoint": config.get("endpoint", ""),
            "model": config.get("model", "minicpm-v4.6-lq50"),
            "request_timeout_s": VLM_TIMEOUT_S,
        })
        self.available = True

    @staticmethod
    def _encode(frame, max_side=448):
        height, width = frame.shape[:2]
        scale = min(1.0, max_side / max(height, width))
        if scale < 1:
            frame = cv2.resize(frame, (round(width * scale), round(height * scale)),
                               interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode()

    def judge_smile(self, crop):
        """返回 1/0/None；None 表示服务不可用或回答无法解析。"""
        uri = self._encode(crop)
        if uri is None:
            return None
        content = [{"type": "text", "text": SMILE_PROMPT},
                   {"type": "image_url", "image_url": {"url": uri}}]
        try:
            reply, _ = self.vlm.request(content, 8)
            text = (reply["message"]["content"] or "").strip()
        except Exception as error:  # noqa: BLE001 - VLM 异常不能中断抽帧
            self.available = False
            return None
        if reply.get("finish_reason") != "stop":
            return None
        if re.search(r"不|否|没", text):
            return 0
        if re.search(r"是|有|微笑|露牙", text) or re.search(r"(?<!\d)1(?!\d)", text):
            return 1
        return None


def scan_video(video_path: Path, detector: PoseDetector, step_s: float):
    """按步长扫描视频，返回每帧的姿态、动作分和画面质量。"""
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise SystemExit(f"无法打开视频: {video_path}")
    fps = capture.get(cv2.CAP_PROP_FPS)
    if not math.isfinite(fps) or fps <= 0:
        fps = 30.0
    step = max(0.1, step_s)
    records, previous, previous_t = [], [], None
    index, next_sample = 0, 0.0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            t = index / fps
            if t + 1e-6 >= next_sample:
                people = detector.detect(frame)
                action, tags = 0.0, []
                for person in people:
                    value, person_tags = action_features(person, frame.shape)
                    if value > action:
                        action, tags = value, person_tags
                    elif value == action:
                        tags = list(dict.fromkeys(tags + person_tags))
                dt = t - previous_t if previous_t is not None else 0.0
                motion = _motion(previous, people, frame.shape, dt)
                hand_speed = _relative_hand_speed(previous, people, frame.shape, dt)
                main = max(people, key=lambda item: (item.box[2] - item.box[0])
                           * (item.box[3] - item.box[1])) if people else None
                faces_ok = [person for person in people if head_visible(person, frame.shape)]
                crop_person = max(faces_ok, key=face_area) if faces_ok else main
                portrait = portrait_box(crop_person, frame.shape) if crop_person else None
                records.append({
                    "index": index, "t": round(t, 3), "people": len(people),
                    "action": round(action, 3), "motion": round(motion, 3),
                    "hand_speed": round(hand_speed, 3),
                    "tags": tags,
                    "faces": any(head_visible(person, frame.shape) for person in people),
                    "head_ok": composition_ok(people, frame.shape),
                    "sharpness": round(region_sharpness(frame, main.box), 1) if main else 0.0,
                    "face_sharpness": round(region_sharpness(frame, portrait), 1)
                    if portrait else 0.0,
                    "portrait": portrait,
                    "boxes": [[round(float(v), 1) for v in person.box] for person in people],
                })
                previous, previous_t = people, t
                next_sample += step
            index += 1
    finally:
        capture.release()
    return records, index / fps


def select_candidates(records, action_threshold, vlm_max, min_gap, max_hand_speed):
    """只从手部相对静止的帧里挑候选，避免挥手拖影。"""
    still = [record for record in records if record["hand_speed"] <= max_hand_speed]
    pool = still if still else records
    actions = [record for record in pool
               if record["action"] >= action_threshold]
    faces = [record for record in pool
             if record["faces"] and record["head_ok"] and record["people"] >= 1]
    faces.sort(key=lambda record: record["sharpness"], reverse=True)
    base = []
    if vlm_max <= 0:
        return actions
    action_quota = max(1, (vlm_max + 1) // 2)
    for record in sorted(actions, key=lambda r: r["action"], reverse=True):
        if len(base) >= action_quota:
            break
        if all(abs(record["t"] - picked["t"]) >= min_gap for picked in base):
            base.append(record)
    for record in faces:
        if len(base) >= vlm_max:
            break
        if all(abs(record["t"] - picked["t"]) >= min_gap for picked in base):
            base.append(record)
    base.sort(key=lambda record: record["t"])
    return base


def _read_frames(video_path: Path, indices):
    wanted, frames = set(indices), {}
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return frames
    index = 0
    try:
        while wanted:
            ok, frame = capture.read()
            if not ok:
                break
            if index in wanted:
                frames[index] = frame
                wanted.discard(index)
            index += 1
    finally:
        capture.release()
    return frames


def choose_keyframes(candidates, frames, judge, count, min_gap, use_vlm,
                     min_sharpness, action_threshold, max_hand_speed):
    """动作由姿态模型判定，微笑由 VLM 看人物特写判定；拖影帧直接排除。"""
    kept, judged = [], []
    for record in candidates:
        frame = frames.get(record["index"])
        if frame is None:
            continue
        sharp_ok = record["sharpness"] >= min_sharpness
        still = record["hand_speed"] <= max_hand_speed
        usable = record["head_ok"] and sharp_ok and still
        crop = None
        if record.get("portrait") and usable:
            x1, y1, x2, y2 = record["portrait"]
            crop = frame[y1:y2, x1:x2]
        smile = None
        if (use_vlm and judge is not None and judge.available
                and crop is not None and crop.size > 0):
            smile = judge.judge_smile(crop)
        action = record["action"]
        action_ok = action >= action_threshold
        keep = bool(usable and (action_ok or smile == 1))
        tags = set(record["tags"]) if action_ok else set()
        if action_ok:
            tags.add("动作")
        if smile == 1:
            tags.add("微笑")
        judged.append({
            "t_s": record["t"], "action": record["action"],
            "motion": record["motion"], "hand_speed": record["hand_speed"],
            "tags": record["tags"],
            "sharpness": record["sharpness"],
            "face_sharpness": record.get("face_sharpness", 0.0),
            "smile": smile, "kept": keep,
        })
        if not keep:
            continue
        kept.append({
            "index": record["index"], "t": record["t"],
            "action": round(action, 3), "smile": smile == 1,
            "tags": sorted(tags), "people": record["people"],
            "sharpness": record["sharpness"],
            "face_sharpness": record.get("face_sharpness", 0.0),
            "boxes": record["boxes"],
            "smile_verdict": smile,
            "score": action * 0.6 + (0.4 if smile == 1 else 0.0)
            + min(record["sharpness"], 800) / 4000
            + min(record.get("face_sharpness", 0.0), 1500) / 7500,
        })
    kept.sort(key=lambda item: item["score"], reverse=True)
    chosen = []
    if count <= 0:
        return chosen, judged
    smile_item = next((item for item in kept if item["smile"]), None)
    if smile_item is not None:
        chosen.append(smile_item)
    for item in kept:
        if len(chosen) >= count:
            break
        if item in chosen:
            continue
        if all(abs(item["t"] - picked["t"]) >= min_gap for picked in chosen):
            chosen.append(item)
    return sorted(chosen, key=lambda item: item["t"]), judged


def quality_fallback(records, count, min_gap, min_sharpness, max_hand_speed):
    pool = [record for record in records
            if record["people"] >= 1 and record["head_ok"]
            and record["sharpness"] >= min_sharpness
            and record["hand_speed"] <= max_hand_speed]
    if not pool:
        pool = [record for record in records
                if record["people"] >= 1 and record["head_ok"]
                and record["sharpness"] >= min_sharpness]
    if not pool:
        pool = [record for record in records
                if record["people"] >= 1 and record["head_ok"]]
    if not pool:
        # 整段都没有头部完整的帧时，至少保留一张有人物的清晰帧，
        # 保证每段录像都有可展示的关键帧。
        pool = [record for record in records if record["people"] >= 1]
    pool.sort(key=lambda record: record["sharpness"]
              - record.get("hand_speed", 0.0) * 40, reverse=True)
    chosen = []
    for record in pool:
        if len(chosen) >= count:
            break
        if all(abs(record["t"] - other["t"]) >= min_gap for other in chosen):
            chosen.append(record)
    chosen.sort(key=lambda record: record["t"])
    return [{"index": record["index"], "t": record["t"],
             "action": record["action"], "smile": False,
             "tags": ["清晰"], "people": record["people"],
             "sharpness": record["sharpness"], "boxes": record["boxes"],
             "verdict": None, "score": record["sharpness"]} for record in chosen]


def write_session_overview(root: Path) -> None:
    """把每个视频的关键帧按行拼成一张会话总览，白底、无黑色补位。"""
    rows = []
    for manifest in sorted(root.glob("*/manifest.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        folder = manifest.parent
        tiles = []
        for item in data.get("images", []):
            image = cv2.imread(str(folder / item["file"]))
            if image is None:
                continue
            height = 200
            scale = height / image.shape[0]
            image = cv2.resize(image, (round(image.shape[1] * scale), height))
            label = item["file"]
            cv2.putText(image, label, (5, 19), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (255, 255, 255), 3, cv2.LINE_AA)
            cv2.putText(image, label, (5, 19), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (0, 160, 0), 1, cv2.LINE_AA)
            tiles.append(cv2.copyMakeBorder(image, 4, 4, 4, 4,
                                            cv2.BORDER_CONSTANT, value=(255, 255, 255)))
        if not tiles:
            placeholder = np.full((208, 368, 3), 255, dtype=np.uint8)
            cv2.putText(placeholder, "no keyframe", (10, 110),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 220), 2, cv2.LINE_AA)
            tiles = [placeholder]
        row = np.hstack(tiles)
        header = np.full((34, row.shape[1], 3), 255, dtype=np.uint8)
        cv2.putText(header, f"{folder.name}  ({len(data.get('images', []))} keyframes)",
                    (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (30, 30, 30), 2,
                    cv2.LINE_AA)
        rows.extend([header, row,
                     np.full((8, row.shape[1], 3), 255, dtype=np.uint8)])
    if not rows:
        return
    width = max(row.shape[1] for row in rows)
    rows = [cv2.copyMakeBorder(row, 0, 0, 0, width - row.shape[1],
                               cv2.BORDER_CONSTANT, value=(255, 255, 255))
            for row in rows]
    ok, encoded = cv2.imencode(".jpg", np.vstack(rows),
                               [cv2.IMWRITE_JPEG_QUALITY, 92])
    if ok:
        (root / "overview.jpg").write_bytes(encoded.tobytes())


def extract_keyframes(video_path: Path, out_dir: Path, options, log=None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    model_path = resolve_model(options.model)
    detector = PoseDetector(model_path)
    try:
        records, duration = scan_video(video_path, detector, options.step)
    finally:
        detector.close()
    candidates = select_candidates(records, options.action_threshold,
                                   options.vlm_max, options.min_gap,
                                   options.max_hand_speed)
    judge = None
    if options.vlm and options.vlm_max > 0:
        try:
            judge = HighlightJudge(json.loads(Path(options.config).read_text()))
        except Exception as error:  # noqa: BLE001 - 服务不可用退回姿态判定
            judge = None
            if log:
                log({"type": "keyframe_vlm_unavailable", "reason": repr(error)})
    frames = _read_frames(video_path, [record["index"] for record in candidates])
    chosen, judged = choose_keyframes(candidates, frames, judge, options.count,
                                      options.min_gap, options.vlm,
                                      options.min_sharpness,
                                      options.action_threshold,
                                      options.max_hand_speed)
    if not chosen and options.count > 0:
        fallback = quality_fallback(records, options.count, options.min_gap,
                                    options.min_sharpness,
                                    options.max_hand_speed)
        frames.update(_read_frames(video_path,
                                   [item["index"] for item in fallback
                                    if item["index"] not in frames]))
        chosen = fallback

    images, last_hash, last_tags = [], None, None
    for number, item in enumerate(chosen, 1):
        frame = frames.get(item["index"])
        if frame is None:
            continue
        digest = ahash(frame)
        tags = item["tags"] or ["关键帧"]
        if (last_hash is not None and hash_distance(digest, last_hash) < DEFAULT_DUP_AHASH
                and tags == last_tags):
            continue
        label = "-".join(NAME_TAGS.get(tag, "key") for tag in tags)
        name = f"key_{number:03d}_{item['t']:.1f}s_{label}.jpg"
        path = out_dir / name
        if not cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, options.jpeg_quality]):
            continue
        last_hash, last_tags = digest, tags
        images.append({
            "file": name, "t_s": item["t"], "tags": tags,
            "action": item["action"], "smile": item["smile"],
            "people": item["people"], "sharpness": item["sharpness"],
            "face_sharpness": item.get("face_sharpness", 0.0),
            "boxes": item["boxes"], "smile_vlm": item.get("smile_verdict"),
        })
    manifest = {
        "source_video": str(video_path),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "params": {
            "model": str(model_path), "step_s": options.step,
            "count": options.count, "min_gap_s": options.min_gap,
            "action_threshold": options.action_threshold,
            "min_sharpness": options.min_sharpness,
            "max_hand_speed": options.max_hand_speed,
            "vlm_max": options.vlm_max, "use_vlm": bool(options.vlm),
            "jpeg_quality": options.jpeg_quality,
        },
        "scan": {"duration_s": round(duration, 2), "samples": len(records),
                 "candidates": len(candidates), "elapsed_s": round(time.monotonic() - started, 2)},
        "candidates": judged,
        "images": images,
    }
    atomic_json(out_dir / "manifest.json", manifest)
    write_contact_sheet(out_dir, images)
    if getattr(options, "overview", None):
        write_session_overview(Path(options.overview))
    if log:
        log({"type": "keyframe_finish", "video": str(video_path),
             "images": len(images), "candidates": len(candidates),
             "elapsed_s": manifest["scan"]["elapsed_s"]})
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从已录视频中筛选动作/微笑关键帧")
    parser.add_argument("--video", required=True, help="输入 MP4")
    parser.add_argument("--output", required=True, help="关键帧输出目录")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="导演配置（复用 VLM endpoint/model）")
    parser.add_argument("--model", help="yolov8s-pose.cix 路径")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT,
                        help="最多保存的关键帧数量")
    parser.add_argument("--step", type=float, default=DEFAULT_STEP_S,
                        help="扫描采样间隔秒数")
    parser.add_argument("--min-gap", type=float, default=DEFAULT_MIN_GAP_S,
                        help="两张关键帧的最小时间间隔秒数")
    parser.add_argument("--action-threshold", type=float,
                        default=DEFAULT_ACTION_THRESHOLD,
                        help="姿态动作分阈值")
    parser.add_argument("--vlm-max", type=int, default=DEFAULT_VLM_MAX,
                        help="送 VLM 确认的候选帧上限，0 表示不用 VLM")
    parser.add_argument("--no-vlm", action="store_true", help="只用姿态动作判定")
    parser.add_argument("--min-sharpness", type=float, default=DEFAULT_MIN_SHARPNESS,
                        help="人体区域清晰度下限，低于该值视为运动模糊")
    parser.add_argument("--max-hand-speed", type=float, default=DEFAULT_MAX_HAND_SPEED,
                        help="手腕相对肩部速度上限（肩宽/秒），超过视为挥手拖影")
    parser.add_argument("--overview", help="生成会话总览图的目录（写入 overview.jpg）")
    parser.add_argument("--jpeg-quality", type=int, default=DEFAULT_JPEG_QUALITY)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    video = Path(args.video)
    if not video.is_file():
        raise SystemExit(f"视频文件不存在: {video}")
    if args.count < 1 or args.count > 30:
        raise SystemExit("--count 需要在 1..30 之间")
    if not 0.1 <= args.step <= 5:
        raise SystemExit("--step 需要在 0.1..5 秒之间")
    if not 0.2 <= args.min_gap <= 30:
        raise SystemExit("--min-gap 需要在 0.2..30 秒之间")
    if not 0.1 <= args.action_threshold <= 1:
        raise SystemExit("--action-threshold 需要在 0.1..1 之间")
    if args.vlm_max < 0 or args.vlm_max > 60:
        raise SystemExit("--vlm-max 需要在 0..60 之间")
    if args.min_sharpness < 0 or args.min_sharpness > 5000:
        raise SystemExit("--min-sharpness 需要在 0..5000 之间")
    if not 0.1 <= args.max_hand_speed <= 10:
        raise SystemExit("--max-hand-speed 需要在 0.1..10 之间")
    if args.no_vlm:
        args.vlm_max = 0
    args.vlm = args.vlm_max > 0
    manifest = extract_keyframes(video, Path(args.output), args)
    print(f"关键帧 {len(manifest['images'])} 张 → {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
