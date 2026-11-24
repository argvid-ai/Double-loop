"""Explicit isolated functional host entry points."""

from pathlib import Path

import uvicorn

from .contracts import load_profile
from .dual_cli import check_ports, download_source, local_token, root_for
from .media import prepare_clips
from .plugins import require_profile


def add_parser(commands):
    command = commands.add_parser("harness", help="端侧语音 / 云端网页的双向功能系统")
    command.add_argument(
        "action",
        choices=[
            "prepare",
            "edge",
            "cloud",
            "verify",
            "start",
            "stop",
            "status",
            "check-observation",
        ],
    )
    command.add_argument(
        "--side", choices=["edge", "cloud"], default="cloud", help="后台启动/停止/状态的节点"
    )
    command.add_argument("--profile", type=Path, default=Path("profiles/harness-functional.json"))
    command.add_argument(
        "--suite",
        choices=["memory", "efficiency", "controls", "cache", "peer-mutations", "status-queries"],
        default="memory",
    )
    command.add_argument("--evidence", type=Path)


def run(args):
    profile = load_profile(args.profile)
    if profile.harness is None:
        raise ValueError("functional profile required")
    require_profile(profile)
    root = root_for(profile)
    if args.action == "check-observation":
        import json
        from types import SimpleNamespace

        from .harness_state import HarnessStore
        from .observation_installation import ObservationRegistry

        if not profile.harness.observation_config_file:
            raise ValueError("profile has no observation_config_file")
        registry = ObservationRegistry(
            SimpleNamespace(side="edge", store=HarnessStore(root / "edge", "edge")),
            profile.harness.observation_config_file,
        )
        print(
            json.dumps(
                {
                    "checked": True,
                    "model_started": False,
                    "task_started": False,
                    "catalogue": registry.catalogue(),
                },
                ensure_ascii=False,
            )
        )
        return
    if args.action in {"start", "stop", "status"}:
        import json

        from .harness_process import manage

        print(
            json.dumps(
                manage(args.action, args.side, profile, args.profile, root), ensure_ascii=False
            )
        )
        return
    if args.action == "verify":
        import asyncio

        from .harness_verify import verify

        if args.evidence is None:
            raise ValueError("verification requires a new --evidence directory")
        asyncio.run(verify(profile, root, args.evidence, args.suite))
        return
    if args.action == "prepare":
        download_source(root)
        prepare_clips(root)
        local_token(root)
        print("准备完毕，未启动模型或任务。")
        return
    from .harness_app import create_harness_app

    port = profile.cloud_port if args.action == "cloud" else profile.edge_port
    check_ports([port])
    app = create_harness_app(profile, root, args.action, local_token(root))
    from .runtime_logging import runtime_log_config

    logging = runtime_log_config(root, args.action, profile.harness)
    if args.action == "cloud" and profile.harness.home_access_file:
        import asyncio
        from .home_access import HomeAccessConfig, serve_home

        config = HomeAccessConfig.model_validate_json(
            Path(profile.harness.home_access_file).read_text()
        )
        check_ports([config.port])
        asyncio.run(serve_home(app, port, config, log_config=logging))
        return
    from .home_access import HTTP_SHUTDOWN_SECONDS

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        workers=1,
        log_level="warning",
        log_config=logging,
        access_log=False,
        timeout_graceful_shutdown=HTTP_SHUTDOWN_SECONDS,
    )
