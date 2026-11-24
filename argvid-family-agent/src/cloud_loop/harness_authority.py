"""Shared scope checks for owner-directed background work."""

import json

from .store import Conflict


LOCAL_WORK_KINDS = {
    "user",
    "voice",
    "autonomy",
    "work.message",
    "storage.review",
    "timer",
    "media.review",
    "task.step_completed",
    "observation.alert",
    "peer.result",
    "peer.failed",
    "scene.changed",
    "task.resumed",
}

RECORDING_CONTROLS = {
    "configure_observation", "control_observation", "control_observation_task",
    "start_observation", "configure_default_observation", "configure_recording_policy",
}


def recording_owner_input(event):
    """Forwarding a human's task does not delegate the shared recorder switch."""
    return (
        not event["read_only"]
        and event["kind"] in {"user", "voice"}
        and ":main:" in (event.get("session_key") or "")
    )


def require_recording_owner(store, event, name, args):
    if name not in RECORDING_CONTROLS:
        return
    if event["read_only"]:
        raise Conflict("read-only input cannot control shared recording")
    # Incident handling may recover analysis without changing camera ownership.
    if (event["kind"] == "observation.alert" and not event["read_only"]
            and name == "control_observation_task" and args.get("scope") == "analysis"):
        saved = store.event(event["id"], event["principal"])
        if (saved["kind"] != "observation.alert" or saved["read_only"]
                or saved["status"] != "running"
                or saved["body"].get("health") != event["body"].get("health")):
            raise Conflict("analysis recovery requires its saved active incident")
        return  # The host additionally checks the persisted incident and generation.
    saved = store.event(event["id"], event["principal"])
    if (not recording_owner_input(saved) or saved["status"] != "running"
            or saved["session_key"] != event.get("session_key")
            or saved["kind"] != event["kind"]):
        raise Conflict("shared recording control requires explicit user input in the main conversation")
    conversation = store.conversations.get(event["principal"], saved["session_key"])
    if conversation["kind"] != "main":
        raise Conflict("shared recording control requires the main conversation")


def local_work(event):
    return not event["read_only"] and event["kind"] in LOCAL_WORK_KINDS


def user_directed(event):
    return event["kind"] in {"user", "voice"} or (
        event["kind"] == "work.message"
        and (
            event["body"].get("user_directed")
            or event["body"].get("source_kind") in {"user", "voice"}
        )
    )


def parent_tasks(db, task_id, principal):
    parents, visited = set(), {task_id}
    while task_id:
        row = db.execute(
            """SELECT e.task_id,e.kind,e.body FROM task_routes r
            JOIN harness_events e ON e.id=r.source_event
            WHERE r.task_id=? AND r.principal=? AND e.principal=?""",
            (task_id, principal, principal),
        ).fetchone()
        if not row or row[0] in visited or row[1] in {"user", "voice", "autonomy"}:
            break
        if user_directed({"kind": row[1], "body": json.loads(row[2])}) and row[0]:
            parent = db.execute(
                "SELECT status FROM business_tasks WHERE id=? AND principal=?", (row[0], principal)
            ).fetchone()
            if parent and parent[0] in {"completed", "cancelled", "expired"}:
                break
        task_id = row[0]
        if task_id:
            parents.add(task_id)
            visited.add(task_id)
    return parents


def related_tasks(store, event):
    """Resolve task ancestry from saved routes, not from searched task versions."""
    tasks = set()
    if event.get("task_id"):
        tasks.add(event["task_id"])
    with store.connect() as db:
        tasks.update(
            row[0]
            for row in db.execute(
                "SELECT task_id FROM task_routes WHERE source_event=? AND principal=?",
                (event["id"], event["principal"]),
            )
        )
        saved = db.execute(
            "SELECT task_id,kind,read_only FROM harness_events WHERE id=? AND principal=?",
            (event["id"], event["principal"]),
        ).fetchone()
        if not saved or saved[1] != event["kind"] or (saved[2] and not event["read_only"]):
            raise Conflict("work trigger differs from its saved input")
        if saved[0]:
            tasks.add(saved[0])
        for task_id in list(tasks):
            tasks.update(parent_tasks(db, task_id, event["principal"]))
    return tasks


def writable_work_handoff(store, event, target):
    """Delegate to an owned descendant without granting direct sibling actions."""
    saved = store.event(event["id"], event["principal"])
    if saved["status"] != "running":
        raise Conflict("work handoff input is no longer running")
    if saved["kind"] != event["kind"]:
        raise Conflict("work trigger differs from its saved input")
    if event["read_only"] or saved["read_only"]:
        return False
    if event["kind"] in {"user", "voice"}:
        return True  # A human may discuss a finished task or explicitly resume a pause.
    if not local_work(saved) or target["status"] in {"paused", "completed", "cancelled", "expired"}:
        return False
    scoped = related_tasks(store, saved)
    with store.connect() as db:
        parents = parent_tasks(db, target["id"], event["principal"])
        # Use the sending work itself, not all its ancestors: siblings cannot
        # inherit the parent's delegation authority merely by sharing it.
        descendant = saved.get("task_id") in parents
        if event["kind"] != "autonomy" and target["id"] not in scoped and not descendant:
            return False
        owners = parents | {target["id"]}
        if saved.get("task_id"):
            owners.add(saved["task_id"])
            owners.update(parent_tasks(db, saved["task_id"], event["principal"]))
        return all(
            store._task(db, ident, event["principal"])["status"]
            not in {"paused", "completed", "cancelled", "expired"}
            for ident in owners
        )


def require_work_target(store, event, task_id):
    if event["kind"] in {"user", "voice", "autonomy", "peer.control"}:
        return
    if not local_work(event) or task_id not in related_tasks(store, event):
        raise Conflict("background action must belong to this work or its parent task")
    with store.connect() as db:
        for parent in parent_tasks(db, task_id, event["principal"]):
            task = store._task(db, parent, event["principal"])
            if task["status"] in {"paused", "cancelled", "expired", "completed"}:
                raise Conflict("parent task no longer authorizes background work")
