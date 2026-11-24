"""Bound retrieval payloads; full records remain available by id and offset."""

from copy import deepcopy


def search_view(result):
    view = deepcopy(result)
    for record in view.get("records", []):
        text = record.get("text")
        if isinstance(text, str) and len(text) > 1000:
            terms = [s for s in view.get("terms", []) if s]
            at = next((text.find(t) for t in terms if text.find(t) >= 1000), -1)
            excerpt = text[:800]
            if at >= 0:
                excerpt += "\n[matching excerpt]\n" + text[max(0, at - 100) : at + 400]
            record.update(
                text=excerpt,
                text_truncated=True,
                total_chars=len(text),
                continuation={
                    "tool": "read_record",
                    "collection": record["collection"],
                    "record_id": record["id"],
                    "offset": 0,
                },
            )
        if record.get("collection") == "tasks" and isinstance(record.get("result"), dict):
            full = record["result"]
            record["result"] = {
                k: full[k]
                for k in ("verified", "asset_id", "sha256", "error", "simulated")
                if k in full
            }
    return view
