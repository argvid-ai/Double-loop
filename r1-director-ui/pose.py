#!/usr/bin/env python3
"""记录机械臂末端位姿，并按记录的末端位姿回位保持。

用法:
    ./pose.py record  [--name home]    # 记录当前末端位姿到 poses/<name>.txt
    ./pose.py restore [--name home]    # 回到记录位姿，由 r1_jog 持续保持

记录内容为 Link7 法兰在基座坐标系下的位置 x/y/z（m）和
roll/pitch/yaw（deg），同时保存记录时的实测 7 个关节角。
回位优先使用关节角 + goto_pose；旧版 EEF 文件没有关节角时才退回 move_abs。

记录来源自动判断:
    1. r1_jog 服务可通过 Unix Socket 访问时，读取 status 的实测末端位姿；
    2. 否则调用 r1_play --snapshot 读取停机关节角，再用 r1_jog --fk
       换算成末端位姿（读取后电机为 mode 0）。

回位需要 r1_jog 已启动并使能:
    ./run.sh --api /tmp/r1_jog.sock

旧版只含 7 个关节角的姿态文件仍可回位：脚本会调用 fk API 换算成末端位姿，
再走 move_abs 绝对末端回位。
"""

import argparse
import hashlib
import json
import math
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_CONFIG = SCRIPT_DIR / "config" / "arm_right.conf"
DEFAULT_POSE_DIR = SCRIPT_DIR / "poses"
DEFAULT_SOCKET = "/tmp/r1_jog.sock"
DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_CALIBRATION = "/home/radxa/r1-tracker/r1-7a_sdk/config/joint_calib_right.txt"
JOINT_COUNT = 7

STABLE_SAMPLES = 5
STABLE_INTERVAL_S = 0.1
STABLE_SPREAD_M = 0.002
STABLE_SPREAD_DEG = 0.3
PENDING_MM_LIMIT = 1.0
PENDING_DEG_LIMIT = 0.5
POSE_POSITION_RESIDUAL_M = 0.005
POSE_ORIENTATION_RESIDUAL_DEG = 3.0

SNAPSHOT_RUNNERS = (
    PROJECT_ROOT / "snapshot-clearance-20261121" / "build" / "r1_play",
    PROJECT_ROOT / "snapshot-hold-20261121" / "build" / "r1_play",
    PROJECT_ROOT / "build" / "r1_play",
)
FK_RUNNERS = (
    SCRIPT_DIR / "build" / "r1_jog",
)


