"""Explicit entry points for the approved, isolated edge-owned interaction trial."""

import argparse
from pathlib import Path

import uvicorn

from .contracts import load_profile
from .dual_cli import check_ports, download_source, local_token, root_for
from .media import prepare_clips
from .plugins import require_profile


def add_parser(commands):
    p = commands.add_parser("interaction", help="端侧主导的对话与异步任务实验")
    p.add_argument("action", choices=["prepare", "edge", "cloud"])
    p.add_argument("--profile", type=Path, default=Path("profiles/o6n-interaction-candidate.json"))


def run(args):
    profile = load_profile(args.profile)
    if profile.interaction is None:
        raise ValueError("profile is not an interactive deployment")
    require_profile(profile)
    root = root_for(profile)
    token = local_token(root)
    if args.action == "prepare":
        download_source(root)
        prepare_clips(root)
        print("素材准备完成；模型与服务尚未由此命令启动。")
        return
    if args.action == "edge":
        from .interaction_app import create_interaction_edge_app

        port = profile.edge_port
        app = create_interaction_edge_app(profile, root, token)
    else:
        from .interaction_cloud import create_interaction_cloud_app

        port = profile.cloud_port
        app = create_interaction_cloud_app(profile, root, token)
    check_ports([port])
    uvicorn.run(app, host="127.0.0.1", port=port, workers=1, log_level="warning")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "edge", "cloud"])
    parser.add_argument(
        "--profile", type=Path, default=Path("profiles/o6n-interaction-candidate.json")
    )
    run(parser.parse_args())
