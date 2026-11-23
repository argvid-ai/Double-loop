#!/usr/bin/env python3
"""人物触发录像 + 导演规则 + 单发连续运镜。

流程:
    1. 连接运行中的 r1_jog，以当前实测关节角为固定基准；
    2. C60E 连续取流，YOLO 检测人数并录像；
    3. MiniCPM-V 按导演规则判断双人交流 / 多人讨论等场景；
    4. 一次只执行一个技能，播放结束并静默 quiet_after_action_s 后才接受下一次：
       - ConversationSweep / GroupWide：traj_j1 连续扫拍；
       - 单人行进、双人交流推进、起立跟随：VisionTrack 闭环跟踪，
         按 YOLO 人体框水平偏差调整 J1，头顶贴近上沿时用 J4 抬高机位；
         画面误差长时间不改善时记录 track_stall；
       双人交流在 ConversationSweep 与 ConversationPush 之间交替；
    5. 画面 aHash、场景和人数与上次触发大致相同时，不重复同一技能；
    6. 每段录像最长 60 秒，人物离开后按 post 秒数封装。

运行环境:
    LD_LIBRARY_PATH=/usr/share/cix/lib \
    ~/yolo_test/p1-yolo-test/.venv/bin/python director_capture.py

前置条件:
    - r1_jog 已启动并使能: ./run.sh --api /tmp/r1_jog.sock
    - C60E 未被其他进程占用
    - 设置 R1_VLM_ENDPOINT，指向已启动的视觉模型服务
"""

import argparse
import glob
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Full, Queue

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DIRECTOR_DIR = PROJECT_ROOT / "director"
if not DIRECTOR_DIR.is_dir():
    DIRECTOR_DIR = PROJECT_ROOT / "r1-roadshow" / "director"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(DIRECTOR_DIR))

import cv2
import numpy as np

import pose
import keyframes
from logic import Director
from vision import PersonDetector, VLM, movement_candidates

DEFAULT_CONFIG = DIRECTOR_DIR / "config.json"
DEFAULT_SOCKET = "/tmp/r1_jog.sock"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "captures"
# 单段安全上限；正常情况下动作完成就结束录像。
DEFAULT_CLIP_SECONDS = 30
DEFAULT_ENTER_S = 1.0
DEFAULT_POST_S = 3.0
# 多人进场整段录像（与导演运镜并存）默认参数。
DEFAULT_MIN_PEOPLE = 2
DEFAULT_PRE_S = 3.0
DEFAULT_MAX_GROUP_S = 120.0
DEFAULT_POSE_HOLD_S = 1.2
DEFAULT_POSE_MOTION = 0.015
DEFAULT_POSE_HEIGHT_CHANGE = 0.05
DEFAULT_SHOT_INTERVAL_S = 2.5
DEFAULT_MAX_SHOTS_PER_HOLD = 4
DEFAULT_FALLBACK_COUNT = 3
DEFAULT_FALLBACK_STEP_S = 2.0
DEFAULT_JPEG_QUALITY = 95
DEFAULT_DUP_AHASH = 6
# 相机取流参数：C60E 实测（2026-11-22，固定 10ms 曝光）brightness=177 时
# 全画面 6.7% 像素 ≥250，白墙和浅色衣服高光被截断；165 时约 1.5%，
# 亮度也已接近原来的观感。sharpness 保持默认不上调，避免放大颗粒。
DEFAULT_BRIGHTNESS = 165
DEFAULT_SHARPNESS = 128
DEFAULT_FOCUS = 300
# 4K 原图模式：相机按 4K 取流，录制成 1080p 视频，关键帧优先用 4K 原图。
STILL_SIZE = (3840, 2160)
VIDEO_SIZE = (1920, 1080)
DEFAULT_STILL_INTERVAL_S = 0.5
DEFAULT_STILL_QUALITY = 92
# 录像关键帧（动作/微笑）默认参数。
DEFAULT_KEYFRAME_COUNT = 6
DEFAULT_KEYFRAME_STEP_S = 0.4
DEFAULT_KEYFRAME_MIN_GAP_S = 1.0
DEFAULT_KEYFRAME_ACTION = 0.5
DEFAULT_KEYFRAME_VLM_MAX = 12
DEFAULT_KEYFRAME_MIN_SHARPNESS = 60.0
DEFAULT_KEYFRAME_MAX_HAND_SPEED = 1.2
DEFAULT_KEYFRAME_MAX_HEAD_SPEED = 1.0
DEFAULT_SWEEP_DEG = 8.0
DEFAULT_WIDE_DEG = 10.0
GOTO_TIMEOUT_S = 20.0
GOTO_PENDING_RAD = 2e-4
SETTLE_TIMEOUT_S = 30.0
TRACK_INTERVAL_S = 0.5
TRACK_DEADBAND = 0.08
# 实测：J1 正向可让人像左移（ex 减小），负向常被相机自动构图抵消。
# 小角度还会被静摩擦吃掉，因此每步至少 2.9°，只用水平闭环，垂直交给相机。
TRACK_PAN_GAIN = 0.50
TRACK_MIN_STEP_RAD = 0.05
TRACK_STEP_LIMIT_RAD = 0.06
TRACK_TOTAL_LIMIT_RAD = 0.50
TRACK_HEAD_MARGIN = 0.06
TRACK_TILT_STEP_RAD = 0.05
TRACK_TILT_TOTAL_LIMIT_RAD = 0.20
# 摆拍取景：头顶贴边时逐步上抬 J4，保证头部完整；脚出画不处理。
HEADROOM_MARGIN = 0.03
HEADROOM_STEP_RAD = 0.04
HEADROOM_INTERVAL_S = 0.8
HEADROOM_LIMIT_RAD = 0.28
TRACK_LOST_S = 1.0
TRACK_NO_RESPONSE_STEPS = 4
FAR_BOX_HEIGHT = 0.45
RETURN_ENVELOPE_RAD = (0.30, 0.90, 0.20, 1.10, 0.20, 0.20, 0.50)


class EventLog:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def write(self, event: dict) -> None:
        event = dict(event)
        event.setdefault("at_utc", datetime.now(timezone.utc).isoformat())
        line = json.dumps(event, ensure_ascii=False, allow_nan=False)
        with self.lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def ahash(frame) -> int:
    """8x8 平均哈希，用来判断画面内容是否大致相同。"""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (8, 8), interpolation=cv2.INTER_AREA)
    mean = float(small.mean())
    value = 0
    for pixel in small.flatten():
        value = (value << 1) | (1 if pixel >= mean else 0)
    return value


def hash_distance(first: int, second: int) -> int:
    return bin(first ^ second).count("1")


