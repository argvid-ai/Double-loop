"""Development probes through public/mock APIs, never an agent or success injector."""

from __future__ import annotations

import asyncio
import json
import statistics
import time
import uuid
from datetime import datetime, timezone

import httpx

from .harness_build import source_identity


class Probe:
    def __init__(self, profile, root, output):
        self.profile, self.root, self.output = profile, root, output
        self.output.mkdir(parents=True, exist_ok=True)
        if (output / "report.json").exists():
            raise ValueError("evidence directory already contains a report")
        token = (root / "node-token").read_text().strip()
        self.clients = {
            side: httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{getattr(profile, side + '_port')}",
                headers={"Authorization": "Bearer " + token},
                timeout=30,
                trust_env=False,
            )
            for side in ["cloud", "edge"]
        }
        self.records = []
        self.metadata = {}
        self.prefix = "probe-" + uuid.uuid4().hex[:10]

    async def preflight(self):
        identity = source_identity()
        self.metadata["source_identity"] = identity
        for side, client in self.clients.items():
            r = await client.get("/health")
            r.raise_for_status()
            health = r.json()
            self.metadata[side] = health
            if health.get("source_digest") != identity["digest"]:
                raise RuntimeError(f"{side} runtime differs from the current source snapshot")
        self.metadata["model_mode"] = (
            "real configured providers; device and voice sources are simulated"
        )
        self.metadata["boundary"] = "developer verification, not independent acceptance"
        self.metadata["latency_boundary"] = (
            "client monotonic submit-to-final-API-reply with 0.25s polling; not microphone-to-sound latency"
        )
        self.save()

    def save(self):
        report = {"metadata": self.metadata, "records": self.records}
        groups = {}
        for record in self.records:
            if "category" in record:
                groups.setdefault(record["category"], []).append(record)
        report["timings"] = {}
        for name, rows in groups.items():
            times = [r["reply_seconds"] for r in rows if r.get("reply_seconds") is not None]
            report["timings"][name] = {
                "samples": len(rows),
                "successful_samples": sum(r.get("passed") is True for r in rows),
                "failures_and_timeouts": [r["id"] for r in rows if r.get("passed") is not True],
                "reply_seconds": times,
                "median": statistics.median(times) if times else None,
                "range": [min(times), max(times)] if times else None,
                "note": "success timings are descriptive; failed rows remain in records and are not silently discarded",
            }
        responses = {}
        budgets = []
        for path in self.output.glob("*.json"):
            if path.name == "report.json":
                continue
            data = json.loads(path.read_text())
            for row in data.get("trace", []):
                payload = row["payload"]
                if row["kind"] == "model.response":
                    responses[payload.get("request_id", path.name + str(row["seq"]))] = payload
                elif row["kind"] == "context.budget":
                    budgets.append(payload)
        report["provider_metrics"] = {
            "calls": len(responses),
            "sdk_retry_notifications": sum(
                r.get("sdk_retry_notifications", 0) for r in responses.values()
            ),
            "roundtrip_seconds": [r["elapsed_seconds"] for r in responses.values()],
            "prompt_tokens": sum(
                (r.get("usage") or {}).get("prompt_tokens", 0) for r in responses.values()
            ),
            "completion_tokens": sum(
                (r.get("usage") or {}).get("completion_tokens", 0) for r in responses.values()
            ),
            "cached_tokens_reported": sum(
                (r.get("usage") or {}).get("cached_tokens", 0) for r in responses.values()
            ),
            "responses_without_cache_counter": sum(
                "cached_tokens" not in (r.get("usage") or {}) for r in responses.values()
            ),
            "context_trims_observed": sum(bool(b.get("removed_message_indices")) for b in budgets),
            "context_counting_methods": sorted(
                {b.get("counting_method", "unknown") for b in budgets}
            ),
            "pure_decode_tokens_per_second": None,
            "limitations": "Missing cache counters are unknown, not misses. Provider completion tokens may include tool/reasoning tokens; no pure decode throughput is inferred.",
        }
        (self.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))

    async def idle(self):
        async with asyncio.timeout(600):
            while True:
                active = False
                for client in self.clients.values():
                    r = await client.get("/harness/test/status")
                    r.raise_for_status()
                    status = r.json()
                    active |= status["model_busy"] or any(
                        x["status"] in {"queued", "running"} and x["count"]
                        for x in status["events"] + status["jobs"]
                    )
                if not active:
                    return
                await asyncio.sleep(0.3)

    async def ask(
        self,
        side,
        text,
        *,
        category="functional",
        mode="query",
        interaction_id=None,
        task_id=None,
        expected=None,
    ):
        ident = self.prefix + "-" + uuid.uuid4().hex[:10]
        record = {
            "id": ident,
            "side": side,
            "text": text,
            "category": category,
            "passed": False,
            "poll_interval_seconds": 0.25,
            "simulated_voice": side == "edge",
        }
        self.records.append(record)
        start = time.monotonic()
        body = {
            "event_id": ident,
            "text": text,
            "mode": mode,
            "interaction_id": interaction_id,
            "task_id": task_id,
        }
        if side == "edge":
            body.update(
                observed_at=datetime.now(timezone.utc).isoformat(),
                source_ref="fixture:developer-probe/" + ident,
                simulated=True,
            )
        client = self.clients[side]
        try:
            r = await client.post(
                "/harness/mock/voice" if side == "edge" else "/harness/input", json=body
            )
            record["ack_seconds"] = time.monotonic() - start
            r.raise_for_status()
            pending = []
            async with asyncio.timeout(600):
                while True:
                    r = await client.get("/harness/events/" + ident)
                    r.raise_for_status()
                    event = r.json()
                    if event["status"] in {"done", "failed", "superseded"}:
                        record["initial_event"] = event
                        if event["status"] != "done":
                            raise RuntimeError("input processing failed: " + str(event["error"]))
                        receipt = (event.get("reply") or {}).get("pending_receipt") or {}
                        if receipt.get("tool") == "peer_request":
                            pending = [receipt["request_id"]]
                            r = await client.get("/harness/events")
                            r.raise_for_status()
                            replies = [
                                e
                                for e in r.json()["events"]
                                if e["body"].get("origin_event_id") == ident
                                and e["kind"] in {"peer.result", "peer.failed"}
                            ]
                            if replies and replies[0]["status"] in {"done", "failed"}:
                                event = replies[0]
                                if event["status"] != "done" or event["kind"] == "peer.failed":
                                    raise RuntimeError("peer query failed")
                                break
                        else:
                            break
                    await asyncio.sleep(0.25)
            record["reply_seconds"] = time.monotonic() - start
            record["event"] = event
            record["answer"] = event["reply"].get("text", "")
            for event_id in {ident, event["id"]}:
                r = await client.get("/harness/trace/" + event_id)
                r.raise_for_status()
                (self.output / f"{side}-{event_id}.json").write_text(
                    json.dumps(r.json(), ensure_ascii=False, indent=2)
                )
            for event_id in pending:
                peer = "cloud" if side == "edge" else "edge"
                r = await self.clients[peer].get("/harness/trace/" + event_id)
                r.raise_for_status()
                (self.output / f"{peer}-{event_id}.json").write_text(
                    json.dumps(r.json(), ensure_ascii=False, indent=2)
                )
            if expected and expected not in record["answer"]:
                raise AssertionError("expected fact missing from answer")
            record["passed"] = True
            print(
                category,
                side,
                "PASS",
                round(record["reply_seconds"], 3),
                record["answer"][:160],
                flush=True,
            )
            return record
        except BaseException as exc:
            record["elapsed_seconds"] = time.monotonic() - start
            record["error_type"] = type(exc).__name__
            self.save()
            raise
        finally:
            self.save()

    async def memory(self):
        fixtures = [
            (
                "edge",
                "请记住：我把客厅的固定观察位叫“窗边位”。只保存这个偏好，不创建任务；先查已有记忆，同义重复项请归并到一条，保留原历史。",
                "interact",
            ),
            (
                "edge",
                "历史备注：上次拍摄任务暂停是因为有人午睡。仅保留在历史，不写入长期记忆，不创建或修改任务。",
                "query",
            ),
            (
                "cloud",
                "请记住：我的周报标题喜欢以“咪咪周记”开头。只保存偏好，不创建任务；先查已有记忆，同义重复项请归并到一条，保留原历史。",
                "interact",
            ),
            (
                "cloud",
                "历史备注：上次独立整理任务因素材授权未确认而暂缓。仅保留历史，不写入长期记忆，不创建或修改任务。",
                "query",
            ),
        ]
        for side, text, mode in fixtures:
            await self.ask(side, text, category="memory_setup", mode=mode)
        await self.idle()
        resets = {}
        for side, client in self.clients.items():
            r = await client.post("/harness/test/clear-sessions")
            r.raise_for_status()
            resets[side] = r.json()
        self.metadata["native_session_reset"] = resets
        self.save()
        for side, question, fact, collection in [
            ("edge", "请检索记忆，我给客厅那个固定观察位起了什么名字？", "窗边位", "memory"),
            ("edge", "请检索历史，上次那个拍摄任务为什么暂停？", "有人午睡", "history"),
            ("cloud", "请检索记忆，我对周报标题有什么偏好？", "咪咪周记", "memory"),
            ("cloud", "请检索历史，上次那个独立整理任务为什么暂缓？", "素材授权未确认", "history"),
        ]:
            result = await self.ask(
                side, question, category="cleared_session_retrieval", expected=fact
            )
            trace = json.loads((self.output / f"{side}-{result['event']['id']}.json").read_text())
            found = []
            for row in trace["trace"]:
                p = row["payload"]
                if row["kind"] == "tool.completed" and p.get("name") == "search_records":
                    found.extend(json.loads(p["result"]).get("records", []))
            if not any(r["collection"] == collection and fact in r.get("text", "") for r in found):
                result["passed"] = False
                result["error_type"] = "MissingRetrievalProvenance"
                self.save()
                raise AssertionError("answer lacks the required retrieval provenance")
            old_keys = set(resets[side]["cleared_keys"])
            if result["event"]["session_key"] not in old_keys:
                raise AssertionError(
                    "fixed main identity changed during explicit test context reset"
                )
            result["retrieved_sources"] = [r["id"] for r in found if r["collection"] == collection]
            self.save()
        await self.ask(
            "edge",
            "请向云端查询：我保存的周报标题偏好是什么？不要创建任务。",
            category="cross_side_query",
            expected="咪咪周记",
        )
        await self.ask(
            "cloud",
            "请向端侧查询：我给客厅固定观察位起了什么名字？不要创建任务。",
            category="cross_side_query",
            expected="窗边位",
        )

    async def efficiency(self):
        for index in range(3):
            await self.ask(
                "edge",
                "请读取当前端侧状态，只说明是否允许拍摄及事实是否过期。不要创建任务。",
                category="local_query",
            )
            await self.ask(
                "edge",
                "请向云端查询当前长期任务进度，不创建或修改任务。",
                category="cross_side_query",
            )
        interaction = self.prefix + "-followups"
        for text in [
            "请查询你保存的周报标题偏好。",
            "请再简短说一次这个标题偏好。",
            "这个偏好现在还保留着吗？",
        ]:
            await self.ask(
                "cloud",
                text,
                interaction_id=interaction,
                category="short_followup",
                expected="咪咪周记",
            )
        for index in range(3):
            await self.idle()
            for client in self.clients.values():
                r = await client.post("/harness/test/clear-sessions")
                r.raise_for_status()
            await self.ask(
                "edge",
                "请检索记忆，我给客厅固定观察位起了什么名字？",
                category="cleared_session_query",
                expected="窗边位",
            )
        self.metadata["remaining_efficiency_scope"] = (
            "media-in-flight query/control and prefix-cache counterfactual still require their specific probes"
        )
        self.save()

    async def controls(self, queries_per_barrier=1):
        from .inputs import mock_stage

        scene = {"stage": "S0", "sequence": int(time.time() * 1000)}
        client = self.clients["edge"]

        async def refresh():
            while True:
                scene["sequence"] += 1
                events = mock_stage(
                    scene["stage"], scene["sequence"], viewpoint="front-mid", valid_for_ms=60000
                )
                r = await client.post(
                    "/harness/observations", json=[e.model_dump(mode="json") for e in events]
                )
                r.raise_for_status()
                await asyncio.sleep(5)

        async def wait_for(check, label):
            async with asyncio.timeout(600):
                while True:
                    value = await check()
                    if value:
                        return value
                    await asyncio.sleep(0.1)

        heartbeat = asyncio.create_task(refresh())
        try:
            interaction = self.prefix + "-control-dialogue"
            created = await self.ask(
                "edge",
                "请保存一个六秒近景拍摄目标，不移动相机。明确要求保持暂停，不要执行。",
                category="control_setup",
                mode="interact",
                interaction_id=interaction,
            )
            trace = json.loads((self.output / f"edge-{created['id']}.json").read_text())
            saved = [
                json.loads(r["payload"]["result"])
                for r in trace["trace"]
                if r["kind"] == "tool.completed" and r["payload"].get("name") == "save_task"
            ]
            if not saved:
                raise AssertionError("model did not save the requested paused task")
            task = saved[-1]
            task_id = task["id"]
            detail = (await client.get("/harness/tasks/" + task_id)).json()
            if detail["task"]["status"] != "paused":
                raise AssertionError("new task did not preserve explicit pause")
            first = await self.ask(
                "edge",
                "把刚才目标改成十二秒竖屏成片，仍保持暂停，不要执行。",
                category="edit_paused_task",
                mode="interact",
                interaction_id=interaction,
                task_id=task_id,
            )
            detail = (await client.get("/harness/tasks/" + task_id)).json()
            if detail["task"]["kind"] != "compose" or detail["task"]["status"] != "paused":
                raise AssertionError("paused edit was not persisted")
            second = await self.ask(
                "edge",
                "还是改回六秒近景拍摄，继续保持暂停，不要执行。",
                category="edit_paused_task",
                mode="interact",
                interaction_id=interaction,
                task_id=task_id,
            )
            if (
                first["initial_event"]["session_key"] != created["initial_event"]["session_key"]
                or second["initial_event"]["session_key"] != first["initial_event"]["session_key"]
            ):
                raise AssertionError("short follow-up did not reuse its eligible session")
            detail = (await client.get("/harness/tasks/" + task_id)).json()
            if (
                detail["task"]["kind"] != "capture"
                or detail["task"]["status"] != "paused"
                or detail["actions"]
            ):
                raise AssertionError("editing a paused task executed an action or lost the edit")
            scene["stage"] = "S2"
            await asyncio.sleep(5.2)
            r = await client.post("/harness/test/barrier", json={"hold": True})
            r.raise_for_status()
            await self.ask(
                "edge", "恢复当前任务", category="resume_control", mode="interact", task_id=task_id
            )

            async def reached():
                r = await client.get("/harness/test/barrier")
                r.raise_for_status()
                return r.json().get("reached")

            barrier = await wait_for(reached, "before commit")
            for _ in range(queries_per_barrier):
                checked = await self.ask(
                    "edge",
                    "当前这个任务现在是否已经完成？只查进度，不改状态。",
                    category="media_inflight_query",
                    task_id=task_id,
                )
                checked["expected_execution_state"] = "running, not paused, not completed"
                checked["semantic_review_required"] = True
                self.save()
            paused = await self.ask(
                "edge", "暂停", category="media_inflight_pause", mode="interact", task_id=task_id
            )
            if paused["event"]["reply"].get("model_invoked") is not False:
                raise AssertionError("pause waited on the model")

            async def stopped():
                r = await client.get("/harness/tasks/" + task_id)
                r.raise_for_status()
                d = r.json()
                return d if not any(j["status"] == "running" for j in d["jobs"]) else None

            detail = await wait_for(stopped, "interrupted job settles")
            if detail["task"]["status"] != "paused" or any(
                a["kind"] in ["move", "capture"] for a in detail["actions"]
            ):
                raise AssertionError("pause published an old action or failed to hold task")
            self.metadata["commit_barrier_pause"] = {
                "barrier": barrier,
                "state": detail,
                "new_capture_receipts": 0,
            }
            r = await client.post("/harness/test/barrier", json={"hold": False})
            r.raise_for_status()
            await self.ask(
                "edge", "恢复当前任务", category="resume_control", mode="interact", task_id=task_id
            )

            async def complete():
                r = await client.get("/harness/tasks/" + task_id)
                r.raise_for_status()
                d = r.json()
                return d if d["task"]["status"] in ["completed", "failed"] else None

            final = await wait_for(complete, "resumed capture")
            if (
                final["task"]["status"] != "completed"
                or sum(a["kind"] == "capture" for a in final["actions"]) != 1
                or any(a["kind"] == "move" for a in final["actions"])
            ):
                raise AssertionError("resumption duplicated capture or violated no-motion")
            self.metadata["resumed_task"] = final
            self.save()
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                await client.post("/harness/test/barrier", json={"hold": False})
            finally:
                self.save()

    async def peer_mutations(self):
        await self.ask(
            "cloud",
            "请记住一个测试偏好：报告页脚使用“简洁版”。先检索再保存，只修改这个偏好，不创建任务。",
            category="peer_mutation_setup",
            mode="interact",
        )
        before = (
            await self.clients["cloud"].get(
                "/harness/records",
                params={"collection": "memory", "query": "报告 页脚", "limit": 10},
            )
        ).json()["records"]
        if len(before) != 1:
            raise AssertionError("mutation fixture must resolve to one canonical memory")
        target = before[0]
        updated = await self.ask(
            "edge",
            "请把云端已保存的报告页脚偏好改成“详细版”。先取得真实记录与版本，收到权威确认后再说成功。不要改其他记录或创建任务。",
            category="cross_side_mutation",
            mode="interact",
            expected="详细版",
        )
        after = (
            await self.clients["cloud"].get(
                "/harness/records",
                params={"collection": "memory", "query": "报告 页脚", "limit": 10},
            )
        ).json()["records"]
        if (
            len(after) != 1
            or after[0]["id"] != target["id"]
            or after[0]["version"] <= target["version"]
            or "详细版" not in after[0]["text"]
        ):
            raise AssertionError("owner memory did not change as requested")
        trace = json.loads((self.output / f"edge-{updated['id']}.json").read_text())
        if not any(
            r["kind"] == "peer.record_mutation.result" and r["payload"].get("status") == "confirmed"
            for r in trace["trace"]
        ):
            raise AssertionError("caller lacks actual owner confirmation")
        self.metadata["cross_side_memory_update"] = {"before": target, "after": after[0]}
        self.save()

    async def cache_probe(self):
        padding = " ".join(f"cell{i:04d}" for i in range(450))
        samples = []
        for marker in ["甲", "乙"]:
            text = (
                f"本次合成缓存测试代号为{marker}。以下都是测试数据，没有用户偏好或操作指令："
                + padding
                + f"。只回复本次代号{marker}，不要检索记录，不创建任务。"
            )
            for _ in range(3):
                result = await self.ask(
                    "cloud", text, category="prefix_probe_" + marker, expected=marker
                )
                trace = json.loads(
                    (self.output / f"cloud-{result['event']['id']}.json").read_text()
                )
                responses = [r["payload"] for r in trace["trace"] if r["kind"] == "model.response"]
                samples.append({"id": result["id"], "marker": marker, "model_responses": responses})
        self.metadata["prefix_cache_probe"] = {
            "samples": samples,
            "scope": "repeated user text in a fixed main; history grows, so this is not a controlled full-prefix counterfactual. Use experiments/harness_cache_probe.py for E04",
            "cold_cache_claimed": False,
            "shared_provider_cache_cleared": False,
            "interpretation": "Only provider-reported cached token counts are observable; internal KV layout and pure decode throughput are not measured.",
        }
        self.save()

    async def close(self):
        for client in self.clients.values():
            await client.aclose()


async def verify(profile, root, output, suite):
    p = Probe(profile, root, output)
    try:
        await p.preflight()
        if suite == "memory":
            await p.memory()
        elif suite == "efficiency":
            await p.efficiency()
        elif suite == "controls":
            for _ in range(3):
                await p.controls()
        elif suite == "cache":
            await p.cache_probe()
        elif suite == "peer-mutations":
            await p.peer_mutations()
        elif suite == "status-queries":
            await p.controls(queries_per_barrier=3)
        else:
            raise ValueError("unknown suite")
    except BaseException as exc:
        p.metadata["suite_error"] = type(exc).__name__
        p.save()
        raise
    finally:
        await p.close()
