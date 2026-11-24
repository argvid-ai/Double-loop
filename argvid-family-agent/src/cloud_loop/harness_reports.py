"""Idempotent text deliverables and a durable notification outbox, never agent prompts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .contracts import canonical, digest
from .store import Conflict


class MatterReports:
    def __init__(self, host):
        self.host, self.store = host, host.store
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS report_period_evidence (
                    event_id TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS matter_reports (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, version INTEGER NOT NULL,
                    principal TEXT NOT NULL, source_event TEXT NOT NULL,
                    content TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS report_delivery (
                    id TEXT PRIMARY KEY, direction TEXT NOT NULL, body TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_try REAL NOT NULL DEFAULT 0, error TEXT
                );
            """)
            # Scheduled reports keep their task/version identity. Operational
            # goal updates have an event identity and may share a task version.
            old_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='matter_reports'"
            ).fetchone()[0]
            if "UNIQUE(task_id,version)" in old_sql.replace(" ", ""):
                db.execute("BEGIN IMMEDIATE")
                db.execute("ALTER TABLE matter_reports RENAME TO matter_reports_previous")
                db.execute("""CREATE TABLE matter_reports (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, version INTEGER NOT NULL,
                    principal TEXT NOT NULL, source_event TEXT NOT NULL,
                    content TEXT NOT NULL, created REAL NOT NULL)""")
                db.execute("INSERT INTO matter_reports SELECT * FROM matter_reports_previous")
                db.execute("DROP TABLE matter_reports_previous")

    def period(self, event, task_id):
        task = self.store.business_task(task_id, event["principal"])
        if task["kind"] != "report":
            raise Conflict("period lookup requires a report matter")
        with self.store.connect() as db:
            occurrence = db.execute(
                "SELECT * FROM wake_occurrences WHERE event_id=?", (event["id"],)
            ).fetchone()
            if occurrence and occurrence["task_id"] != task_id:
                raise Conflict("report period must belong to the triggering task")
            published = db.execute(
                "SELECT id FROM matter_reports WHERE task_id=? AND version=?",
                (task_id, task["version"]),
            ).fetchone()
        spec = (task.get("schedule") or {}).get("spec", {})
        zone = ZoneInfo(spec.get("timezone", "Asia/Shanghai"))
        due = occurrence["due"] if occurrence else self.store.clock()
        end = datetime.fromtimestamp(due, zone).replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=1)
        return {
            "task_id": task_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "date": start.date().isoformat(),
            "weekday": "星期" + "一二三四五六日"[start.weekday()],
            "timezone": str(zone),
            "time_basis": "scheduled_occurrence" if occurrence else "current_host_day",
            "occurrence_id": occurrence["id"] if occurrence else None,
            "task_version": task["version"],
            "published_for_this_occurrence": published["id"] if published else None,
        }

    def window_records(self, principal, start, end):
        if not 0 < end - start <= 90000:
            raise Conflict("report window must be one local calendar day")
        # Historical committed receipts, not a claim that files remain readable
        # today. Both kinds use completion/save time, not reserve/capture time.
        jobs = """SELECT 'job' AS record_type,j.id,j.task_id,j.kind AS operation,j.version,
            b.goal,MIN(a.created) AS completed_at,j.result AS info
            FROM harness_jobs j JOIN business_tasks b ON b.id=j.task_id
            JOIN audit a ON json_extract(a.payload,'$.job_id')=j.id AND a.kind='job.finished'
            WHERE b.principal=? AND j.status='completed' AND json_extract(j.result,'$.verified')=1
            GROUP BY j.id HAVING MIN(a.created)>=? AND MIN(a.created)<?"""
        with self.store.connect() as db:
            db.execute("BEGIN")
            sql = jobs
            parameters = [principal, start, end]
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='observation_exports'"
            ).fetchone():
                sql += """ UNION ALL SELECT 'observation' AS record_type,e.id,e.task_id,'observation_save' AS operation,
                    json_extract(e.body,'$.version') AS version,'' AS goal,MIN(a.created) AS completed_at,f.info
                    FROM observation_exports e JOIN assets f ON f.id=e.id AND f.task_id=e.task_id AND f.kind='observation'
                    JOIN audit a ON a.kind='observation.export.saved' AND a.task_id=e.task_id
                        AND json_extract(a.payload,'$.asset_id')=e.id
                    WHERE e.principal=? AND e.status='saved'
                        AND json_extract(e.verified,'$.file_bytes_real')=1
                        AND json_extract(f.info,'$.asset.sha256')=json_extract(e.verified,'$.sha256')
                        AND json_extract(a.payload,'$.sha256')=json_extract(e.verified,'$.sha256')
                    GROUP BY e.id HAVING MIN(a.created)>=? AND MIN(a.created)<?"""
                parameters.extend([principal, start, end])
            grouped = db.execute(
                "SELECT operation,COUNT(*) n FROM (" + sql + ") GROUP BY operation", parameters
            ).fetchall()
            counts = {row["operation"]: row["n"] for row in grouped}
            rows = db.execute(
                "SELECT * FROM (" + sql + ") ORDER BY completed_at,record_type,id LIMIT 100",
                parameters,
            ).fetchall()
        records = []
        record_bytes = 2
        for row in rows:
            info = json.loads(row["info"])
            observation = row["record_type"] == "observation"
            record = {
                "id": self.host.side + ":" + row["record_type"] + ":" + row["id"],
                "side": self.host.side,
                "task_id": row["task_id"],
                "task_version": row["version"],
                "operation": row["operation"],
                "goal": row["goal"][:160],
                "completed_at": row["completed_at"],
                "asset_id": row["id"] if observation else info.get("asset_id"),
                "sha256": info["asset"]["sha256"] if observation else info.get("sha256"),
                "simulated": info.get("simulated", True),
                "authority": "committed_verified_observation_save"
                if observation
                else "committed_verified_job_receipt",
                "current_file_availability_checked": False,
            }
            if observation:
                record.update(
                    project_id=info["project_id"],
                    source_ref=info["source_ref"],
                    generation=info["selection"]["generation"],
                    inference_simulated=info["selection"]["inference_simulated"],
                    requested_start_ms=info["requested_start_ms"],
                    requested_end_ms=info["requested_end_ms"],
                    file_duration_ms=info["asset"]["duration_ms"],
                )
            size = len(canonical(record).encode()) + (1 if records else 0)
            if record_bytes + size > 6000:
                break
            records.append(record)
            record_bytes += size
        total = sum(counts.values())
        return {
            "side": self.host.side,
            "records": records,
            "window_start": start,
            "window_end": end,
            "total_count": total,
            "returned_count": len(records),
            "truncated": total > len(records),
            "counts_by_operation": counts,
            "record_page_byte_limit": 6000,
            "observation_receipts_supported": True,
        }

    async def read_period(self, event, task_id):
        period = self.period(event, task_id)
        start = datetime.fromisoformat(period["start"]).timestamp()
        end = datetime.fromisoformat(period["end"]).timestamp()
        own = self.window_records(event["principal"], start, end)
        records = list(own["records"])
        peer_available = True
        coverage = {self.host.side: {k: v for k, v in own.items() if k != "records"}}
        try:
            response = await self.host.client.post(
                "/harness/reports/period",
                json={
                    "principal": event["principal"],
                    "start": period["start"],
                    "end": period["end"],
                },
            )
            response.raise_for_status()
            remote = response.json()
            if remote.get("side") != self.host.peer:
                raise ValueError("wrong evidence owner")
            if not isinstance(remote.get("records"), list) or len(remote["records"]) > 100:
                raise ValueError("invalid report evidence page")
            if any(
                row.get("side") != self.host.peer
                or not start <= row.get("completed_at", float("-inf")) < end
                for row in remote["records"]
            ):
                raise ValueError("remote report records outside requested window")
            coverage[self.host.peer] = {k: v for k, v in remote.items() if k != "records"}
            records += remote["records"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            peer_available = False
        value = {
            "period": period,
            "records": records,
            "peer_available": peer_available,
            "coverage": coverage,
            "complete_index": peer_available
            and all(
                c.get("observation_receipts_supported") is True and c.get("truncated") is False
                for c in coverage.values()
            ),
            "meaning": "Historical verified job completion and observation-save receipts for all owned projects in this window. Counts include all matching receipts; records are capped100 and6000UTF8bytes per node. Missing peer/legacy coverage is unknown, not zero. Files are not re-read or uploaded; replay is not live capture.",
        }
        with self.store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO report_period_evidence VALUES (?,?)",
                (event["id"], canonical(value)),
            )
        self.store.audit(
            task_id,
            "report.period.read",
            {
                "event_id": event["id"],
                "period": period,
                "source_ids": [r["id"] for r in records],
                "peer_available": peer_available,
            },
        )
        return value

    def publish(self, event, args):
        if event["read_only"]:
            raise Conflict("query cannot publish a deliverable")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.store._task(db, args.task_id, event["principal"])
            operational_update = task["kind"] == "goal" and event["kind"] != "timer"
            identity = {"task": task["id"], "version": task["version"]}
            if operational_update:
                identity["event"] = event["id"]
            ident = "report-" + digest(identity)[:32]
            if task["kind"] not in {"report", "goal"}:
                raise Conflict("this matter does not authorize a text report")
            if task["version"] != event.get("_task_versions", {}).get(task["id"]):
                raise Conflict("read latest task before publishing")
            active = db.execute(
                "SELECT status FROM harness_events WHERE id=?", (event["id"],)
            ).fetchone()
            if not active or active["status"] != "running":
                raise Conflict("input no longer active")
            previous = db.execute(
                "SELECT content FROM matter_reports WHERE id=?", (ident,)
            ).fetchone()
            if previous:
                return {**json.loads(previous["content"]), "duplicate": True}
            if task["status"] in {"paused", "cancelled", "completed", "expired"}:
                raise Conflict("matter does not permit publication")
            rule = db.execute(
                "SELECT * FROM wake_rules WHERE task_id=? AND deleted=0", (task["id"],)
            ).fetchone()
            occurrence = db.execute(
                "SELECT * FROM wake_occurrences WHERE event_id=?", (event["id"],)
            ).fetchone()
            if rule and operational_update:
                spec = json.loads(rule["spec"])
            elif rule:
                if (
                    not occurrence
                    or occurrence["rule_revision"] != rule["revision"]
                    or occurrence["task_version"] != task["version"]
                ):
                    raise Conflict("scheduled report requires its current occurrence")
                # once becomes disabled after dispatch; its unchanged revision
                # still authorizes this already-dispatched occurrence.
                if not rule["enabled"] and json.loads(rule["spec"])["schedule"] != "once":
                    raise Conflict("rule disabled")
                spec = json.loads(rule["spec"])
            else:
                if occurrence:
                    raise Conflict("rule was deleted before publication")
                spec = {
                    "delivery_side": self.host.side,
                    "delivery_channel": event["channel"]
                    if event["channel"] in {"voice", "web"}
                    else "web",
                }
            evidence_row = db.execute(
                "SELECT value FROM report_period_evidence WHERE event_id=?", (event["id"],)
            ).fetchone()
            evidence = json.loads(evidence_row["value"]) if evidence_row else None
            if occurrence and (
                not evidence or evidence["period"]["task_version"] != task["version"]
            ):
                raise Conflict(
                    "read_report_period for this occurrence before publishing; prior reports and host today do not define this period"
                )
            if evidence and evidence["period"]["task_version"] != task["version"]:
                raise Conflict("report period evidence belongs to an older task version")
            if evidence:
                expected_period = self.period(event, task["id"])
                if any(
                    evidence["period"].get(key) != expected_period[key]
                    for key in (
                        "task_id",
                        "task_version",
                        "occurrence_id",
                        "start",
                        "end",
                        "timezone",
                        "time_basis",
                    )
                ):
                    raise Conflict(
                        "report period evidence belongs to another task, occurrence or window"
                    )
            sources = []
            for source_id in args.source_ids:
                row = (
                    next((r for r in evidence["records"] if r["id"] == source_id), None)
                    if evidence
                    else None
                )
                if row is not None:
                    sources.append(
                        {
                            "collection": "observation_receipt"
                            if row.get("authority") == "committed_verified_observation_save"
                            else "job_receipt",
                            "id": source_id,
                            "side": row["side"],
                        }
                    )
                    continue
                if occurrence:
                    raise Conflict(
                        "report reference is not a verified completion receipt in this occurrence window"
                    )
                found = None
                for table in ("business_tasks", "record_history", "user_memory"):
                    row = db.execute(
                        "SELECT * FROM " + table + " WHERE id=? AND principal=?",
                        (source_id, event["principal"]),
                    ).fetchone()
                    if row:
                        found = {"collection": table, "id": source_id}
                        break
                if found is None:
                    raise Conflict("report cites missing or unowned record")
                sources.append(found)
            report_text = args.text
            if evidence:
                notes = []
                if not evidence["peer_available"]:
                    notes.append("对侧记录当前不可读，本报告不能代表双端全部记录。")
                if any(
                    c.get("observation_receipts_supported") is not True
                    for c in evidence.get("coverage", {}).values()
                ):
                    notes.append("部分节点的记录接口尚未确认覆盖观察片段。")
                if any(c.get("truncated") for c in evidence.get("coverage", {}).values()):
                    notes.append("回执列表仅展示部分条目，总数按各节点本期汇总记录计算。")
                if notes:
                    report_text = "\n".join(notes) + "\n" + report_text
                report_text = (
                    "报告期间："
                    + evidence["period"]["date"]
                    + "（"
                    + expected_period["weekday"]
                    + "，"
                    + evidence["period"]["timezone"]
                    + "）。\n文件回放/Mock作业记录，非真实相机采集。\n"
                    + report_text
                )
            content = {
                "id": ident,
                "task_id": task["id"],
                "task_version": task["version"],
                "text": report_text,
                "period": evidence["period"] if evidence else None,
                "evidence_records": evidence["records"] if evidence else [],
                "evidence_coverage": evidence.get("coverage", {}) if evidence else {},
                "sources": sources,
                "source_event": event["id"],
                "occurrence_id": occurrence["id"] if occurrence else None,
                "sender": self.host.side,
                "recipient": spec["delivery_side"],
                "channel": spec["delivery_channel"],
                "principal": event["principal"],
                "created": self.store.clock(),
                "content_origin": "model_generated_report",
                "audio_generated": False,
                "simulated": spec["delivery_channel"] == "voice",
            }
            from .harness_contracts import ReportDelivery

            ReportDelivery.model_validate(content)
            db.execute(
                "INSERT INTO matter_reports VALUES (?,?,?,?,?,?,?)",
                (
                    ident,
                    task["id"],
                    task["version"],
                    event["principal"],
                    event["id"],
                    canonical(content),
                    self.store.clock(),
                ),
            )
            self.store._history(
                db,
                ident,
                event["principal"],
                task["id"],
                "report",
                args.text,
                event["id"],
                self.store.clock(),
            )
            if content["recipient"] == self.host.side:
                self._receive(db, content, local=True)
            else:
                db.execute(
                    "INSERT INTO report_delivery(id,direction,body,status) VALUES (?,'out',?,'pending')",
                    (ident, canonical(content)),
                )
            report_result = {
                "report_id": ident,
                "text": args.text,
                "simulated": content["simulated"],
                "verified": True,
            }
            prior = task.get("result") or {}
            if task["kind"] == "goal" and any(
                key in prior for key in ("asset_id", "review_required", "media_manifest")
            ):
                # A progress report is another deliverable, not a replacement
                # for the current film's render/review receipt.
                report_result = {**prior, "latest_report_id": ident}
            db.execute(
                "UPDATE business_tasks SET result=?,status=?,updated=? WHERE id=?",
                (
                    canonical(report_result),
                    task["status"] if task["kind"] == "goal" else "ready" if rule else "completed",
                    self.store.clock(),
                    task["id"],
                ),
            )
            self.store._audit(
                db, task["id"], "report.published", {"event_id": event["id"], "report": content}
            )
            return {
                **content,
                "status": "stored"
                if content["recipient"] == self.host.side
                else "pending_delivery",
                "duplicate": False,
            }

    def _receive(self, db, body, *, local=False):
        if body["recipient"] != self.host.side or (not local and body["sender"] != self.host.peer):
            raise Conflict("wrong report direction")
        existing = db.execute(
            "SELECT body FROM report_delivery WHERE id=?", (body["id"],)
        ).fetchone()
        if existing:
            if existing["body"] != canonical(body):
                raise Conflict("report identity collision")
            return {"accepted": True, "duplicate": True}
        db.execute(
            "INSERT INTO report_delivery(id,direction,body,status) VALUES (?,'in',?,'delivered')",
            (body["id"], canonical(body)),
        )
        conversation = self.store.conversations.main(body["principal"], db=db)
        event_id = "notice-" + body["id"]
        reply = {
            "text": body["text"],
            "source": "committed_report",
            "report_id": body["id"],
            "model_invoked": False,
            "audio_generated": False,
            "simulated": body["simulated"],
            "session_key": conversation["id"],
        }
        delivery = self.store.delivery.offer(body["principal"], body["task_id"], {
            "id": event_id, "target": conversation["id"], "channel": body["channel"],
            "kind": "report.notification",
            "body": {"text": body["text"], "report": body,
                     "origin_event_id": body["source_event"]},
            "reply": reply,
        }, db=db)
        self.store._audit(
            db,
            None,
            "report.delivered",
            {
                "event_id": event_id,
                "report_id": body["id"],
                "sender": body["sender"],
                "channel": body["channel"],
                "simulated": body["simulated"],
                "audio_generated": False, "user_notification": delivery["status"],
            },
        )
        return {"accepted": True, "duplicate": False}

    def receive(self, body):
        from .harness_contracts import ReportDelivery

        value = ReportDelivery.model_validate(body).model_dump(mode="json", exclude_unset=True)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._receive(db, value)

    async def deliver_once(self):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM report_delivery WHERE direction='out' AND status='pending' AND next_try<=?",
                (self.store.clock(),),
            ).fetchall()
        for row in rows:
            body = json.loads(row["body"])
            try:
                response = await self.host.client.post("/harness/reports/receive", json=body)
                response.raise_for_status()
                if response.json().get("accepted") is not True:
                    raise ValueError("missing report receipt")
            except (httpx.HTTPError, ValueError) as exc:
                attempts = row["attempts"] + 1
                permanent = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {
                    400,
                    401,
                    403,
                    404,
                    409,
                    422,
                }
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE report_delivery SET attempts=?,status=?,next_try=?,error=? WHERE id=?",
                        (
                            attempts,
                            "failed"
                            if permanent or attempts >= self.host.profile.delivery_max_attempts
                            else "pending",
                            self.store.clock() + min(30, 2**attempts),
                            type(exc).__name__,
                            row["id"],
                        ),
                    )
            else:
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE report_delivery SET status='delivered' WHERE id=?", (row["id"],)
                    )

    def list(self, principal):
        with self.store.connect() as db:
            return [
                {**dict(r), "body": json.loads(r["body"])}
                for r in db.execute(
                    "SELECT * FROM report_delivery WHERE json_extract(body,'$.principal')=? ORDER BY rowid DESC LIMIT 100",
                    (principal,),
                )
            ]