def sharpness(frame) -> float:
    """用灰度缩略图的 Laplacian 方差衡量清晰度，用于在摆拍区间内择优。"""
    height, width = frame.shape[:2]
    if width > 640:
        scale = 640 / width
        frame = cv2.resize(frame, (640, max(1, round(height * scale))),
                           interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def normalized_boxes(people, shape):
    """人体框转成归一化的 (中心 x, 中心 y, 宽, 高)。"""
    height, width = shape[:2]
    boxes = []
    for person in people:
        x1, y1, x2, y2 = (float(value) for value in person["xyxy"])
        boxes.append(((x1 + x2) / 2 / width, (y1 + y2) / 2 / height,
                      (x2 - x1) / width, (y2 - y1) / height))
    return boxes


def parse_zone(value: str):
    """解析左,上,右,下归一化触发区域。"""
    try:
        left, top, right, bottom = (float(item) for item in value.split(","))
    except ValueError:
        raise SystemExit("--zone 格式为 左,上,右,下，例如 0.1,0.1,0.9,0.95")
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise SystemExit("--zone 坐标须从左上到右下，且在 0 到 1 之间")
    return left, top, right, bottom


def people_in_zone(people, shape, zone):
    """用人体框底边中心判断是否在触发区域内。"""
    height, width = shape[:2]
    left, top, right, bottom = zone
    result = []
    for person in people:
        x1, y1, x2, y2 = (float(value) for value in person["xyxy"])
        x, y = (x1 + x2) / 2 / width, y2 / height
        if left <= x <= right and top <= y <= bottom:
            result.append(person)
    return result


def composition_ok(people, shape, margin=0.01, min_area=0.08):
    """所有人头部完整可见即可，允许脚被画面下沿裁掉。

    头部完整性按人体框的上边和左右边判断；下边不再作为拒绝条件，
    避免近距离双人场景因为脚出画而丢掉所有摆拍帧。
    """
    if not people:
        return None
    height, width = shape[:2]
    area = 0.0
    for person in people:
        x1, y1, x2, y2 = (float(value) for value in person["xyxy"])
        if (x1 <= margin * width or y1 <= margin * height
                or x2 >= (1 - margin) * width):
            return None
        area += (x2 - x1) * (y2 - y1)
    if area / (width * height) < min_area:
        return None
    return area / (width * height)


def feet_cut(people, shape, margin=0.01):
    """是否有人的脚/腿部被画面下沿裁掉；这不影响抽帧。"""
    if not people:
        return False
    height = shape[0]
    return any(float(person["xyxy"][3]) >= (1 - margin) * height
               for person in people)


def write_contact_sheet(out_dir: Path, images, columns: int = 4,
                        tile_width: int = 480) -> None:
    """把抽帧图片拼成一张预览图，便于现场快速检查。"""
    tiles = []
    for item in images:
        path = out_dir / item["file"]
        image = cv2.imread(str(path))
        if image is None:
            continue
        scale = tile_width / image.shape[1]
        image = cv2.resize(image, (tile_width, max(1, round(image.shape[0] * scale))))
        cv2.putText(image, item["file"], (6, min(22, image.shape[0] - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2, cv2.LINE_AA)
        cv2.putText(image, item["file"], (6, min(22, image.shape[0] - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1, cv2.LINE_AA)
        tiles.append(image)
    if not tiles:
        return
    # 按实际张数排列，避免不足 4 张时右侧补出黑块。
    columns = min(columns, len(tiles))
    tile_height = max(tile.shape[0] for tile in tiles)
    rows = []
    for start in range(0, len(tiles), columns):
        row = tiles[start:start + columns]
        while len(row) < columns:
            row.append(np.zeros((tile_height, tile_width, 3), dtype=np.uint8))
        row = [cv2.copyMakeBorder(tile, 0, tile_height - tile.shape[0], 0, 0,
                                  cv2.BORDER_CONSTANT, value=(0, 0, 0))
               for tile in row]
        rows.append(np.hstack(row))
    sheet = np.vstack(rows)
    ok, encoded = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if ok:
        (out_dir / "contact.jpg").write_bytes(encoded.tobytes())



def choose_skill(action, last_skill):
    """把导演规则动作翻译成实机技能：人物类全部走闭环视觉跟踪。"""
    if action in ("PortraitStandingPreset", "FollowLeft", "FollowRight", "RiseFollow"):
        return "VisionTrack"
    if action == "ConversationSweep":
        return "ConversationPush" if last_skill == "ConversationSweep" else "ConversationSweep"
    if action == "GroupWide":
        return "GroupWide"
    return None


class VisionState:
    """主循环持续写入最新人体框，跟踪线程按需读取。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.people = []
        self.shape = None
        self.stamp = 0.0

    def update(self, people, shape) -> None:
        with self.lock:
            self.people = list(people)
            self.shape = shape
            self.stamp = time.monotonic()

    def snapshot(self):
        with self.lock:
            return list(self.people), self.shape, self.stamp


def framing_error(people, shape):
    """返回人体框并集中心相对画面中心的归一化偏差 (ex, ey)。"""
    if not people or shape is None:
        return None
    height, width = shape[:2]
    boxes = [person["xyxy"] for person in people]
    x1 = min(box[0] for box in boxes)
    x2 = max(box[2] for box in boxes)
    y1 = min(box[1] for box in boxes)
    y2 = max(box[3] for box in boxes)
    return ((x1 + x2) / 2 / width - 0.5, (y1 + y2) / 2 / height - 0.5)


def head_top_ratio(people, shape):
    """人体框顶部（头顶）在画面中的归一化位置。"""
    if not people or shape is None:
        return None
    return min(float(person["xyxy"][1]) for person in people) / shape[0]


class ArmMover:
    """一次只执行一个技能：J1 扫拍或基于 YOLO 框的闭环视觉跟踪。"""

    def __init__(self, socket_path: str, base_q, log: EventLog,
                 vision: VisionState, track_seconds: float):
        self.socket_path = socket_path
        self.base = [float(value) for value in base_q]
        self.log = log
        self.vision = vision
        self.track_seconds = track_seconds
        self.action = None
        self.duration = 0.0
        self.active_until = 0.0
        self.thread = None
        self.cancel = threading.Event()
        self.away = False

    def busy(self) -> bool:
        moving = self.thread is not None and self.thread.is_alive()
        return moving or time.monotonic() < self.active_until

    def add_headroom_tilt(self) -> bool:
        """远处有人头贴画面顶边时，向上抬一点 J4，保证头部完整。

        只调整机位俯仰，不改 J1；累计上限相对本次会话基准。
        """
        if self.busy():
            return False
        try:
            arm = pose.Arm(self.socket_path)
        except OSError as error:
            self.log.write(
                {"type": "motion_error", "action": "HeadroomTilt",
                 "reason": f"API 不可用: {error}"}
            )
            return False
        try:
            status = arm.call("status")
            if status.get("state") != "running":
                return False
            command = [float(value) for value in status["q_rad"]]
            if command[3] + HEADROOM_STEP_RAD - self.base[3] > HEADROOM_LIMIT_RAD:
                return False
            command[3] += HEADROOM_STEP_RAD
            reply = arm.call("goto_pose " + " ".join(repr(value) for value in command))
        except Exception as error:  # noqa: BLE001 - 录像不能因控制服务断开而退出
            self.log.write(
                {"type": "motion_error", "action": "HeadroomTilt",
                 "reason": f"{type(error).__name__}: {error}"}
            )
            return False
        finally:
            arm.close()
        if not reply.get("ok"):
            self.log.write(
                {"type": "motion_error", "action": "HeadroomTilt",
                 "reason": str(reply.get("error", "unknown"))}
            )
            return False
        self.log.write(
            {"type": "headroom_tilt", "q4_rad": round(command[3], 6),
             "offset_rad": round(command[3] - self.base[3], 6)}
        )
        self.away = True
        return True

    def start(self, action: str, sweep_deg: float, wide_deg: float,
              motion_seconds: float = 0.0, friction_nm: float = -1.0) -> bool:
        if self.busy():
            return False
        if action == "GroupWide":
            amplitude = math.radians(wide_deg)
            requested = 14.0
        elif action == "ConversationSweep":
            amplitude = math.radians(sweep_deg)
            requested = 12.0
        elif action in ("VisionTrack", "ConversationPush"):
            return self._start_track(action)
        else:
            return False
        if motion_seconds > 0:
            requested = motion_seconds

        command = f"traj_j1 {amplitude:.6f} {requested:.3f}"
        if friction_nm >= 0:
            command += f" {friction_nm:.3f}"
        try:
            arm = pose.Arm(self.socket_path)
        except OSError as error:
            self.log.write(
                {
                    "type": "motion_error",
                    "action": action,
                    "reason": f"API 不可用: {error}",
                }
            )
            # 服务断开时退避，避免每个视觉周期都重复连接。
            self.active_until = time.monotonic() + 5.0
            return False
        try:
            reply = arm.call(command)
        except Exception as error:  # noqa: BLE001 - 录像不能因控制服务断开而退出
            self.log.write(
                {
                    "type": "motion_error",
                    "action": action,
                    "reason": f"{type(error).__name__}: {error}",
                }
            )
            self.active_until = time.monotonic() + 5.0
            return False
        finally:
            arm.close()
        if not reply.get("ok"):
            self.log.write(
                {
                    "type": "motion_error",
                    "action": action,
                    "reason": str(reply.get("error", "unknown")),
                }
            )
            return False
        actual = float(reply.get("sweep_duration_s", requested))
        self.action = action
        self.duration = actual
        self.active_until = time.monotonic() + actual + 0.3
        self.log.write(
            {
                "type": "motion_start",
                "action": action,
                "amplitude_rad": amplitude,
                "duration_s": actual,
                "mode": "traj_j1",
            }
        )
        return True

    def _start_track(self, action: str) -> bool:
        """启动 YOLO 闭环跟踪；初始状态不可用时立即失败，不占用触发名额。"""
        try:
            arm = pose.Arm(self.socket_path)
        except OSError as error:
            self.log.write(
                {
                    "type": "motion_error",
                    "action": action,
                    "reason": f"API 不可用: {error}",
                }
            )
            self.active_until = time.monotonic() + 5.0
            return False
        try:
            status = arm.call("status")
            if not status.get("ok"):
                raise RuntimeError(str(status.get("error", "status failed")))
            if status.get("state") != "running":
                raise RuntimeError(f"state={status.get('state')}")
            start_q = [float(value) for value in status["q_rad"]]
        except Exception as error:  # noqa: BLE001 - 记录后保持服务运行
            self.log.write(
                {
                    "type": "motion_error",
                    "action": action,
                    "reason": f"{type(error).__name__}: {error}",
                }
            )
            return False
        finally:
            arm.close()

        estimate = self.track_seconds + 5.0
        self.cancel.clear()
        self.action = action
        self.away = True
        self.duration = estimate
        self.active_until = time.monotonic() + estimate
        self.thread = threading.Thread(
            target=self._run_track, args=(action, start_q), daemon=True
        )
        self.thread.start()
        self.log.write(
            {
                "type": "motion_start",
                "action": action,
                "duration_s": estimate,
                "mode": "vision_track",
            }
        )
        return True

    def _run_track(self, action: str, start_q) -> None:
        """按最新 YOLO 人体框水平偏差闭环调整 J1，直到居中或到达限幅。"""
        arm = None
        try:
            arm = pose.Arm(self.socket_path)
            deadline = time.monotonic() + self.track_seconds
            last_seen = time.monotonic()
            last_cmd = 0.0
            last_error = None
            no_response = 0
            settled = 0
            while not self.cancel.is_set() and time.monotonic() < deadline:
                people, shape, stamp = self.vision.snapshot()
                now = time.monotonic()
                if not people or shape is None or now - stamp > 1.0:
                    if now - last_seen > TRACK_LOST_S:
                        break
                    time.sleep(0.1)
                    continue
                last_seen = now
                error = framing_error(people, shape)
                if error is None:
                    time.sleep(0.1)
                    continue
                ex, ey = error
                top = head_top_ratio(people, shape)
                head_high = top is not None and top < TRACK_HEAD_MARGIN
                if abs(ex) <= TRACK_DEADBAND and not head_high:
                    last_error = None
                    no_response = 0
                    settled += 1
                    if settled >= 6:
                        # 连续约 1 秒保持居中，提前结束本次跟踪，加快下一段响应。
                        break
                    time.sleep(0.15)
                    continue
                settled = 0
                if now - last_cmd < TRACK_INTERVAL_S:
                    time.sleep(0.05)
                    continue
                status = arm.call("status")
                if status.get("state") != "running":
                    raise RuntimeError(f"state={status.get('state')}")
                command = [float(value) for value in status["q_rad"]]
                pan = 0.0
                if abs(ex) > TRACK_DEADBAND:
                    pan = TRACK_PAN_GAIN * ex
                    if abs(pan) < TRACK_MIN_STEP_RAD:
                        pan = math.copysign(TRACK_MIN_STEP_RAD, pan)
                    pan = max(-TRACK_STEP_LIMIT_RAD,
                              min(TRACK_STEP_LIMIT_RAD, pan))
                    if abs((command[0] + pan) - self.base[0]) > TRACK_TOTAL_LIMIT_RAD:
                        pan = 0.0
                # 头顶贴近画面上沿时抬高机位，让人物重新完整入画。
                tilt = TRACK_TILT_STEP_RAD if head_high else 0.0
                if tilt and (abs((command[3] + tilt) - self.base[3])
                             > TRACK_TILT_TOTAL_LIMIT_RAD):
                    tilt = 0.0
                if pan == 0.0 and tilt == 0.0:
                    break
                command[0] += pan
                command[3] += tilt
                reply = arm.call(
                    "goto_pose " + " ".join(repr(value) for value in command)
                )
                if not reply.get("ok"):
                    raise RuntimeError(str(reply.get("error", "goto_pose failed")))
                last_cmd = now
                magnitude = math.hypot(ex, ey)
                if last_error is not None and magnitude > last_error - 0.004:
                    no_response += 1
                else:
                    no_response = 0
                last_error = magnitude
                self.log.write(
                    {
                        "type": "track_step",
                        "action": action,
                        "ex": ex,
                        "ey": ey,
                        "head_top": top,
                        "pan_rad": pan,
                        "tilt_rad": tilt,
                        "mode": "vision_track",
                    }
                )
                if no_response == TRACK_NO_RESPONSE_STEPS:
                    # 相机可能在做数字补偿，但继续按 YOLO 结果修正到限幅为止。
                    self.log.write(
                        {
                            "type": "track_stall",
                            "action": action,
                            "reason": "画面误差未明显改善，继续跟踪到限幅",
                        }
                    )
                time.sleep(0.05)
            # 跟踪结束后保持当前构图，不自动回位；确认无人时由主循环回基准。
        except Exception as error:  # noqa: BLE001 - 记录后保持服务运行
            if not self.cancel.is_set():
                self.log.write(
                    {
                        "type": "motion_error",
                        "action": action,
                        "reason": f"{type(error).__name__}: {error}",
                    }
                )
        finally:
            if arm is not None:
                arm.close()
            if not self.cancel.is_set():
                # 跟踪可能提前结束，让主循环立即进入静默窗口。
                self.active_until = 0.0

    def _wait_joint(self, arm) -> bool:
        deadline = time.monotonic() + SETTLE_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.cancel.is_set():
                return False
            status = arm.call("status")
            if status.get("state") != "running":
                return False
            if float(status.get("pending_joint_rad", 0.0)) <= GOTO_PENDING_RAD:
                return True
            time.sleep(0.1)
        return False

    def poll(self):
        if self.action is not None and not self.busy():
            finished = self.action
            self.log.write({"type": "motion_finish", "action": finished, "ok": True})
            self.action = None
            self.duration = 0.0
            self.thread = None
            return finished
        return None

    def return_to_base(self) -> None:
        self.cancel.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        if self.action is None and not self.away:
            return
        try:
            arm = pose.Arm(self.socket_path)
        except OSError as error:
            self.log.write(
                {
                    "type": "motion_error",
                    "kind": "return_to_base",
                    "reason": f"API 不可用: {error}",
                }
            )
            self.action = None
            self.duration = 0.0
            self.active_until = 0.0
            return
        try:
            for _ in range(12):
                status = arm.call("status")
                if status.get("state") != "running":
                    break
                current = [float(value) for value in status["q_rad"]]
                delta = [self.base[i] - current[i] for i in range(7)]
                if max(abs(value) for value in delta) <= 0.01:
                    break
                target = [
                    current[i] + max(-RETURN_ENVELOPE_RAD[i] * 0.9,
                                     min(RETURN_ENVELOPE_RAD[i] * 0.9, delta[i]))
                    for i in range(7)
                ]
                reply = arm.call(
                    "goto_pose " + " ".join(repr(value) for value in target)
                )
                if not reply.get("ok"):
                    self.log.write(
                        {
                            "type": "motion_error",
                            "kind": "return_to_base",
                            "reason": str(reply.get("error", "unknown")),
                        }
                    )
                    break
                deadline = time.monotonic() + GOTO_TIMEOUT_S
                while time.monotonic() < deadline:
                    status = arm.call("status")
                    if float(status.get("pending_joint_rad", 0.0)) <= GOTO_PENDING_RAD:
                        break
                    time.sleep(0.1)
        finally:
            arm.close()
        self.log.write(
            {"type": "motion_finish", "action": self.action, "ok": True,
             "reason": "return_to_base"}
        )
        self.action = None
        self.duration = 0.0
        self.active_until = 0.0
        self.thread = None
        self.away = False


class ClipWriter:
    def __init__(self, clips_dir: Path, fps: float, size, log: EventLog,
                 prefix: str = "person", on_finish=None, pre_roll: int = 0):
        self.clips_dir = clips_dir
        self.fps = fps
        self.size = size
        self.log = log
        self.prefix = prefix
        self.on_finish = on_finish
        # 预录帧在开录瞬间一次性入队，时间戳几乎相同；
        # 它们按顺序写入，之后的实时帧再按时间戳补帧。
        self.pre_roll = max(0, int(pre_roll))
        self.writer = None
        self.part_path = None
        self.final_path = None
        self.started = 0.0
        self.index = 0
        self.written = 0
        self.resized = 0
        self.dropped = 0
        self.duplicated = 0
        self.queue = None
        self.thread = None

    def active(self) -> bool:
        return self.writer is not None

    @property
    def name(self):
        return self.final_path.stem if self.final_path is not None else None

    def position(self) -> float:
        """当前画面在视频时间线上的秒数（含预录段）。"""
        if not self.active():
            return 0.0
        return self.pre_roll / max(self.fps, 1e-6) + (time.monotonic() - self.started)

    def _encode(self) -> None:
        """编码线程：主循环只负责取流，避免取流被 mp4v 编码阻塞。

        按每帧的采集时间戳补齐帧数：取流偶发丢帧时复用上一帧，
        保证 MP4 时长等于实际拍摄的时间跨度，不会整体快放或变慢。
        """
        last = None
        first_ts = None
        index = 0
        while True:
            item = self.queue.get()
            if item is None:
                return
            captured_at, frame = item
            if self.pre_roll and index < self.pre_roll:
                # 预录帧直接按顺序写入，不参与时间戳补帧。
                try:
                    self.writer.write(frame)
                    self.written += 1
                    last = frame
                except Exception as error:  # noqa: BLE001 - 编码异常只记录
                    self.log.write({"type": "clip_encode_error",
                                    "reason": f"{type(error).__name__}: {error}"})
                index += 1
                continue
            if first_ts is None:
                first_ts = captured_at
            target = self.pre_roll + int(round((captured_at - first_ts) * self.fps))
            index += 1
            if last is not None:
                for _ in range(min(max(target - self.written, 0), max(1, int(self.fps)))):
                    try:
                        self.writer.write(last)
                        self.written += 1
                        self.duplicated += 1
                    except Exception as error:  # noqa: BLE001 - 编码异常只记录
                        self.log.write({"type": "clip_encode_error",
                                        "reason": f"{type(error).__name__}: {error}"})
            try:
                self.writer.write(frame)
                self.written += 1
                last = frame
            except Exception as error:  # noqa: BLE001 - 编码异常只记录
                self.log.write({"type": "clip_encode_error",
                                "reason": f"{type(error).__name__}: {error}"})

    def start(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.index += 1
        name = f"{self.prefix}_{stamp}_{self.index:03d}"
        self.part_path = self.clips_dir / (name + ".part.mp4")
        self.final_path = self.clips_dir / (name + ".mp4")
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self.writer = cv2.VideoWriter(
            str(self.part_path), fourcc, self.fps, self.size
        )
        if not self.writer.isOpened():
            self.writer = None
            raise RuntimeError("无法创建 MP4 录像文件，检查 OpenCV 编码器")
        self.started = time.monotonic()
        self.written = 0
        self.resized = 0
        self.dropped = 0
        self.duplicated = 0
        # 有容量上限的队列：编码偶发变慢时吸收抖动，绝不让延迟无限累积。
        # 预录帧会在开录瞬间一次性入队，队列必须额外容纳它们。
        self.queue = Queue(maxsize=max(8, int(round(self.fps * 0.5)) + self.pre_roll))
        self.thread = threading.Thread(target=self._encode, daemon=True)
        self.thread.start()
        self.log.write({"type": "clip_start", "file": str(self.part_path),
                        "fps": round(self.fps, 2), "size": list(self.size),
                        "queue_max": self.queue.maxsize, "pre_roll": self.pre_roll})

    def write(self, frame, copy: bool = True) -> None:
        if self.queue is None:
            return
        if (frame.shape[1], frame.shape[0]) != self.size:
            frame = cv2.resize(frame, self.size)
            self.resized += 1
        try:
            # 实时帧复制一份交给编码线程；预录帧已在缓冲区独占，直接入队省内存。
            payload = frame.copy() if copy else frame
            self.queue.put_nowait((time.monotonic(), payload))
        except Full:
            self.dropped += 1

    def elapsed(self) -> float:
        return time.monotonic() - self.started if self.active() else 0.0

    def finish(self, reason: str) -> None:
        if self.writer is None:
            return
        elapsed = time.monotonic() - self.started
        if self.queue is not None:
            self.queue.put(None)
            if self.thread is not None:
                self.thread.join(timeout=10)
            self.thread = None
        self.writer.release()
        self.writer = None
        self.queue = None
        final_path = None
        if self.part_path is not None and self.final_path is not None:
            os.replace(self.part_path, self.final_path)
            final_path = self.final_path
            self.log.write(
                {
                    "type": "clip_finish",
                    "file": str(self.final_path),
                    "elapsed_s": round(elapsed, 3),
                    "written_frames": self.written,
                    "resized_frames": self.resized,
                    "dropped_frames": self.dropped,
                    "duplicated_frames": self.duplicated,
                    "write_fps": round(self.written / elapsed, 2) if elapsed > 0 else 0,
                    "target_fps": round(self.fps, 2),
                    "reason": reason,
                }
            )
            if elapsed > 0 and self.written / elapsed < self.fps * 0.9:
                self.log.write({"type": "clip_fps_low",
                                "file": str(self.final_path),
                                "write_fps": round(self.written / elapsed, 2),
                                "target_fps": round(self.fps, 2),
                                "dropped_frames": self.dropped,
                                "duplicated_frames": self.duplicated})
                print(f"[录像] 编码帧率不足：{self.written / elapsed:.1f}/"
                      f"{self.fps:.1f} fps，丢帧 {self.dropped}，"
                      f"补帧 {self.duplicated}", flush=True)
        self.part_path = None
        self.final_path = None
        if final_path is not None and self.on_finish is not None:
            try:
                self.on_finish(final_path)
            except Exception as error:  # noqa: BLE001 - 抽帧失败不能影响录像
                self.log.write({"type": "keyframe_submit_error",
                                "file": str(final_path),
                                "reason": f"{type(error).__name__}: {error}"})


class StillSaver:
    """录像期间按固定间隔保存 4K 原图，关键帧阶段优先用原图输出。"""

    def __init__(self, folder: Path, interval: float, quality: int, log: EventLog):
        self.root = folder / "stills"
        self.interval = max(0.1, float(interval))
        self.quality = max(60, min(100, int(quality)))
        self.log = log
        self.name = None
        self.dir = None
        self.stills = []
        self.last_t = None
        self.count = 0
        self.failed = 0

    def start(self, name: str) -> None:
        self.name = name
        self.dir = self.root / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stills, self.last_t, self.count, self.failed = [], None, 0, 0
        self._flush()
        self.log.write({"type": "stills_start", "name": name,
                        "interval_s": self.interval})

    def maybe_save(self, t: float, frame) -> None:
        if self.dir is None:
            return
        if self.last_t is not None and t - self.last_t < self.interval:
            return
        if shutil.disk_usage(self.dir).free < 2 * 1024**3:
            self.log.write({"type": "stills_skip_disk", "name": self.name})
            self.dir = None
            return
        self.last_t = t
        self.count += 1
        name = f"still_{self.count:04d}_{t:06.2f}s.jpg"
        if cv2.imwrite(str(self.dir / name), frame,
                       [cv2.IMWRITE_JPEG_QUALITY, self.quality]):
            self.stills.append({"file": name, "t_s": round(t, 3)})
            self._flush()
        else:
            self.failed += 1

    def _flush(self) -> None:
        if self.dir is None:
            return
        atomic_json(self.dir / "index.json",
                    {"count": len(self.stills), "stills": self.stills})

    def finish(self) -> None:
        if self.dir is None:
            return
        self._flush()
        self.log.write({"type": "stills_finish", "name": self.name,
                        "stills": len(self.stills), "failed": self.failed})
        self.name = None
        self.dir = None
        self.stills = []


class KeyframeWorker:
    """后台串行调用 keyframes.py，避免关键帧筛选阻塞录像主循环。"""

    def __init__(self, folder: Path, args, log: EventLog):
        self.folder = folder
        self.args = args
        self.log = log
        self.queue = Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, video, name: str = None) -> None:
        if getattr(self.args, "no_keyframes", False) or video is None:
            return
        video = Path(video)
        name = name or video.stem
        # 短动作片段只需少量帧和更少的 VLM 请求，避免队列越积越多。
        group = name.startswith("group")
        count = self.args.keyframe_count if group else min(self.args.keyframe_count, 3)
        vlm_max = self.args.keyframe_vlm_max if group \
            else min(self.args.keyframe_vlm_max, 4)
        step = min(self.args.keyframe_step, 0.3) if group \
            else max(self.args.keyframe_step, 0.5)
        stills_dir = self.folder / "stills" / name
        self.queue.put((video, self.folder / "keyframes" / name, count,
                        vlm_max, step, stills_dir))

    def pending(self) -> int:
        return self.queue.qsize()

    def _run(self) -> None:
        env = dict(os.environ)
        paths = [p for p in env.get("LD_LIBRARY_PATH", "").split(":") if p]
        if "/usr/share/cix/lib" not in paths:
            paths.insert(0, "/usr/share/cix/lib")
        env["LD_LIBRARY_PATH"] = ":".join(paths)
        while True:
            item = self.queue.get()
            if item is None:
                return
            video, out_dir, count, vlm_max, step, stills_dir = item
            out_dir.mkdir(parents=True, exist_ok=True)
            command = [
                sys.executable, str(SCRIPT_DIR / "keyframes.py"),
                "--video", str(video), "--output", str(out_dir),
                "--config", str(self.args.config),
                "--count", str(count),
                "--step", str(step),
                "--min-gap", str(self.args.keyframe_min_gap),
                "--action-threshold", str(self.args.keyframe_action),
                "--vlm-max", "0" if self.args.no_keyframe_vlm else str(vlm_max),
                "--min-sharpness", str(self.args.keyframe_min_sharpness),
                "--max-hand-speed", str(self.args.keyframe_max_hand_speed),
                "--max-head-speed", str(self.args.keyframe_max_head_speed),
                "--overview", str(self.folder / "keyframes"),
                "--jpeg-quality", str(DEFAULT_JPEG_QUALITY),
            ]
            if self.args.keyframe_model:
                command += ["--model", str(self.args.keyframe_model)]
            if stills_dir.is_dir():
                command += ["--stills", str(stills_dir)]
            try:
                with (out_dir / "extract.log").open("a", encoding="utf-8") as stream:
                    result = subprocess.run(command, stdout=stream,
                                            stderr=subprocess.STDOUT, env=env)
                self.log.write({"type": "keyframe_job", "video": str(video),
                                "dir": str(out_dir), "returncode": result.returncode})
            except Exception as error:  # noqa: BLE001 - 后台任务异常只记录
                self.log.write({"type": "keyframe_job_error", "video": str(video),
                                "reason": f"{type(error).__name__}: {error}"})


class PoseCollector:
    """多人相对静止时按节奏保存摆拍帧，不依赖关键点模型。"""

    def __init__(self, out_dir: Path, log: EventLog, min_people: int,
                 hold_s: float, motion: float, height_change: float,
                 shot_interval: float, max_shots: int,
                 jpeg_quality: int = DEFAULT_JPEG_QUALITY):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.min_people = min_people
        self.hold_s = hold_s
        self.motion = motion
        self.height_change = height_change
        self.shot_interval = shot_interval
        self.max_shots = max_shots
        self.jpeg_quality = jpeg_quality
        self.tracks = []
        self.last_t = None
        self.still_since = None
        self.hold_start = None
        self.hold_index = 0
        self.hold_people = 0
        self.shots_in_hold = 0
        self.saturated = False
        self.window_start = None
        self.best = None
        self.last_hash = None
        self.images = []
        self.holds = []

    def _still(self, t, boxes):
        """按最近 0.7 秒轨迹判断当前多人是否保持相对静止。"""
        self.tracks = [track for track in self.tracks if t - track["seen"] <= 0.7]
        matched = []
        fresh = []
        still = True
        for box in boxes:
            best, best_distance = None, 0.08
            for index, track in enumerate(self.tracks):
                if index in matched:
                    continue
                old = track["box"]
                distance = math.hypot(box[0] - old[0], box[1] - old[1])
                ratio = box[2] / old[2] if old[2] > 1e-6 else 0.0
                if distance < best_distance and 0.7 <= ratio <= 1.42:
                    best, best_distance = index, distance
            if best is None:
                # 新出现或明显移动的人，打断静止判定。
                still = False
                fresh.append({"box": box, "seen": t})
                continue
            matched.append(best)
            old_track = self.tracks[best]
            dt = max(t - old_track["seen"], 1e-3)
            # 把不同检测周期换算成 0.2 秒尺度的位移。
            equivalent = best_distance * 0.2 / dt
            height_change = abs(box[3] - old_track["box"][3]) / max(
                old_track["box"][3], 1e-6)
            if equivalent > self.motion or height_change > self.height_change:
                still = False
            fresh.append({"box": box, "seen": t})
        for index, track in enumerate(self.tracks):
            if index not in matched:
                fresh.append(track)
        self.tracks = fresh
        return still

    def update(self, t: float, frame, people) -> None:
        self.last_t = t
        if len(people) < self.min_people:
            self._close_hold(t)
            self.tracks = []
            self.still_since = None
            return
        if not self._still(t, normalized_boxes(people, frame.shape)):
            self._close_hold(t)
            self.still_since = None
            return
        if self.still_since is None:
            self.still_since = t
        if self.hold_start is None:
            if t - self.still_since < self.hold_s:
                return
            self.hold_start = self.still_since
            self.hold_index += 1
            self.hold_people = len(people)
            self.shots_in_hold = 0
            self.saturated = False
            self.window_start = t
            self.log.write({"type": "pose_hold_start", "hold": self.hold_index,
                            "t_s": round(self.hold_start, 3), "people": self.hold_people})
        if self.saturated:
            return
        candidate = self._candidate(t, frame, people)
        if candidate is not None and (self.best is None
                                      or candidate["score"] > self.best["score"]):
            self.best = candidate
        if self.window_start is not None and t - self.window_start >= self.shot_interval:
            self._commit()
            self.window_start = t
            if self.shots_in_hold >= self.max_shots:
                self.saturated = True

    def _candidate(self, t, frame, people):
        height, width = frame.shape[:2]
        area = composition_ok(people, frame.shape)
        if area is None:
            # 头部或左右出画的人像不进入候选，保证保存的每张图头都完整。
            return None
        sharp = sharpness(frame)
        score = sharp * 1.35 + area * 1000.0
        boxes = [[round(float(value), 1) for value in person["xyxy"]]
                 for person in people]
        return {"frame": frame.copy(), "t": t, "people": len(people), "boxes": boxes,
                "sharpness": round(sharp, 1), "clipped": False,
                "feet_cut": feet_cut(people, frame.shape),
                "composition_ok": True, "score": score}

    def _commit(self) -> bool:
        if self.best is None:
            return False
        if self.shots_in_hold >= self.max_shots:
            self.best = None
            return False
        best, self.best = self.best, None
        digest = ahash(best["frame"])
        if (self.last_hash is not None
                and hash_distance(digest, self.last_hash) < DEFAULT_DUP_AHASH):
            self.log.write({"type": "pose_duplicate", "t_s": round(best["t"], 3)})
            return False
        self.shots_in_hold += 1
        name = f"pose_{len(self.images) + 1:03d}_{best['t']:.1f}s.jpg"
        path = self.out_dir / name
        if not cv2.imwrite(str(path), best["frame"],
                           [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]):
            self.log.write({"type": "pose_write_error", "file": str(path)})
            return False
        self.last_hash = digest
        item = {"file": name, "t_s": round(best["t"], 3), "hold": self.hold_index,
                "people": best["people"], "sharpness": best["sharpness"],
                "boxes": best["boxes"], "clipped": best["clipped"],
                "feet_cut": best.get("feet_cut", False),
                "composition_ok": best["composition_ok"], "source": "hold"}
        self.images.append(item)
        self.log.write({"type": "pose_saved", **item, "path": str(path)})
        return True

    def _close_hold(self, t) -> None:
        if self.hold_start is None:
            return
        self._commit()
        self.holds.append({"index": self.hold_index,
                           "start_s": round(self.hold_start, 3),
                           "end_s": round(t, 3),
                           "people": self.hold_people})
        self.log.write({"type": "pose_hold_end", "hold": self.hold_index,
                        "start_s": round(self.hold_start, 3),
                        "end_s": round(t, 3), "people": self.hold_people})
        self.hold_start = None
        self.window_start = None
        self.saturated = False
        self.best = None

    def finalize(self, source_video=None) -> None:
        if self.hold_start is not None:
            self._close_hold(self.last_t if self.last_t is not None else self.hold_start)
        self._commit()
        manifest = {
            "source_video": str(source_video) if source_video else None,
            "min_people": self.min_people,
            "pose_hold_s": self.hold_s,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "holds": self.holds,
            "images": self.images,
        }
        atomic_json(self.out_dir / "manifest.json", manifest)
        write_contact_sheet(self.out_dir, self.images)


def _fallback_frames(video_path: Path, collector: PoseCollector, detector,
                     args, log: EventLog) -> int:
    """整段没有摆拍图片时，按清晰度与构图从视频里补几张。"""
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        log.write({"type": "fallback_error", "reason": f"无法打开视频 {video_path}"})
        return 0
    fps = capture.get(cv2.CAP_PROP_FPS)
    if not math.isfinite(fps) or fps <= 0:
        fps = 30.0
    step = max(0.5, args.fallback_step)
    pool = []
    index, next_sample = 0, 0.0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            t = index / fps
            if t + 1e-6 >= next_sample:
                people = detector.detect(frame)
                area = composition_ok(people, frame.shape) if (
                    len(people) >= collector.min_people) else None
                if area is not None:
                    sharp = sharpness(frame)
                    pool.append({"score": sharp + area * 1000.0,
                                 "sharpness": sharp, "t": t, "index": index,
                                 "people": len(people)})
                next_sample += step
            index += 1
    finally:
        capture.release()
    pool.sort(key=lambda item: item["score"], reverse=True)
    chosen = []
    for item in pool:
        if all(abs(item["t"] - other["t"]) >= 1.5 for other in chosen):
            chosen.append(item)
        if len(chosen) >= args.fallback_count:
            break
    chosen.sort(key=lambda item: item["t"])
    if not chosen:
        log.write({"type": "fallback_empty", "video": str(video_path)})
        return 0
    capture = cv2.VideoCapture(str(video_path))
    saved = 0
    try:
        for number, item in enumerate(chosen, 1):
            capture.set(cv2.CAP_PROP_POS_FRAMES, item["index"])
            ok, frame = capture.read()
            if not ok:
                continue
            name = f"fallback_{number:03d}_{item['t']:.1f}s.jpg"
            if not cv2.imwrite(str(collector.out_dir / name), frame,
                               [cv2.IMWRITE_JPEG_QUALITY, DEFAULT_JPEG_QUALITY]):
                continue
            collector.images.append({
                "file": name, "t_s": round(item["t"], 3), "hold": 0,
                "people": item["people"], "sharpness": round(item["sharpness"], 1),
                "boxes": [], "clipped": False, "composition_ok": True,
                "source": "fallback"})
            saved += 1
            log.write({"type": "fallback_saved", "file": name,
                       "t_s": round(item["t"], 3), "people": item["people"]})
    finally:
        capture.release()
    return saved


def extract_from_video(video_path: Path, out_dir: Path, detector, args,
                       log: EventLog) -> int:
    """离线/兜底抽帧：先按静止区间取图，没有区间时按清晰度补图。"""
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        log.write({"type": "extract_error", "reason": f"无法打开视频 {video_path}"})
        return 0
    fps = capture.get(cv2.CAP_PROP_FPS)
    if not math.isfinite(fps) or fps <= 0:
        fps = 30.0
    collector = PoseCollector(out_dir, log, args.min_people, args.pose_hold,
                              args.pose_motion, args.pose_height_change,
                              args.shot_interval, args.max_shots_per_hold)
    index, next_sample = 0, 0.0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            t = index / fps
            if t + 1e-6 >= next_sample:
                collector.update(t, frame, detector.detect(frame))
                next_sample += 0.5
            index += 1
    finally:
        capture.release()
    if not collector.images:
        _fallback_frames(video_path, collector, detector, args, log)
    collector.finalize(video_path)
    log.write({"type": "extract_finish", "video": str(video_path),
               "images": len(collector.images), "holds": len(collector.holds)})
    return len(collector.images)


class Showcase:
    """多人进场整段录像 + 摆拍抽帧，嵌在导演主循环里与运镜并存。"""

    def __init__(self, folder: Path, clips_dir: Path, log: EventLog, detector,
                 args, fps: float, size, session: dict, on_clip=None,
                 camera=None):
        self.folder = folder
        self.clips_dir = clips_dir
        self.poses_root = folder / "poses"
        self.log = log
        self.detector = detector
        self.args = args
        self.fps = fps
        self.size = size
        self.session = session
        self.on_clip = on_clip
        self.camera = camera
        self.min_people = args.min_people
        self.zone = parse_zone(args.zone)
        self.buffer = deque(maxlen=max(1, int(round((args.pre + args.enter + 0.5) * fps))))
        self.state = "standby"
        self.group_index = 0
        self.group_high_res = False
        self.clip = None
        self.collector = None
        self.present_since = None
        self.last_seen = None
        self.last_count = 0
        self.people_max = 0
        self.trigger_s = 0.0
        self.started = time.monotonic()
        self.last_standby = 0.0
        self.last_status = 0.0
        self.last_disk_check = 0.0
        self.groups = []

    @property
    def recording(self) -> bool:
        return self.state == "recording"

    def _write_standby(self, t, frame) -> None:
        preview = frame.copy()
        text = f"STANDBY {t:5.1f}s people={self.last_count} need>={self.min_people}"
        cv2.putText(preview, text, (24, 48), cv2.FONT_HERSHEY_SIMPLEX,
                    1.0, (0, 255, 0), 3, cv2.LINE_AA)
        cv2.putText(preview, text, (24, 48), cv2.FONT_HERSHEY_SIMPLEX,
                    1.0, (0, 0, 0), 1, cv2.LINE_AA)
        pending = self.folder / "standby.tmp.jpg"
        if cv2.imwrite(str(pending), preview, [cv2.IMWRITE_JPEG_QUALITY, 85]):
            os.replace(pending, self.folder / "standby.jpg")

    def on_frame(self, now, frame) -> None:
        t = now - self.started
        if self.state == "standby":
            # 4K 帧体积太大，只在基础分辨率下维护预录缓冲；
            # 切换瞬间的 4K 帧也不混入 1080p 缓冲。
            if (self.camera is None or not self.camera.high) \
                    and (frame.shape[1], frame.shape[0]) == (self.size[0], self.size[1]):
                self.buffer.append(frame)
            if now - self.last_standby >= 2.0:
                self.last_standby = now
                self._write_standby(t, frame)
            if now - self.last_status >= 2.0:
                self.last_status = now
                waiting = now - self.present_since if self.present_since is not None else 0.0
                print(f"[待机] {t:.1f}s 人数={self.last_count} 需≥{self.min_people} "
                      f"进场确认={waiting:.1f}/{self.args.enter:.1f}s", flush=True)
            return
        if self.state != "recording" or self.clip is None:
            return
        self.clip.write(frame)
        if now - self.last_disk_check >= 2.0:
            self.last_disk_check = now
            if shutil.disk_usage(self.folder).free < 2 * 1024**3:
                self.finish_group("disk_low", now)
                return
        if self.clip.elapsed() >= self.args.max_group:
            self.finish_group("max_duration", now)

    def on_detection(self, now, frame, people) -> None:
        t = now - self.started
        people = people_in_zone(people, frame.shape, self.zone)
        count = len(people)
        self.last_count = count
        if self.state == "standby":
            if count >= self.min_people:
                if self.present_since is None:
                    self.present_since = now
                    # 在进场确认的 1 秒内完成 4K 切换与编码测速，
                    # 避免真正开录时再花 2 秒错过开头。
                    if self.camera is not None and getattr(self.args, "group_4k", False) \
                            and not getattr(self.args, "video", None):
                        self.camera.enter_high_res()
                    self.log.write({"type": "showcase_present", "people": count,
                                    "need": self.min_people})
                if now - self.present_since >= self.args.enter:
                    self.start_group(t, now, count)
            else:
                if self.present_since is not None and self.camera is not None \
                        and self.camera.high:
                    # 确认阶段人数掉回门槛以下：没开录就切回待机分辨率。
                    self.camera.exit_high_res()
                    self.buffer.clear()
                self.present_since = None
            return
        if self.collector is not None:
            self.collector.update(t - self.trigger_s, frame, people)
        if count >= self.min_people:
            self.last_seen = now
            self.people_max = max(self.people_max, count)
        elif self.last_seen is not None and now - self.last_seen >= self.args.post:
            self.finish_group("person_left", now)

    def start_group(self, t, now, count) -> None:
        self.group_index += 1
        self.state = "recording"
        self.trigger_s = t
        self.people_max = count
        self.last_seen = now
        self.present_since = None
        size, fps, high = self.size, self.fps, False
        if self.camera is not None and getattr(self.args, "group_4k", False) \
                and not getattr(self.args, "video", None):
            size, fps = self.camera.enter_high_res()
            high = self.camera.high
        self.group_high_res = high
        # 预录帧先入队，ClipWriter 会把它们按顺序写到片段开头。
        buffered = [] if high else list(self.buffer)
        self.clip = ClipWriter(self.clips_dir, fps, size, self.log,
                               prefix="group", pre_roll=len(buffered))
        self.clip.start()
        for frame in buffered:
            self.clip.write(frame, copy=False)
        self.buffer.clear()
        self.collector = PoseCollector(
            self.poses_root / f"group_{self.group_index:03d}", self.log,
            self.min_people, self.args.pose_hold, self.args.pose_motion,
            self.args.pose_height_change, self.args.shot_interval,
            self.args.max_shots_per_hold)
        self.log.write({"type": "showcase_group_start", "group": self.group_index,
                        "people": count, "pre_frames": len(buffered),
                        "resolution": list(size), "record_fps": round(fps, 2),
                        "high_res": high, "trigger_s": round(t, 3)})
        print(f"[录像] 第 {self.group_index} 组进场，人数={count}，"
              f"{size[0]}x{size[1]} @ {fps:.1f}fps，预录 {len(buffered)} 帧",
              flush=True)

    def finish_group(self, reason: str, now) -> int:
        if self.state != "recording" or self.clip is None:
            return 0
        t = now - self.started
        final_path = self.clip.final_path
        clip_size, clip_fps = self.clip.size, self.clip.fps
        self.clip.finish(reason)
        self.clip = None
        if self.camera is not None and self.camera.high:
            self.camera.exit_high_res()
            self.buffer.clear()
        images = 0
        if self.collector is not None:
            self.collector.finalize(final_path)
            images = len(self.collector.images)
            if images == 0 and not self.group_high_res \
                    and final_path is not None and final_path.exists():
                self.log.write({"type": "showcase_fallback_start",
                                "video": str(final_path)})
                images = extract_from_video(final_path, self.collector.out_dir,
                                            self.detector, self.args, self.log)
                self.collector.finalize(final_path)
        meta = {
            "type": "group",
            "group": self.group_index,
            "video": final_path.name if final_path else None,
            "trigger_s": round(self.trigger_s, 3),
            "end_s": round(t, 3),
            "reason": reason,
            "people_max": self.people_max,
            "images": images,
            "resolution": list(clip_size),
            "record_fps": round(clip_fps, 2),
            "high_res": self.group_high_res,
            "pose_dir": f"poses/group_{self.group_index:03d}",
        }
        atomic_json(self.clips_dir / f"group_{self.group_index:03d}.json", meta)
        self.groups.append(meta)
        self.session["groups"] = list(self.groups)
        atomic_json(self.folder / "session.json", self.session)
        self.log.write({"type": "showcase_group_finish", **meta})
        print(f"[封装] 第 {self.group_index} 组结束（{reason}），"
              f"抽帧 {images} 张", flush=True)
        if self.on_clip is not None and final_path is not None:
            try:
                self.on_clip(final_path, f"group_{self.group_index:03d}")
            except Exception as error:  # noqa: BLE001 - 抽帧提交失败不影响录像
                self.log.write({"type": "keyframe_submit_error",
                                "file": str(final_path),
                                "reason": f"{type(error).__name__}: {error}"})
        self.state = "standby"
        self.collector = None
        self.last_seen = None
        self.people_max = 0
        self.last_standby = 0.0
        self.last_status = 0.0
        return images

    def stop(self) -> None:
        if self.state == "recording":
            self.finish_group("session_stop", time.monotonic())


def read_camera_controls(source: str, names):
    """回读相机控制实际值；读取失败返回空字典。"""
    result = subprocess.run(
        ["v4l2-ctl", "-d", source, "--get-ctrl", ",".join(names)],
        capture_output=True, text=True, check=False,
    )
    values = {}
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        value = value.strip()
        match = re.match(r"-?\d+", value)
        if match:
            values[name.strip()] = int(match.group())
    return values


def apply_camera_controls(source: str, exposure_ms: float, gain: int,
                          focus: int, brightness: int, sharpness: int):
    """设置并回读相机控制，失败必须记录，不能静默忽略。"""
    # C60E 的自动构图/数字变焦会抵消机械臂转动，必须关闭才能做视觉闭环。
    requested = {
        "zoom_absolute": 0,
        "zoom_continuous": 0,
        "power_line_frequency": 1,  # 50Hz，避免灯光条纹
        "brightness": max(0, min(255, int(brightness))),
        "sharpness": max(0, min(255, int(sharpness))),
        "focus_automatic_continuous": 0,
        "focus_absolute": max(0, min(1023, int(focus))),
    }
    # V4L2 exposure_time_absolute 的单位是 100us：10ms = 100。
    exposure_units = int(round(max(0.0, exposure_ms) * 10))
    if exposure_units > 0:
        requested["auto_exposure"] = 1
        requested["exposure_time_absolute"] = max(1, min(5000, exposure_units))
        requested["gain"] = max(0, min(100, int(gain)))
    else:
        requested["auto_exposure"] = 3
    result = subprocess.run(
        ["v4l2-ctl", "-d", source, "-c",
         ",".join(f"{name}={value}" for name, value in requested.items())],
        capture_output=True, text=True, check=False,
    )
    actual = read_camera_controls(source, list(requested))
    errors = []
    if result.returncode != 0:
        errors.append(result.stderr.strip() or result.stdout.strip())
    for name, value in requested.items():
        if name in actual and actual[name] != value:
            errors.append(f"{name} 请求 {value}，实际 {actual[name]}")
    print("相机控制: " + " ".join(f"{name}={actual.get(name, '?')}"
                                for name in requested), flush=True)
    for error in errors:
        print(f"相机控制警告: {error}", flush=True)
    return {"requested": requested, "actual": actual, "errors": errors}


def probe_exposure_response(cap, source: str, exposure_units: int):
    """用两档曝光各抓一帧比较亮度，确认手动曝光是否真的生效。"""
    def grab_mean(units):
        subprocess.run(["v4l2-ctl", "-d", source, "-c",
                        f"auto_exposure=1,exposure_time_absolute={units}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
        # 4K 取流下曝光生效有延迟，至少等 0.35 秒再取样，否则会读到上一档画面。
        deadline = time.monotonic() + 0.35
        while time.monotonic() < deadline:
            cap.read()
        ok, frame = cap.read()
        if not ok:
            return None
        return float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean())

    low = grab_mean(10)
    high = grab_mean(400)
    subprocess.run(["v4l2-ctl", "-d", source, "-c",
                    f"auto_exposure=1,exposure_time_absolute={exposure_units}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if low is None or high is None:
        return {"low": low, "high": high, "responsive": None}
    responsive = abs(high - low) / max(high, 1.0) > 0.15
    return {"low": round(low, 1), "high": round(high, 1),
            "responsive": bool(responsive)}


def open_capture(source: str, config: dict, video: bool, target_fps: float = 0.0,
                 width=None, height=None, exposure_ms: float = 0.0,
                 gain: int = 0, brightness: int = DEFAULT_BRIGHTNESS,
                 sharpness: int = DEFAULT_SHARPNESS, focus: int = DEFAULT_FOCUS):
    control_info = None
    exposure_probe = None
    if video:
        cap = cv2.VideoCapture(source)
    else:
        # 上一个进程释放设备可能有延迟，重试并同时尝试 by-id 与真实节点。
        real = os.path.realpath(source)
        deadline = time.monotonic() + 8.0
        cap = None
        while time.monotonic() < deadline:
            for candidate in (source, real):
                probe = cv2.VideoCapture(candidate, cv2.CAP_V4L2)
                if probe.isOpened():
                    cap = probe
                    break
                probe.release()
            if cap is not None:
                break
            time.sleep(0.5)
        if cap is None:
            raise SystemExit(f"无法打开视频源: {source}（已重试 8 秒）")
    if not cap.isOpened():
        raise SystemExit(f"无法打开视频源: {source}")
    if not video:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width or config["camera_width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height or config["camera_height"])
        cap.set(cv2.CAP_PROP_FPS, target_fps or config["camera_fps"])
        # C60E 只有在取流已经开始后才接受曝光/增益写入；
        # 在打开前设置会被忽略，画面仍由相机自动曝光决定。
        for _ in range(8):
            cap.read()
        control_info = apply_camera_controls(
            source, exposure_ms, gain, focus, brightness, sharpness)
        for _ in range(3):
            cap.read()
    negotiated = cap.get(cv2.CAP_PROP_FPS)
    if not math.isfinite(negotiated) or negotiated <= 0:
        negotiated = 30.0
    fps = negotiated
    if not video:
        # 协商帧率不等于实际可读帧率（MJPEG 解码与 USB 带宽都会限速），
        # 实测 1 秒，按实际速度写 MP4，避免回放变速。
        for _ in range(6):
            cap.read()
        started = time.monotonic()
        count = 0
        while time.monotonic() - started < 1.0:
            if not cap.read()[0]:
                break
            count += 1
        elapsed = max(time.monotonic() - started, 1e-3)
        measured = count / elapsed
        if measured > 1:
            fps = measured
        if exposure_ms > 0:
            exposure_probe = probe_exposure_response(
                cap, source, int(round(exposure_ms * 10)))
            if exposure_probe.get("responsive") is False:
                print(
                    "相机曝光警告: 手动曝光值回读成功但画面亮度几乎不变，"
                    "说明相机固件仍在自动曝光；运动模糊只能靠补光或放慢动作改善。",
                    flush=True,
                )
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or config["camera_width"]
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or config["camera_height"]
    suffix = "" if video else f"（协商 {negotiated:.1f} fps）"
    print(f"视频源: {source}  实际 {width}x{height} @ {fps:.1f} fps{suffix}", flush=True)
    if control_info is not None:
        control_info["exposure_probe"] = exposure_probe
    return cap, fps, (width, height), control_info


class Camera:
    """相机句柄：待机用基础分辨率，整组录像时切到 4K，结束后切回。"""

    def __init__(self, cap, fps: float, size, log: EventLog):
        self.cap = cap
        self.fps = fps
        self.size = size
        # 录制分辨率；4K 原图模式下取流是 4K，视频仍按 1080p 录制。
        self.video_size = size
        self.base_fps = fps
        self.base_size = size
        self.log = log
        self.high = False
        self.high_fps = None

    def read(self):
        return self.cap.read()

    def release(self) -> None:
        self.cap.release()

    def _reconfig(self, width, height, target_fps):
        """切换分辨率后必须确认实际帧尺寸，并丢弃过渡期的旧尺寸帧。"""
        for _ in range(3):
            self.cap.read()
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, target_fps)
        deadline = time.monotonic() + 2.0
        dropped, frame_size = 0, None
        while time.monotonic() < deadline:
            ok, frame = self.cap.read()
            if not ok:
                break
            frame_size = (frame.shape[1], frame.shape[0])
            if frame_size == (int(width), int(height)):
                break
            dropped += 1
            time.sleep(0.01)
        if frame_size is None:
            frame_size = (int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                          int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        negotiated = self.cap.get(cv2.CAP_PROP_FPS)
        if not math.isfinite(negotiated) or negotiated <= 0:
            negotiated = target_fps
        if dropped:
            self.log.write({"type": "camera_reconfig_discard", "requested": [width, height],
                            "actual": list(frame_size), "discarded": dropped})
            print(f"[相机] 切换 {width}x{height} 丢弃 {dropped} 帧过渡画面", flush=True)
        if frame_size != (int(width), int(height)):
            self.log.write({"type": "camera_reconfig_mismatch",
                            "requested": [width, height], "actual": list(frame_size)})
            print(f"[相机] 切换失败：请求 {width}x{height}，实际 "
                  f"{frame_size[0]}x{frame_size[1]}", flush=True)
        return frame_size, negotiated

    def _measure_encode_fps(self, size, target_fps) -> float:
        """用真实 4K 帧测一次 mp4v 可持续写入帧率，避免回放变速。"""
        temp = Path(tempfile.gettempdir()) / f"r1-camera-calib-{os.getpid()}.mp4"
        writer = cv2.VideoWriter(str(temp), cv2.VideoWriter_fourcc(*"mp4v"),
                                 target_fps, size)
        count = 0
        started = time.monotonic()
        try:
            while count < 8:
                ok, frame = self.cap.read()
                if not ok:
                    break
                if (frame.shape[1], frame.shape[0]) != size:
                    frame = cv2.resize(frame, size)
                writer.write(frame)
                count += 1
            elapsed = time.monotonic() - started
        finally:
            writer.release()
            try:
                temp.unlink()
            except OSError:
                pass
        if count == 0 or elapsed <= 0:
            return target_fps
        return max(4.0, min(target_fps, count / elapsed))

    def enter_high_res(self, width=3840, height=2160, target_fps=30.0):
        if self.high:
            return self.size, self.fps
        size, negotiated = self._reconfig(width, height, target_fps)
        if size[0] < width * 0.9 or size[1] < height * 0.9:
            self._reconfig(self.base_size[0], self.base_size[1], self.base_fps)
            self.log.write({"type": "camera_high_res_unsupported",
                            "requested": [width, height], "actual": list(size)})
            return self.base_size, self.base_fps
        if self.high_fps is None:
            self.high_fps = self._measure_encode_fps(size, negotiated)
        self.high = True
        self.size = size
        self.fps = self.high_fps
        self.log.write({"type": "camera_high_res", "size": list(size),
                        "capture_fps": round(negotiated, 2),
                        "record_fps": round(self.fps, 2)})
        print(f"[4K] 切换 {size[0]}x{size[1]}，录制约 {self.fps:.1f} fps", flush=True)
        return self.size, self.fps

    def exit_high_res(self):
        if not self.high:
            return self.size, self.fps
        self._reconfig(self.base_size[0], self.base_size[1], self.base_fps)
        self.high = False
        self.size = self.base_size
        self.fps = self.base_fps
        self.log.write({"type": "camera_base_res", "size": list(self.size),
                        "fps": round(self.fps, 2)})
        print(f"[1080p] 切回 {self.size[0]}x{self.size[1]} @ {self.fps:.1f} fps",
              flush=True)
        return self.size, self.fps


def vlm_worker(vlm: VLM, requests: Queue, results: Queue, stop: threading.Event):
    while not stop.is_set():
        try:
            captured_at, frames, count, candidates = requests.get(timeout=0.2)
        except Empty:
            continue
        try:
            audit, _ = vlm.classify(frames, count, candidates)
        except Exception as error:  # noqa: BLE001 - 记录后继续
            audit = {"error": f"{type(error).__name__}: {error}"}
        results.put((captured_at, count, audit, tuple(candidates)))


def fallback_observation(count: int) -> dict:
    if count == 0:
        scene = "empty"
    elif count == 1:
        scene = "single_presenter"
    elif count == 2:
        scene = "two_conversation"
    else:
        scene = "group_discussion"
    return {
        "scene": scene,
        "people": count,
        "confidence": 1.0,
        "evidence": "YOLO 人数回退，未使用视觉模型",
    }


def atomic_json(path: Path, value: dict) -> None:
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(pending, path)


def measure_loop_fps(camera, detector, detect_interval, seconds=2.0,
                     still_interval=0.0, still_quality=DEFAULT_STILL_QUALITY) -> float:
    """按真实主循环负载（取流+缩放+检测+存 4K 原图）测可持续帧率。"""
    for _ in range(4):
        camera.read()
    started = time.monotonic()
    count, last_detect, last_still = 0, 0.0, 0.0
    while time.monotonic() - started < seconds:
        ok, full = camera.read()
        if not ok:
            break
        frame = full
        if (full.shape[1], full.shape[0]) != tuple(camera.video_size):
            frame = cv2.resize(full, tuple(camera.video_size),
                               interpolation=cv2.INTER_AREA)
        now = time.monotonic()
        if now - last_detect >= detect_interval:
            detector.detect(frame)
            last_detect = now
        if still_interval > 0 and now - last_still >= still_interval:
            cv2.imencode(".jpg", full, [cv2.IMWRITE_JPEG_QUALITY, still_quality])
            last_still = now
        count += 1
    elapsed = max(time.monotonic() - started, 1e-3)
    return count / elapsed if count else camera.fps


def run(args) -> int:
    config = json.loads(Path(args.config).read_text())
    socket_path = args.socket or config.get("api_socket") or DEFAULT_SOCKET

    try:
        arm_status = pose.Arm(socket_path)
    except OSError as error:
        raise SystemExit(
            f"无法连接 r1_jog API ({socket_path}): {error}\n"
            "请先启动 ./run.sh --api /tmp/r1_jog.sock，并按 Enter 使能 FOC。"
        )
    try:
        status = arm_status.call("status")
        if not status.get("ok"):
            raise SystemExit("r1_jog 状态查询失败")
        if status.get("dry_run") and not args.allow_dry_run:
            raise SystemExit("r1_jog 处于 dry-run；仅流程验证请加 --allow-dry-run")
        base_q = status["q_meas_rad"]
        print(
            "固定基准关节角: " + " ".join(f"{value:+.4f}" for value in base_q),
            flush=True,
        )
    finally:
        arm_status.close()

    source = args.video
    if not source:
        cameras = sorted(glob.glob("/dev/v4l/by-id/*C60E*video-index0"))
        source = args.camera or (cameras[0] if cameras else None)
    if source is None:
        raise SystemExit("未找到 C60E；用 --camera 或 --video 指定视频源")

    folder = (
        Path(args.output)
        if args.output
        else DEFAULT_OUTPUT_ROOT
        / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-director-capture"
    )
    clips_dir = folder / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(folder).free < 2 * 1024**3:
        raise SystemExit("磁盘可用空间不足 2 GiB")

    log = EventLog(folder / "events.jsonl")
    log.write(
        {
            "type": "session_start",
            "source": source,
            "socket": socket_path,
            "base_q": base_q,
            "clip_seconds": args.clip_seconds,
            "sweep_deg": args.sweep_deg,
            "wide_deg": args.wide_deg,
            "track_seconds": args.track_seconds,
            "vlm": not args.no_vlm,
        }
    )
    session = {
        "mode": "director-capture",
        "source": source,
        "socket": socket_path,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "session_dir": str(folder),
        "base_q_rad": base_q,
        "clip_seconds": args.clip_seconds,
        "sweep_deg": args.sweep_deg,
        "wide_deg": args.wide_deg,
        "track_seconds": args.track_seconds,
        "vlm": not args.no_vlm,
        "camera_fps": args.fps,
        "showcase": not args.no_showcase,
        "showcase_params": {
            "min_people": args.min_people,
            "enter_s": args.enter,
            "pre_s": args.pre,
            "post_s": args.post,
            "max_group_s": args.max_group,
            "pose_hold_s": args.pose_hold,
            "shot_interval_s": args.shot_interval,
        },
        "clips": [],
        "groups": [],
        "decisions": 0,
    }
    atomic_json(folder / "session.json", session)

    stills_4k = bool(getattr(args, "stills_4k", True)) and not args.video
    capture_width = STILL_SIZE[0] if stills_4k else args.width
    capture_height = STILL_SIZE[1] if stills_4k else args.height
    cap, fps, size, camera_controls = open_capture(
        source, config, bool(args.video), args.fps,
        capture_width, capture_height, args.exposure_ms, args.gain,
        args.brightness, args.sharpness, args.focus)
    camera = Camera(cap, fps, size, log)
    video_size = VIDEO_SIZE if stills_4k else size
    camera.video_size = video_size
    session.update(camera_fps=round(fps, 2), camera_size=list(size),
                   video_size=list(video_size), stills_4k=stills_4k,
                   exposure_ms=args.exposure_ms, gain=args.gain,
                   brightness=args.brightness, sharpness=args.sharpness,
                   focus=args.focus, camera_controls=camera_controls,
                   group_4k=bool(args.group_4k))
    log.write({"type": "camera_controls", "controls": camera_controls,
               "stills_4k": stills_4k, "capture_size": list(size),
               "video_size": list(video_size)})
    atomic_json(folder / "session.json", session)
    detector = PersonDetector(config)
    record_fps = measure_loop_fps(
        camera, detector, args.detect_interval, 2.0,
        args.still_interval if stills_4k else 0.0, args.still_quality)
    camera.record_fps = record_fps
    print(f"主循环实测 {record_fps:.1f} fps，视频按该帧率录制", flush=True)
    session.update(record_fps=round(record_fps, 2))
    atomic_json(folder / "session.json", session)
    vlm = None if args.no_vlm else VLM(config)
    director = Director(
        confirmations=config["confirmations"],
        cooldown_s=config["cooldown_s"],
        max_age_s=config["max_observation_age_s"],
        quiet_after_s=config.get("quiet_after_action_s", 0),
    )
    vision_state = VisionState()
    mover = ArmMover(socket_path, base_q, log, vision_state, args.track_seconds)
    keyframes = None if args.no_keyframes else KeyframeWorker(folder, args, log)
    stills = None if not stills_4k else StillSaver(
        folder, args.still_interval, args.still_quality, log)
    clip = ClipWriter(clips_dir, record_fps, video_size, log,
                      on_finish=keyframes.submit if keyframes else None)
    showcase = None
    if not args.no_showcase:
        showcase = Showcase(folder, clips_dir, log, detector, args,
                            record_fps, video_size,
                            session, on_clip=keyframes.submit if keyframes else None,
                            camera=camera)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    vlm_requests: Queue = Queue(maxsize=1)
    vlm_results: Queue = Queue()
    vlm_thread = None
    if vlm is not None:
        vlm_thread = threading.Thread(
            target=vlm_worker, args=(vlm, vlm_requests, vlm_results, stop), daemon=True
        )
        vlm_thread.start()

    samples = deque(maxlen=3)
    last_sample = 0.0
    last_submit = 0.0
    last_fallback = 0.0
    last_detect = 0.0
    last_headroom = 0.0
    frame_period = 1.0 / fps if fps > 0 else 1.0 / 30.0
    next_frame = time.monotonic()
    last_seen = 0.0
    present_since = None
    people = []
    detected = 0
    preview_saved = False
    started = time.monotonic()
    decisions = 0
    last_action_name = None
    last_action_scene = None
    last_action_people = None
    last_action_hash = None
    read_frames = 0
    last_stats = started
    stats_frames = 0

    try:
        while not stop.is_set():
            if args.duration > 0 and time.monotonic() - started >= args.duration:
                break
            ok, full = camera.read()
            if not ok:
                log.write({"type": "source_error", "reason": "视频源结束或取流中断"})
                break
            if (full.shape[1], full.shape[0]) == tuple(video_size):
                frame = full
            else:
                # 4K 原图模式：视频、检测、VLM 都用 1080p，4K 帧只用于存原图。
                frame = cv2.resize(full, tuple(video_size),
                                   interpolation=cv2.INTER_AREA)
            read_frames += 1
            now = time.monotonic()
            if not args.video and now - last_stats >= 10.0:
                window = now - last_stats
                log.write({"type": "capture_stats",
                           "read_frames": read_frames,
                           "read_fps": round(read_frames / (now - started), 2),
                           "window_fps": round((read_frames - stats_frames) / window, 2),
                           "elapsed_s": round(now - started, 1)})
                stats_frames = read_frames
                last_stats = now
            if args.video:
                next_frame += frame_period
                delay = next_frame - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_frame = time.monotonic()

            if not preview_saved:
                cv2.imwrite(str(folder / "preview.jpg"), frame)
                preview_saved = True

            if now - last_detect >= args.detect_interval:
                people = detector.detect(frame)
                detected = len(people)
                last_detect = now
                vision_state.update(people, frame.shape)
                if showcase is not None:
                    showcase.on_detection(now, frame, people)
                    if showcase.recording and clip.active():
                        # 多人整段录像接管写盘，避免同一画面重复保存。
                        clip.finish("group_recording")
            if showcase is not None:
                showcase.on_frame(now, frame)

            # 摆拍/整段录像期间，头顶贴边时小幅上抬 J4，保证每个人头部完整。
            if (showcase is not None and showcase.recording and people
                    and now - last_headroom >= HEADROOM_INTERVAL_S
                    and not mover.busy()):
                top = head_top_ratio(people, frame.shape)
                if top is not None and top < HEADROOM_MARGIN:
                    last_headroom = now
                    mover.add_headroom_tilt()

            if detected > 0:
                last_seen = now
                if present_since is None:
                    present_since = now
            elif present_since is not None and now - last_seen >= args.post:
                present_since = None
            if present_since is None and detected == 0:
                # 确认无人后允许下一次相同场景重新触发。
                last_action_name = None
                last_action_hash = None
                if mover.away and not mover.busy():
                    # 跟踪改变了机位，人离开后回到本次会话的基准姿态。
                    mover.return_to_base()

            far = False
            if people:
                frame_h = frame.shape[0]
                far = max(
                    (float(p["xyxy"][3]) - float(p["xyxy"][1])) for p in people
                ) / frame_h < FAR_BOX_HEIGHT
            if far and mover.away and not mover.busy():
                # 人退远后框明显变小，收回抬起的机位，回到会话基准构图。
                mover.return_to_base()

            if clip.active():
                clip.write(frame)
                if clip.elapsed() >= args.clip_seconds:
                    # 安全上限；正常情况下由动作完成触发结束。
                    clip.finish("max_duration_safety")
                    session["clips"] = sorted(
                        path.name for path in clips_dir.glob("person_*.mp4")
                    )
                    atomic_json(folder / "session.json", session)

            if now - last_sample >= 0.5:
                samples.append((now, frame.copy()))
                last_sample = now

            if (
                vlm is not None
                and detected > 0
                and len(samples) >= 3
                and now - last_submit >= config["decision_interval_s"]
                and vlm_requests.empty()
            ):
                frames = [item[1] for item in samples]
                first_people = detector.detect(frames[0])
                candidates = movement_candidates(
                    first_people, people, frames[0].shape, frames[-1].shape
                )
                vlm_requests.put((now, frames, detected, candidates))
                last_submit = now

            while not vlm_results.empty():
                captured_at, count, audit, candidates = vlm_results.get()
                observation = audit.get("observation") or {}
                scene = observation.get("scene")
                if count >= 1 and scene in (None, "uncertain", "occluded", "empty"):
                    # 画面被裁切或模型不确定时，用 YOLO 人数保证双人/多人仍能触发运镜。
                    observation = fallback_observation(count)
                    audit["fallback_reason"] = (
                        f"视觉模型 scene={scene}，按 {count} 人回退"
                    )
                # 起立/走位以 YOLO 检测框的几何变化为准：模型即使仍选了静态单人，
                # 只要连续帧的框确实变高或横移，就直接采用动态场景。
                for label, target in (
                    ("起立", "sit_to_stand"),
                    ("向左走", "walking_left"),
                    ("向右走", "walking_right"),
                ):
                    if label in candidates and count >= 1:
                        observation = {
                            "scene": target,
                            "people": count,
                            "evidence": f"YOLO 检测框变化：{label}",
                        }
                        audit["yolo_override"] = label
                        break
                result = director.decide(
                    observation, time.monotonic(), captured_at, detector_count=count
                )
                decisions += 1
                event = {
                    "type": "decision",
                    "sequence": decisions,
                    "captured_age_s": time.monotonic() - captured_at,
                    "people": count,
                    "decision": result,
                    "vision": audit,
                }
                log.write(event)
                print(
                    json.dumps(
                        {
                            "decision": decisions,
                            "action": result.get("action"),
                            "status": result.get("status"),
                            "scene": result.get("scene"),
                            "people": count,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                action = result.get("action")
                skill = choose_skill(action, last_action_name)
                if result.get("status") == "ready" and skill is not None:
                    scene = result.get("scene")
                    frame_hash = ahash(frame)
                    same_scene = (
                        last_action_name == skill
                        and last_action_scene == scene
                        and last_action_people == count
                    )
                    if same_scene:
                        log.write(
                            {
                                "type": "action_suppressed",
                                "action": skill,
                                "rule_action": action,
                                "reason": "same_scene",
                                "scene": scene,
                                "people": count,
                                "hash_distance": (
                                    hash_distance(frame_hash, last_action_hash)
                                    if last_action_hash is not None
                                    else None
                                ),
                            }
                        )
                    elif mover.start(skill, args.sweep_deg, args.wide_deg,
                                     args.motion_seconds, args.friction_nm):
                        if showcase is None or not showcase.recording:
                            if clip.active():
                                clip.finish("action_replaced")
                            clip.start()
                        director.started(time.monotonic(), mover.duration)
                        last_action_name = skill
                        last_action_scene = scene
                        last_action_people = count
                        last_action_hash = frame_hash
                        log.write(
                            {
                                "type": "action_accepted",
                                "action": skill,
                                "rule_action": action,
                                "duration_s": mover.duration,
                                "mode": "traj_j1" if skill in (
                                    "ConversationSweep", "GroupWide",
                                    "FollowLeft", "FollowRight") else "goto_pose",
                            }
                        )
                elif result.get("status") == "ready":
                    # 非运动镜头只做节流，避免每 2 秒重复写同一条判断。
                    director.started(time.monotonic(), 5.0)

            if (
                vlm is not None
                and detected == 0
                and now - last_fallback >= config["decision_interval_s"]
            ):
                last_fallback = now
                director.decide(
                    fallback_observation(0), now, now, detector_count=0
                )

            if (
                vlm is None
                and now - last_fallback >= config["decision_interval_s"]
            ):
                # 无视觉模型时用人数回退，仍然遵守连续确认和冷却。
                last_fallback = now
                result = director.decide(
                    fallback_observation(detected),
                    time.monotonic(),
                    now,
                    detector_count=detected,
                )
                if result.get("status") == "ready":
                    action = result.get("action")
                    decisions += 1
                    log.write(
                        {
                            "type": "decision",
                            "sequence": decisions,
                            "people": detected,
                            "decision": result,
                            "fallback": True,
                        }
                    )
                    skill = choose_skill(action, last_action_name)
                    if skill is not None:
                        scene = result.get("scene")
                        frame_hash = ahash(frame)
                        same_scene = (
                            last_action_name == skill
                            and last_action_scene == scene
                            and last_action_people == detected
                        )
                        if same_scene:
                            log.write(
                                {
                                    "type": "action_suppressed",
                                    "action": skill,
                                    "rule_action": action,
                                    "reason": "same_scene",
                                    "scene": scene,
                                    "people": detected,
                                    "fallback": True,
                                }
                            )
                        elif mover.start(skill, args.sweep_deg, args.wide_deg,
                                         args.motion_seconds, args.friction_nm):
                            if showcase is None or not showcase.recording:
                                if clip.active():
                                    clip.finish("action_replaced")
                                clip.start()
                            director.started(time.monotonic(), mover.duration)
                            last_action_name = skill
                            last_action_scene = scene
                            last_action_people = detected
                            last_action_hash = frame_hash
                            log.write(
                                {
                                    "type": "action_accepted",
                                    "action": skill,
                                    "rule_action": action,
                                    "duration_s": mover.duration,
                                    "fallback": True,
                                }
                            )
                    else:
                        director.started(time.monotonic(), 5.0)

            finished = mover.poll()
            if finished is not None:
                director.finished(time.monotonic())
                if clip.active():
                    clip.finish("action_done")
                    session["clips"] = sorted(
                        path.name for path in clips_dir.glob("person_*.mp4")
                    )
                    atomic_json(folder / "session.json", session)

            # 4K 原图：跟随当前正在录制的片段，按间隔保存到同名目录。
            if stills is not None:
                active_clip = None
                if clip.active():
                    active_clip = clip
                elif (showcase is not None and showcase.recording
                      and showcase.clip is not None and showcase.clip.active()):
                    active_clip = showcase.clip
                if active_clip is not None:
                    if stills.name != active_clip.name:
                        stills.start(active_clip.name)
                    stills.maybe_save(active_clip.position(), full)
                elif stills.name is not None:
                    stills.finish()
            session["decisions"] = decisions
            time.sleep(0.001)
    finally:
        stop.set()
        if stills is not None:
            stills.finish()
        clip.finish("session_stop")
        if showcase is not None:
            showcase.stop()
        if vlm_thread is not None:
            vlm_thread.join(timeout=3)
        mover.return_to_base()
        camera.release()
        detector.close()
        session["clips"] = sorted(path.name for path in clips_dir.glob("person_*.mp4"))
        session["group_clips"] = sorted(path.name for path in clips_dir.glob("group_*.mp4"))
        keyframe_root = folder / "keyframes"
        session["keyframe_dirs"] = sorted(
            path.name for path in keyframe_root.glob("*") if path.is_dir()
        ) if keyframe_root.is_dir() else []
        session["keyframe_pending"] = keyframes.pending() if keyframes else 0
        session["group_4k_fps"] = round(camera.high_fps, 2) if camera.high_fps else None
        session["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        session["elapsed_s"] = time.monotonic() - started
        session["read_frames"] = read_frames
        session["measured_read_fps"] = (
            round(read_frames / session["elapsed_s"], 2)
            if session["elapsed_s"] > 0 else 0
        )
        atomic_json(folder / "session.json", session)
        log.write({"type": "session_stop", "clips": session["clips"]})
        print(f"会话结束: {folder}", flush=True)
        print(f"片段: {session['clips']}", flush=True)
    return 0


def run_extract(args) -> int:
    """只对现成视频抽帧，不连接机械臂、不打开相机。"""
    if not args.video:
        raise SystemExit("--extract-only 需要 --video 指定视频文件")
    video = Path(args.video)
    if not video.is_file():
        raise SystemExit(f"视频文件不存在: {video}")
    config = json.loads(Path(args.config).read_text())
    folder = (
        Path(args.output)
        if args.output
        else DEFAULT_OUTPUT_ROOT
        / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-pose-extract"
    )
    folder.mkdir(parents=True, exist_ok=True)
    log = EventLog(folder / "events.jsonl")
    detector = PersonDetector(config)
    try:
        count = extract_from_video(video, folder, detector, args, log)
    finally:
        detector.close()
    print(f"抽帧完成: {count} 张，目录 {folder}", flush=True)
    return 0


def run_keyframes_only(args) -> int:
    """只对现成视频筛选动作/微笑关键帧，不连机械臂、不打开相机。"""
    if not args.video:
        raise SystemExit("--keyframes-only 需要 --video 指定视频文件")
    video = Path(args.video)
    if not video.is_file():
        raise SystemExit(f"视频文件不存在: {video}")
    folder = (
        Path(args.output)
        if args.output
        else DEFAULT_OUTPUT_ROOT
        / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-keyframes"
    )
    folder.mkdir(parents=True, exist_ok=True)
    log = EventLog(folder / "events.jsonl")
    options = argparse.Namespace(
        model=args.keyframe_model,
        config=args.config,
        count=args.keyframe_count,
        step=args.keyframe_step,
        min_gap=args.keyframe_min_gap,
        action_threshold=args.keyframe_action,
        min_sharpness=args.keyframe_min_sharpness,
        max_hand_speed=args.keyframe_max_hand_speed,
        max_head_speed=args.keyframe_max_head_speed,
        vlm_max=0 if args.no_keyframe_vlm else args.keyframe_vlm_max,
        vlm=not args.no_keyframe_vlm,
        overview=str(folder.parent),
        jpeg_quality=DEFAULT_JPEG_QUALITY,
        stills=None,
        still_tolerance=0.4,
    )
    manifest = keyframes.extract_keyframes(video, folder, options, log=log.write)
    print(f"关键帧 {len(manifest['images'])} 张 → {folder}", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="人物触发录像 + 导演规则 + J1 运镜")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--camera", help="C60E 设备路径")
    source.add_argument("--video", help="测试用视频文件")
    parser.add_argument("--socket", help="r1_jog socket，默认 /tmp/r1_jog.sock")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output", help="会话目录")
    parser.add_argument("--clip-seconds", type=float, default=DEFAULT_CLIP_SECONDS,
                        help="单段安全上限秒数；动作完成时提前结束")
    parser.add_argument("--duration", type=float, default=0.0, help="总运行秒数，0 为直到 Ctrl+C")
    parser.add_argument("--enter", type=float, default=DEFAULT_ENTER_S,
                        help="多人进场确认秒数")
    parser.add_argument("--post", type=float, default=DEFAULT_POST_S,
                        help="人数不足后结束整段录像的延时秒数")
    parser.add_argument("--pre", type=float, default=DEFAULT_PRE_S,
                        help="进场前预录秒数（仅多人整段录像）")
    parser.add_argument("--detect-interval", type=float, default=0.13)
    parser.add_argument("--sweep-deg", type=float, default=DEFAULT_SWEEP_DEG)
    parser.add_argument("--wide-deg", type=float, default=DEFAULT_WIDE_DEG)
    parser.add_argument("--track-seconds", type=float, default=15.0,
                        help="单次视觉跟踪的最长持续时间")
    parser.add_argument("--motion-seconds", type=float, default=0.0,
                        help="覆盖扫拍时长，0 为按动作自动选择")
    parser.add_argument("--friction-nm", type=float, default=-1.0,
                        help="J1 摩擦力矩 Nm；-1 使用 r1_jog 配置")
    parser.add_argument("--no-vlm", action="store_true", help="只用 YOLO 人数回退")
    parser.add_argument("--allow-dry-run", action="store_true")
    parser.add_argument("--no-showcase", action="store_true",
                        help="关闭多人进场整段录像与摆拍抽帧")
    parser.add_argument("--no-keyframes", action="store_true",
                        help="关闭每段录像的动作/微笑关键帧筛选")
    parser.add_argument("--group-4k", dest="group_4k", action="store_true",
                        help="多人整组录像切到 4K（实测 mp4v 只有约 7fps，"
                             "且 4K 相对 1080p 几乎不增加细节，默认关闭）")
    parser.set_defaults(group_4k=False)
    parser.add_argument("--keyframes-only", action="store_true",
                        help="只对 --video 筛选关键帧，不启动导演主循环")
    parser.add_argument("--keyframe-count", type=int, default=DEFAULT_KEYFRAME_COUNT,
                        help="每段录像最多保存的关键帧数量")
    parser.add_argument("--keyframe-step", type=float,
                        default=DEFAULT_KEYFRAME_STEP_S,
                        help="关键帧扫描采样间隔秒数")
    parser.add_argument("--keyframe-min-gap", type=float,
                        default=DEFAULT_KEYFRAME_MIN_GAP_S,
                        help="两张关键帧的最小时间间隔秒数")
    parser.add_argument("--keyframe-action", type=float,
                        default=DEFAULT_KEYFRAME_ACTION,
                        help="姿态动作分阈值")
    parser.add_argument("--keyframe-vlm-max", type=int,
                        default=DEFAULT_KEYFRAME_VLM_MAX,
                        help="送 VLM 确认的候选帧上限")
    parser.add_argument("--keyframe-min-sharpness", type=float,
                        default=DEFAULT_KEYFRAME_MIN_SHARPNESS,
                        help="逐人脸清晰度下限（人脸缩到不超过 240px 后测），"
                             "每张可见人脸都必须达标")
    parser.add_argument("--keyframe-max-hand-speed", type=float,
                        default=DEFAULT_KEYFRAME_MAX_HAND_SPEED,
                        help="手腕相对肩部速度上限（肩宽/秒），超过视为挥手拖影")
    parser.add_argument("--keyframe-max-head-speed", type=float,
                        default=DEFAULT_KEYFRAME_MAX_HEAD_SPEED,
                        help="头部相对肩部速度上限（肩宽/秒），超过视为转头拖影")
    parser.add_argument("--no-keyframe-vlm", action="store_true",
                        help="关键帧只用姿态动作判定，不调用 MiniCPM-V")
    parser.add_argument("--keyframe-model",
                        help="yolov8s-pose.cix 路径，默认自动查找板上已有模型")
    parser.add_argument("--extract-only", action="store_true",
                        help="只对 --video 抽帧，不启动导演主循环")
    parser.add_argument("--min-people", type=int, default=DEFAULT_MIN_PEOPLE,
                        help="多人进场触发门槛，默认 2")
    parser.add_argument("--zone", default="0,0,1,1",
                        help="进场触发区域 左,上,右,下，默认全画面")
    parser.add_argument("--fps", type=float, default=30.0,
                        help="相机取流帧率，默认 30；4K 用 30，1080p 可用 60"
                             "（60 模式录像约 33fps，会变速，见 README）")
    parser.add_argument("--width", type=int,
                        help="相机取流宽度，默认 director/config.json（4K 用 3840）")
    parser.add_argument("--height", type=int,
                        help="相机取流高度，默认 director/config.json（4K 用 2160）")
    parser.add_argument("--exposure-ms", type=float, default=10.0,
                        help="手动曝光毫秒，默认 10ms 抑制运动模糊；0 为自动曝光")
    parser.add_argument("--gain", type=int, default=80,
                        help="手动曝光时的增益 0..100，默认 80（传 0 会真正写 0）")
    parser.add_argument("--brightness", type=int, default=DEFAULT_BRIGHTNESS,
                        help="相机 brightness，默认 165；177 以上会截断白墙高光")
    parser.add_argument("--sharpness", type=int, default=DEFAULT_SHARPNESS,
                        help="相机 sharpness，默认 128，不上调以免放大颗粒")
    parser.add_argument("--focus", type=int, default=DEFAULT_FOCUS,
                        help="固定对焦位置 0..1023，默认 300（实测约 2m 合焦）")
    parser.add_argument("--no-stills-4k", dest="stills_4k", action="store_false",
                        help="关闭 4K 原图模式（该模式相机按 4K 取流，"
                             "录 1080p 视频并存 4K 原图供关键帧使用）；默认开启")
    parser.add_argument("--still-interval", type=float,
                        default=DEFAULT_STILL_INTERVAL_S,
                        help="4K 原图保存间隔秒数，默认 0.5")
    parser.add_argument("--still-quality", type=int, default=DEFAULT_STILL_QUALITY,
                        help="4K 原图 JPEG 质量，默认 92")
    parser.set_defaults(stills_4k=True)
    parser.add_argument("--max-group", type=float, default=DEFAULT_MAX_GROUP_S,
                        help="单组整段录像的安全上限秒数")
    parser.add_argument("--pose-hold", type=float, default=DEFAULT_POSE_HOLD_S,
                        help="多人静止多久判定为摆拍")
    parser.add_argument("--pose-motion", type=float, default=DEFAULT_POSE_MOTION,
                        help="摆拍判定的归一化位移阈值")
    parser.add_argument("--pose-height-change", type=float,
                        default=DEFAULT_POSE_HEIGHT_CHANGE,
                        help="摆拍判定的人体框高度变化阈值")
    parser.add_argument("--shot-interval", type=float, default=DEFAULT_SHOT_INTERVAL_S,
                        help="同一摆拍区间内的抽帧间隔秒数")
    parser.add_argument("--max-shots-per-hold", type=int,
                        default=DEFAULT_MAX_SHOTS_PER_HOLD,
                        help="同一摆拍区间最多抽帧数")
    parser.add_argument("--fallback-count", type=int, default=DEFAULT_FALLBACK_COUNT,
                        help="没有摆拍区间时兜底抽帧数量")
    parser.add_argument("--fallback-step", type=float, default=DEFAULT_FALLBACK_STEP_S,
                        help="兜底抽帧的采样间隔秒数")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.extract_only:
        return run_extract(args)
    if args.keyframes_only:
        if not 1 <= args.keyframe_count <= 30:
            raise SystemExit("--keyframe-count 需要在 1..30 之间")
        if not 0.1 <= args.keyframe_step <= 5:
            raise SystemExit("--keyframe-step 需要在 0.1..5 秒之间")
        if not 0.2 <= args.keyframe_min_gap <= 30:
            raise SystemExit("--keyframe-min-gap 需要在 0.2..30 秒之间")
        if not 0.1 <= args.keyframe_action <= 1:
            raise SystemExit("--keyframe-action 需要在 0.1..1 之间")
        if not 0 <= args.keyframe_vlm_max <= 60:
            raise SystemExit("--keyframe-vlm-max 需要在 0..60 之间")
        if not 0 <= args.keyframe_min_sharpness <= 5000:
            raise SystemExit("--keyframe-min-sharpness 需要在 0..5000 之间")
        if not 0.1 <= args.keyframe_max_head_speed <= 10:
            raise SystemExit("--keyframe-max-head-speed 需要在 0.1..10 之间")
        return run_keyframes_only(args)
    if not 10 <= args.clip_seconds <= 120:
        raise SystemExit("--clip-seconds 需要在 10..120 之间")
    if not 0 <= args.sweep_deg <= 15 or not 0 <= args.wide_deg <= 15:
        raise SystemExit("扫拍角度需要在 0..15 度之间")
    if not 3 <= args.track_seconds <= 60:
        raise SystemExit("--track-seconds 需要在 3..60 之间")
    if args.min_people < 1:
        raise SystemExit("--min-people 需要大于等于 1")
    if not 0 <= args.enter <= 5 or not 0 <= args.pre <= 10 or not 0 <= args.post <= 30:
        raise SystemExit("--enter/--pre/--post 超出允许范围")
    if not 0.001 <= args.pose_motion <= 0.2:
        raise SystemExit("--pose-motion 需要在 0.001..0.2 之间")
    if not 0.002 <= args.pose_height_change <= 0.5:
        raise SystemExit("--pose-height-change 需要在 0.002..0.5 之间")
    if not 10 <= args.max_group <= 600:
        raise SystemExit("--max-group 需要在 10..600 秒之间")
    if not 5 <= args.fps <= 60:
        raise SystemExit("--fps 需要在 5..60 之间")
    if args.width is not None and not 640 <= args.width <= 3840:
        raise SystemExit("--width 需要在 640..3840 之间")
    if args.height is not None and not 480 <= args.height <= 2160:
        raise SystemExit("--height 需要在 480..2160 之间")
    if not 0 <= args.exposure_ms <= 50:
        raise SystemExit("--exposure-ms 需要在 0..50 之间")
    if not 0 <= args.gain <= 100:
        raise SystemExit("--gain 需要在 0..100 之间")
    if not 0 <= args.brightness <= 255:
        raise SystemExit("--brightness 需要在 0..255 之间")
    if not 0 <= args.sharpness <= 255:
        raise SystemExit("--sharpness 需要在 0..255 之间")
    if not 0 <= args.focus <= 1023:
        raise SystemExit("--focus 需要在 0..1023 之间")
    if not 0.1 <= args.still_interval <= 5:
        raise SystemExit("--still-interval 需要在 0.1..5 秒之间")
    if not 60 <= args.still_quality <= 100:
        raise SystemExit("--still-quality 需要在 60..100 之间")
    if args.stills_4k and not args.video:
        # 4K 原图模式已经按 4K 取流，整组录像不再切分辨率。
        args.group_4k = False
    if not 0.2 <= args.pose_hold <= 10:
        raise SystemExit("--pose-hold 需要在 0.2..10 秒之间")
    if not 0.5 <= args.shot_interval <= 30:
        raise SystemExit("--shot-interval 需要在 0.5..30 秒之间")
    if not 1 <= args.max_shots_per_hold <= 20:
        raise SystemExit("--max-shots-per-hold 需要在 1..20 之间")
    if not 1 <= args.fallback_count <= 20:
        raise SystemExit("--fallback-count 需要在 1..20 之间")
    if not 0.5 <= args.fallback_step <= 30:
        raise SystemExit("--fallback-step 需要在 0.5..30 秒之间")
    if not 1 <= args.keyframe_count <= 30:
        raise SystemExit("--keyframe-count 需要在 1..30 之间")
    if not 0.1 <= args.keyframe_step <= 5:
        raise SystemExit("--keyframe-step 需要在 0.1..5 秒之间")
    if not 0.2 <= args.keyframe_min_gap <= 30:
        raise SystemExit("--keyframe-min-gap 需要在 0.2..30 秒之间")
    if not 0.1 <= args.keyframe_action <= 1:
        raise SystemExit("--keyframe-action 需要在 0.1..1 之间")
    if not 0 <= args.keyframe_vlm_max <= 60:
        raise SystemExit("--keyframe-vlm-max 需要在 0..60 之间")
    if not 0 <= args.keyframe_min_sharpness <= 5000:
        raise SystemExit("--keyframe-min-sharpness 需要在 0..5000 之间")
    if not 0.1 <= args.keyframe_max_hand_speed <= 10:
        raise SystemExit("--keyframe-max-hand-speed 需要在 0.1..10 之间")
    if not 0.1 <= args.keyframe_max_head_speed <= 10:
        raise SystemExit("--keyframe-max-head-speed 需要在 0.1..10 之间")
    parse_zone(args.zone)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
