import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx
import uvicorn

from .app import create_app


def parser():
    result = argparse.ArgumentParser(description="NanoBot 云端 Agent + Mock 端侧")
    result.add_argument("--data", type=Path, default=Path(".state"))
    result.add_argument(
        "--disable-edge", action="store_true", help="不向模型注册拍摄能力，用于能力缺失验证"
    )
    commands = result.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="启动本地 API 与事件 worker")
    serve.add_argument("--port", type=int, default=8787)
    demo = commands.add_parser("demo", help="真实模型运行猫咪案例，模拟端侧完成事件")
    demo.add_argument("--example", type=Path, default=Path("examples/cat-story.json"))
    demo.add_argument("--after", type=float, default=0, help="延后启动秒数，用于验证定时入口")
    demo.add_argument("--timeout", type=float, default=300)
    commands.add_parser("check", help="检查本机模型配置与组件加载")
    from .dual_cli import add_parser

    add_parser(commands)
    from .interaction_cli import add_parser as add_interaction_parser

    add_interaction_parser(commands)
    from .harness_cli import add_parser as add_harness_parser

    add_harness_parser(commands)
    return result


async def demo(app, args):
    example = json.loads(args.example.read_text())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://demo"
        ) as client:
            body = {"prompt": example["prompt"], "context": example["context"]}
            if args.after > 0:
                from datetime import datetime, timezone

                body["run_at"] = datetime.fromtimestamp(
                    time.time() + args.after, timezone.utc
                ).isoformat()
            response = await client.post("/tasks", json=body)
            response.raise_for_status()
            task_id = response.json()["id"]
            print(f"task_id={task_id}", flush=True)
            print("Cloud: real NanoBot + configured model; Edge: explicit Mock", flush=True)
            end = time.monotonic() + args.timeout
            observed = None
            completed_edge_ids = set()
            while time.monotonic() < end:
                task = (await client.get(f"/tasks/{task_id}")).json()
                if task["status"] != observed:
                    observed = task["status"]
                    print(f"status={observed}", flush=True)
                if task["status"] == "waiting":
                    for edge in task["edge_tasks"]:
                        if edge["status"] == "accepted" and edge["id"] not in completed_edge_ids:
                            completion = {
                                "event_id": f"demo:{uuid.uuid4().hex}",
                                "result": example["mock_result"],
                            }
                            route = f"/mock/edge-tasks/{edge['id']}/complete"
                            first = await client.post(route, json=completion)
                            first.raise_for_status()
                            duplicate = await client.post(route, json=completion)
                            duplicate.raise_for_status()
                            assert duplicate.json()["duplicate"]
                            completed_edge_ids.add(edge["id"])
                            print("Mock completion delivered; duplicate ignored", flush=True)
                if task["status"] in {"completed", "unsupported", "failed", "needs_input"}:
                    report = await client.get(f"/tasks/{task_id}/report")
                    evidence = {
                        "task_id": task_id,
                        "status": task["status"],
                        "model": app.state.service.agent.model,
                        "cloud_model_mode": "real",
                        "edge_simulated": True,
                        "events": task["events"],
                        "runs": task["runs"],
                        "outcome": task["outcome"],
                        "report_exists": report.status_code == 200,
                        "completed_edge_tasks": len(completed_edge_ids),
                    }
                    evidence_path = args.data.resolve() / "tasks" / task_id / "evidence.json"
                    evidence_path.parent.mkdir(parents=True, exist_ok=True)
                    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
                    print(json.dumps(evidence, ensure_ascii=False, indent=2), flush=True)
                    print(f"Evidence: {evidence_path}")
                    if (
                        task["status"] != "completed"
                        or not completed_edge_ids
                        or report.status_code != 200
                    ):
                        raise RuntimeError("Demo acceptance failed; inspect the task evidence")
                    return
                await asyncio.sleep(0.25)
            raise TimeoutError(f"Demo timed out; task {task_id} remains persisted")


def main():
    args = parser().parse_args()
    if args.command == "harness":
        from .harness_cli import run

        run(args)
        return
    if args.command == "interaction":
        from .interaction_cli import run

        run(args)
        return
    if args.command == "dual":
        from .dual_cli import dispatch

        dispatch(args)
        return
    from .runtime import NanobotAgent

    try:
        agent = NanobotAgent(args.data, edge_enabled=not args.disable_edge)
        if args.command == "check":
            print(json.dumps(agent.configuration_summary(), ensure_ascii=False, indent=2))
        elif args.command == "serve":
            uvicorn.run(create_app(args.data, agent), host="127.0.0.1", port=args.port)
        else:
            asyncio.run(demo(create_app(args.data, agent), args))
    except (ValueError, RuntimeError, TimeoutError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
