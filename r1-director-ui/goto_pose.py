#!/usr/bin/env python3
"""按姿态文件把机械臂移动到记录的末端位姿并保持。

这是旧入口的兼容包装，实际逻辑已并入 pose.py restore，统一走
move_abs 绝对末端位姿回位（位置 x/y/z + roll/pitch/yaw）。

用法:
    ./goto_pose.py [--socket /tmp/r1_jog.sock] [--pose START.txt] [--timeout 180]

旧版只含 7 个关节角的姿态文件会被自动换算成末端位姿。
先启动服务（需要按 Enter 使能）:
    ./run.sh --api /tmp/r1_jog.sock
"""

import argparse
import sys
from pathlib import Path

import pose

DEFAULT_SOCKET = "/tmp/r1_jog.sock"
DEFAULT_CALIB = "/home/radxa/r1-tracker/r1-7a_sdk/config/joint_calib_right.txt"


def main() -> int:
    parser = argparse.ArgumentParser(description="调用 r1_jog API 回到记录末端位姿")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--pose", required=True, help="经当前安装核验的姿态文件")
    parser.add_argument("--calibration", default=DEFAULT_CALIB)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--allow-dry-run",
        action="store_true",
        help="允许从 dry-run 服务回位，仅用于流程验证",
    )
    args = parser.parse_args()

    source = Path(args.pose).expanduser()
    restore_args = argparse.Namespace(
        config=str(pose.DEFAULT_CONFIG),
        socket=args.socket,
        calibration=args.calibration,
        port=None,
        name=source.stem,
        pose_dir=str(source.parent),
        allow_dry_run=args.allow_dry_run,
        timeout=args.timeout,
    )
    return pose.cmd_restore(restore_args)


if __name__ == "__main__":
    sys.exit(main())
