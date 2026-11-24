"""Persist admission counts independently of audit rotation and process restarts."""

from .store import Conflict


def reserve_request(store, budget, event_id, request_id):
    if budget is None:
        return
    with store.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS model_request_budgets (
            id TEXT PRIMARY KEY, ceiling INTEGER NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS model_request_admissions (
            budget_id TEXT NOT NULL, event_id TEXT NOT NULL, request_id TEXT NOT NULL,
            created REAL NOT NULL, PRIMARY KEY(budget_id,event_id,request_id))""")
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR IGNORE INTO model_request_budgets VALUES (?,?)", (budget.id, budget.limit)
        )
        ceiling = db.execute(
            "SELECT ceiling FROM model_request_budgets WHERE id=?", (budget.id,)
        ).fetchone()[0]
        if ceiling != budget.limit:
            raise Conflict("persistent model budget identity cannot change its ceiling")
        exists = db.execute(
            "SELECT 1 FROM model_request_admissions WHERE budget_id=? AND event_id=? AND request_id=?",
            (budget.id, event_id, request_id),
        ).fetchone()
        if exists:
            # A replayed transport admission is not a new request. Pi generates
            # fresh request IDs for retries, including compaction requests.
            return
        count = db.execute(
            "SELECT COUNT(*) FROM model_request_admissions WHERE budget_id=?", (budget.id,)
        ).fetchone()[0]
        if count >= ceiling:
            raise Conflict("persistent model request budget exhausted")
        db.execute(
            "INSERT INTO model_request_admissions VALUES (?,?,?,?)",
            (budget.id, event_id, request_id, store.clock()),
        )