class Arm:
    """r1_jog 本地 Unix Socket 客户端。"""

    def __init__(self, path: str, timeout: float = 2.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.stream = self.sock.makefile("rwb")

    def call(self, command: str) -> dict:
        self.stream.write((command + "\n").encode())
        self.stream.flush()
        line = self.stream.readline()
        if not line:
            raise RuntimeError("API 连接已断开")
        return json.loads(line)

    def close(self) -> None:
        try:
            self.stream.close()
        finally:
            self.sock.close()


def load_config(path: Path) -> dict:
    config = {
        "port": DEFAULT_PORT,
        "calibration": DEFAULT_CALIBRATION,
        "api_socket": "",
    }
    if not path.is_file():
        print(f"警告: 未找到配置 {path}，使用默认值", file=sys.stderr)
        return config
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in config:
            config[key] = value
    return config


def calibration_digest(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"标定文件不存在: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_joints(values) -> list:
    if not isinstance(values, list) or len(values) != JOINT_COUNT:
        raise SystemExit(f"需要 {JOINT_COUNT} 个关节角，实际为 {values!r}")
    result = []
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            raise SystemExit("关节角包含非有限数值")
        result.append(number)
    return result


def validate_pose(values) -> dict:
    pose = {}
    for key in ("x", "y", "z", "roll", "pitch", "yaw"):
        if key not in values:
            raise SystemExit(f"末端位姿缺少字段 {key}")
        try:
            number = float(values[key])
        except (TypeError, ValueError):
            raise SystemExit(f"末端位姿 {key} 不是合法数字")
        if not math.isfinite(number):
            raise SystemExit(f"末端位姿 {key} 不是有限数值")
        pose[key] = number
    return pose


def pose_from_status(status: dict) -> dict:
    """优先使用实测位姿，旧服务只提供指令位姿时退回到 pose_m。"""
    position = status.get("pose_meas_m") or status.get("pose_m") or {}
    rpy = status.get("rpy_meas_deg") or status.get("rpy_deg") or {}
    return validate_pose(
        {
            "x": position.get("x"),
            "y": position.get("y"),
            "z": position.get("z"),
            "roll": rpy.get("roll"),
            "pitch": rpy.get("pitch"),
            "yaw": rpy.get("yaw"),
        }
    )


def format_pose(pose: dict) -> str:
    return (
        f"xyz=({pose['x']:+.4f}, {pose['y']:+.4f}, {pose['z']:+.4f}) m  "
        f"rpy=({pose['roll']:+.2f}, {pose['pitch']:+.2f}, {pose['yaw']:+.2f}) deg"
    )


def pose_path(args) -> Path:
    directory = Path(args.pose_dir) if args.pose_dir else DEFAULT_POSE_DIR
    name = args.name if args.name.endswith(".txt") else args.name + ".txt"
    return directory / name


def write_pose(path: Path, digest: str, pose: dict, joints=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = " ".join(
        f"{pose[key]:.7f}" for key in ("x", "y", "z", "roll", "pitch", "yaw")
    )
    text = digest + "\neef " + values + "\n"
    if joints is not None:
        text += "joint " + " ".join(f"{value:.7f}" for value in joints) + "\n"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o644)


def load_pose(path: Path):
    """返回 (digest, kind, pose, joints)；joints 可能为 None（旧版 EEF 文件）。"""
    if not path.is_file():
        raise SystemExit(f"姿态文件不存在: {path}（先执行 record）")
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        raise SystemExit(f"姿态文件格式不正确: {path}")
    digest = lines[0].strip()
    if len(digest) != 64:
        raise SystemExit(f"姿态文件缺少有效的标定哈希: {path}")
    fields = lines[1].split()
    if not fields:
        raise SystemExit(f"姿态文件内容为空: {path}")
    try:
        if fields[0].lower() == "eef":
            numbers = [float(value) for value in fields[1:]]
            if len(numbers) != 6:
                raise ValueError
            pose = dict(zip(("x", "y", "z", "roll", "pitch", "yaw"), numbers))
            pose = validate_pose(pose)
            joints = None
            if len(lines) >= 3:
                joint_fields = lines[2].split()
                if joint_fields and joint_fields[0].lower() == "joint":
                    joints = validate_joints([float(value) for value in joint_fields[1:]])
            return digest, "eef", pose, joints
        joints = validate_joints([float(value) for value in fields])
        return digest, "joint", None, joints
    except ValueError:
        raise SystemExit(f"姿态文件中的数值不是合法数字: {path}")


def position_distance(a: dict, b: dict) -> float:
    return math.sqrt(
        (a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2 + (a["z"] - b["z"]) ** 2
    )


def rpy_to_matrix(pose: dict):
    roll = math.radians(pose["roll"])
    pitch = math.radians(pose["pitch"])
    yaw = math.radians(pose["yaw"])
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


def orientation_distance_deg(a: dict, b: dict) -> float:
    first = rpy_to_matrix(a)
    second = rpy_to_matrix(b)
    trace = 0.0
    for row in range(3):
        for col in range(3):
            trace += first[row][col] * second[row][col]
    cosine = max(-1.0, min(1.0, (trace - 1.0) / 2.0))
    return math.degrees(math.acos(cosine))


def try_connect(path: str):
    try:
        return Arm(path)
    except OSError:
        return None


def sample_stable(arm: Arm, force: bool) -> dict:
    samples = []
    pending_mm = 0.0
    pending_deg = 0.0
    for index in range(STABLE_SAMPLES):
        status = arm.call("status")
        if not status.get("ok"):
            raise SystemExit("状态查询失败: " + str(status.get("error", "unknown")))
        samples.append(pose_from_status(status))
        pending_mm = float(status.get("pending_mm", 0.0))
        pending_deg = float(status.get("pending_deg", 0.0))
        if index + 1 < STABLE_SAMPLES:
            time.sleep(STABLE_INTERVAL_S)

    last = samples[-1]
    spread_m = max(position_distance(pose, last) for pose in samples)
    spread_deg = max(orientation_distance_deg(pose, last) for pose in samples)

    if force:
        return last
    if pending_mm >= PENDING_MM_LIMIT or pending_deg >= PENDING_DEG_LIMIT:
        raise SystemExit(
            "机械臂仍在运动"
            f"（pending_mm={pending_mm:.1f}, pending_deg={pending_deg:.2f}），"
            "请到位后重试；确认要记录当前瞬时姿态可加 --force"
        )
    if spread_m >= STABLE_SPREAD_M or spread_deg >= STABLE_SPREAD_DEG:
        raise SystemExit(
            f"采样期间末端变化 {spread_m * 1000:.1f} mm / {spread_deg:.2f} deg，"
            "机械臂未稳定；请松开或等待后重试，确认要记录可加 --force"
        )
    return last


def find_executable(explicit: str, candidates, label: str) -> Path:
    if explicit:
        runner = Path(explicit)
        if not (runner.is_file() and os.access(runner, os.X_OK)):
            raise SystemExit(f"指定的{label}不可执行: {runner}")
        return runner
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise SystemExit(f"找不到可执行的{label}；请用命令行参数显式指定")


def clean_env() -> dict:
    """Trae 会注入指向自带 libstdc++ 的 LD_LIBRARY_PATH，调用二进制前清掉。"""
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    return env


def record_from_snapshot(args, config: dict, calibration: Path):
    runner = find_executable(args.snapshot_runner, SNAPSHOT_RUNNERS, "快照程序")
    port = args.port or config.get("port") or DEFAULT_PORT
    try:
        completed = subprocess.run(
            [str(runner), "--snapshot", port, str(calibration)],
            capture_output=True,
            text=True,
            timeout=30,
            env=clean_env(),
        )
    except subprocess.TimeoutExpired:
        raise SystemExit("快照超时（30 s），请检查串口和机械臂状态")

    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    if completed.returncode != 0:
        if "already holds the lock" in output or "holds the lock" in output:
            raise SystemExit(
                "r1_jog 正在占用串口但未启用 API；"
                "请用 ./run.sh --api /tmp/r1_jog.sock 启动后，通过运行中的服务记录"
            )
        print(output.strip(), file=sys.stderr)
        raise SystemExit(f"快照失败，退出码 {completed.returncode}")

    snapshot = None
    for line in (completed.stdout or "").splitlines():
        if line.startswith("SNAPSHOT "):
            snapshot = json.loads(line[len("SNAPSHOT "):])
    if snapshot is None:
        print(output.strip(), file=sys.stderr)
        raise SystemExit("快照输出中没有 SNAPSHOT 行")
    if snapshot.get("mode") != 0:
        raise SystemExit(f"快照时电机不是 mode 0（实际 {snapshot.get('mode')}），拒绝记录")
    joints = validate_joints(snapshot.get("q_sdk_rad"))
    pose = joints_to_pose(args, joints, config)
    print("注意: 快照结束后电机为 mode 0，机械臂会失力；请先承托，回位前重新启动 r1_jog。")
    return pose, joints


def joints_to_pose(args, joints, config: dict) -> dict:
    runner = find_executable(args.fk_runner, FK_RUNNERS, "正解程序")
    try:
        completed = subprocess.run(
            [str(runner), "--fk", *[repr(float(value)) for value in joints]],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(SCRIPT_DIR),
            env=clean_env(),
        )
    except subprocess.TimeoutExpired:
        raise SystemExit("正解超时（10 s）")
    output = completed.stdout or ""
    if completed.returncode != 0:
        print((completed.stderr or "").strip(), file=sys.stderr)
        raise SystemExit(f"正解失败，退出码 {completed.returncode}")
    for line in output.splitlines():
        if line.startswith("FK "):
            payload = json.loads(line[len("FK "):])
            position = payload.get("pose_m", {})
            rpy = payload.get("rpy_deg", {})
            return validate_pose(
                {
                    "x": position.get("x"),
                    "y": position.get("y"),
                    "z": position.get("z"),
                    "roll": rpy.get("roll"),
                    "pitch": rpy.get("pitch"),
                    "yaw": rpy.get("yaw"),
                }
            )
    raise SystemExit("正解输出中没有 FK 行")


def cmd_record(args) -> int:
    config = load_config(Path(args.config))
    calibration = Path(args.calibration or config.get("calibration") or DEFAULT_CALIBRATION)
    digest = calibration_digest(calibration)
    socket_path = args.socket or config.get("api_socket") or DEFAULT_SOCKET

    arm = try_connect(socket_path)
    joints = None
    if arm is not None:
        try:
            pong = arm.call("ping")
            if not pong.get("ok"):
                raise SystemExit("API ping 失败")
            status = arm.call("status")
            if not status.get("ok"):
                raise SystemExit("状态查询失败: " + str(status.get("error", "unknown")))
            if status.get("dry_run") and not args.allow_dry_run:
                raise SystemExit(
                    "服务处于 dry-run，未连接实机；仅做流程验证请加 --allow-dry-run"
                )
            pose = sample_stable(arm, args.force)
            status = arm.call("status")
            if not status.get("ok"):
                raise SystemExit("状态查询失败: " + str(status.get("error", "unknown")))
            joints = validate_joints(status.get("q_meas_rad"))
            source = "API 实测末端位姿 + 实测关节角"
        finally:
            arm.close()
    else:
        pose, joints = record_from_snapshot(args, config, calibration)
        source = "停机快照 + 正解"

    target = pose_path(args)
    write_pose(target, digest, pose, joints)
    print(f"已记录末端位姿（{source}）")
    print(f"  文件: {target}")
    print(f"  标定: {digest[:12]}...")
    print(f"  {format_pose(pose)}")
    return 0


def pause_quietly(arm: Arm) -> None:
    try:
        arm.call("pause")
    except Exception:
        pass


def convert_joint_file(arm: Arm, joints) -> dict:
    reply = arm.call("fk " + " ".join(repr(float(value)) for value in joints))
    if not reply.get("ok"):
        raise SystemExit(
            "旧姿态文件是关节角，但服务不支持 fk 换算: "
            + str(reply.get("error", "unknown"))
        )
    position = reply.get("pose_m", {})
    rpy = reply.get("rpy_deg", {})
    return validate_pose(
        {
            "x": position.get("x"),
            "y": position.get("y"),
            "z": position.get("z"),
            "roll": rpy.get("roll"),
            "pitch": rpy.get("pitch"),
            "yaw": rpy.get("yaw"),
        }
    )


def cmd_restore(args) -> int:
    config = load_config(Path(args.config))
    calibration = Path(args.calibration or config.get("calibration") or DEFAULT_CALIBRATION)
    digest = calibration_digest(calibration)
    socket_path = args.socket or config.get("api_socket") or DEFAULT_SOCKET

    source = pose_path(args)
    stored_digest, kind, target, joints = load_pose(source)
    if stored_digest != digest:
        raise SystemExit(
            "标定文件与记录位姿不一致:\n"
            f"  记录 {stored_digest}\n  当前 {digest}\n"
            "请确认使用的是同一份 joint_calib_right.txt"
        )

    try:
        arm = Arm(socket_path)
    except OSError as error:
        print(f"无法连接 r1_jog API ({socket_path}): {error}", file=sys.stderr)
        print(
            "请先启动: ./run.sh --api /tmp/r1_jog.sock\n"
            "实机启动后还需要在终端按 Enter 使能 FOC。",
            file=sys.stderr,
        )
        return 1

    try:
        if not arm.call("ping").get("ok"):
            print("API ping 失败", file=sys.stderr)
            return 1
        status = arm.call("status")
        if not status.get("ok"):
            print("状态查询失败: " + str(status.get("error", "unknown")), file=sys.stderr)
            return 1
        if status.get("dry_run") and not args.allow_dry_run:
            print(
                "服务处于 dry-run，未连接实机；仅做流程验证请加 --allow-dry-run",
                file=sys.stderr,
            )
            return 1

        if kind == "joint":
            joints = target
            target = convert_joint_file(arm, joints)
            print("旧姿态文件为关节角，已通过 fk 换算为末端位姿。")
        elif target is None and joints is not None:
            target = convert_joint_file(arm, joints)

        current = pose_from_status(status)
        print(f"姿态文件: {source}")
        print("当前位姿: " + format_pose(current))
        if target is not None:
            print("目标位姿: " + format_pose(target))
            print(
                f"偏差:     {position_distance(current, target) * 1000:.1f} mm, "
                f"{orientation_distance_deg(current, target):.2f} deg"
            )

        try:
            if status.get("state") == "paused":
                arm.call("resume")
                print("已从暂停状态恢复，准备回位。")

            if joints is not None:
                # 统一回位路径：先把轨迹起点同步到当前实测角，再用 goto_pose。
                try:
                    synced = arm.call("sync_current")
                    if not synced.get("ok"):
                        print("警告: 服务不支持 sync_current，按当前指令角起步", file=sys.stderr)
                except Exception as error:
                    print(f"警告: sync_current 失败（{error}），按当前指令角起步", file=sys.stderr)

                command = "goto_pose " + " ".join(repr(float(value)) for value in joints)
                reply = arm.call(command)
                if not reply.get("ok"):
                    error = str(reply.get("error", "unknown"))
                    raise SystemExit(
                        "API 拒绝: "
                        + error
                        + "\n目标超出回位包络或不可达，请先点动靠近，或重新 record。"
                    )

                deadline = time.time() + args.timeout
                last_print = 0.0
                while True:
                    status = arm.call("status")
                    if not status.get("ok"):
                        raise RuntimeError(
                            "状态查询失败: " + str(status.get("error", "unknown"))
                        )
                    measured_joints = validate_joints(status.get("q_meas_rad"))
                    joint_error = max(
                        abs(a - b) for a, b in zip(measured_joints, joints)
                    )
                    pending = float(status.get("pending_joint_rad", 0.0))
                    now = time.time()
                    if now - last_print >= 1.0:
                        print(
                            f"进行中 剩余 {pending:.4f} rad  "
                            f"关节残差 {joint_error:.4f} rad"
                        )
                        last_print = now
                    if joint_error <= args.joint_tolerance and pending <= 0.002:
                        break
                    if now >= deadline:
                        arm.call("pause")
                        print(
                            f"超时: 关节残差 {joint_error:.4f} rad，"
                            f"剩余 {pending:.4f} rad；已发送 pause 停在当前位置",
                            file=sys.stderr,
                        )
                        return 1
                    time.sleep(0.1)
            else:
                command = "move_abs " + " ".join(
                    repr(float(target[key]))
                    for key in ("x", "y", "z", "roll", "pitch", "yaw")
                )
                reply = arm.call(command)
                if not reply.get("ok"):
                    error = str(reply.get("error", "unknown"))
                    if "outside envelope" in error or "unreachable" in error:
                        raise SystemExit(
                            "API 拒绝: "
                            + error
                            + "\n当前位姿离记录位姿太远，请先点动靠近，或重新 record。"
                        )
                    raise SystemExit("API 拒绝: " + error)

                deadline = time.time() + args.timeout
                last_print = 0.0
                while True:
                    status = arm.call("status")
                    if not status.get("ok"):
                        raise RuntimeError(
                            "状态查询失败: " + str(status.get("error", "unknown"))
                        )
                    measured = pose_from_status(status)
                    position_error = position_distance(measured, target)
                    orientation_error = orientation_distance_deg(measured, target)
                    pending_mm = float(status.get("pending_mm", 0.0))
                    pending_deg = float(status.get("pending_deg", 0.0))
                    now = time.time()
                    if now - last_print >= 1.0:
                        print(
                            f"进行中 剩余 {pending_mm:.1f} mm / {pending_deg:.2f} deg  "
                            f"实测偏差 {position_error * 1000:.1f} mm / "
                            f"{orientation_error:.2f} deg"
                        )
                        last_print = now
                    if (
                        position_error <= POSE_POSITION_RESIDUAL_M
                        and orientation_error <= POSE_ORIENTATION_RESIDUAL_DEG
                        and pending_mm <= 0.5
                        and pending_deg <= 0.2
                    ):
                        break
                    if now >= deadline:
                        arm.call("pause")
                        print(
                            f"超时: 仍未到位，剩余 {pending_mm:.1f} mm / "
                            f"{pending_deg:.2f} deg；已发送 pause 停在当前位置",
                            file=sys.stderr,
                        )
                        return 1
                    time.sleep(0.1)
        except KeyboardInterrupt:
            pause_quietly(arm)
            print("\n已中断，机械臂停在当前姿态并锁止（state=paused）。")
            return 130
        except SystemExit:
            raise
        except Exception as error:
            pause_quietly(arm)
            print(f"错误: {error}；已发送 pause 停在当前位置", file=sys.stderr)
            return 1

        final = arm.call("status")
        measured = pose_from_status(final)
        if joints is not None:
            measured_joints = validate_joints(final.get("q_meas_rad"))
            joint_error = max(abs(a - b) for a, b in zip(measured_joints, joints))
            print("已到位: " + format_pose(measured))
            if target is not None:
                print(
                    f"末端残差 {position_distance(measured, target) * 1000:.1f} mm / "
                    f"{orientation_distance_deg(measured, target):.2f} deg, "
                    f"关节残差 {joint_error:.4f} rad, "
                    f"跟踪误差 {float(final.get('track_err_rad', 0.0)):.4f} rad"
                )
            else:
                print(
                    f"关节残差 {joint_error:.4f} rad, "
                    f"跟踪误差 {float(final.get('track_err_rad', 0.0)):.4f} rad"
                )
            if joint_error > args.joint_tolerance:
                print("关节残差超过回位容差，按失败处理", file=sys.stderr)
                return 1
        else:
            position_error = position_distance(measured, target)
            orientation_error = orientation_distance_deg(measured, target)
            print("已到位: " + format_pose(measured))
            print(
                f"最大残差 {position_error * 1000:.1f} mm / {orientation_error:.2f} deg, "
                f"跟踪误差 {float(final.get('track_err_rad', 0.0)):.4f} rad"
            )
            if (
                position_error > POSE_POSITION_RESIDUAL_M
                or orientation_error > POSE_ORIENTATION_RESIDUAL_DEG
            ):
                print("残差超过末端回位容差，按失败处理", file=sys.stderr)
                return 1
        print("机械臂保持 running 状态并由 r1_jog 持续 FOC 保持；退出本脚本不影响保持。")
        print("需要显式暂停锁止可发送 pause 或按空格。")
        return 0
    finally:
        arm.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="记录机械臂末端位姿并按记录位姿回位保持")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub):
        sub.add_argument("--config", default=str(DEFAULT_CONFIG))
        sub.add_argument("--socket", help="r1_jog API socket 路径")
        sub.add_argument("--calibration", help="关节标定文件路径")
        sub.add_argument("--port", help="串口路径，仅快照模式使用")
        sub.add_argument("--name", default="home", help="姿态名称，默认 home")
        sub.add_argument("--pose-dir", help="姿态文件目录，默认 arm-control/poses")
        sub.add_argument(
            "--allow-dry-run",
            action="store_true",
            help="允许从 dry-run 服务记录/回位，仅用于流程验证",
        )

    record = subparsers.add_parser("record", help="记录当前末端位姿")
    add_common(record)
    record.add_argument("--force", action="store_true", help="跳过稳定性检查")
    record.add_argument("--snapshot-runner", help="指定 r1_play --snapshot 程序")
    record.add_argument("--fk-runner", help="指定 r1_jog 正解程序")
    record.set_defaults(func=cmd_record)

    restore = subparsers.add_parser("restore", help="回到记录位姿并保持")
    add_common(restore)
    restore.add_argument("--timeout", type=float, default=180.0)
    restore.add_argument(
        "--joint-tolerance",
        type=float,
        default=0.06,
        help="goto_pose 回位的关节残差容差（rad），默认 0.06",
    )
    restore.set_defaults(func=cmd_restore)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
