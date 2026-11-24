"""Read JSON candidates from stdin; count metadata, never infer visual quality."""

import json
import sys


def summarize(value):
    candidates = value["candidates"]
    if not isinstance(candidates, list) or len(candidates) > 10000:
        raise ValueError("candidates must be a list of at most 10000 entries")
    ids = set()
    sources = set()
    duration = 0
    for candidate in candidates:
        ident = candidate["id"]
        source = candidate["source_ref"]
        ms = candidate["duration_ms"]
        if not isinstance(ident, str) or not ident or len(ident) > 200 or ident in ids:
            raise ValueError("candidate IDs must be unique")
        if not isinstance(source, str) or not source or len(source) > 400:
            raise ValueError("source_ref required")
        if isinstance(ms, bool) or not isinstance(ms, int) or not 0 < ms <= 3600000:
            raise ValueError("duration_ms must be positive and bounded")
        ids.add(ident)
        sources.add(source)
        duration += ms
    return {
        "candidate_count": len(ids),
        "source_count": len(sources),
        "total_duration_ms": duration,
        "authority": "input_manifest_metadata_only",
        "visual_quality_assessed": False,
    }


if __name__ == "__main__":
    try:
        print(json.dumps(summarize(json.load(sys.stdin)), ensure_ascii=False))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        print(json.dumps({"error": "invalid_candidate_manifest"}))
        sys.exit(2)
