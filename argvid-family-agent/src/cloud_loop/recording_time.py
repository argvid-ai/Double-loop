"""Recording wall clocks from owner manifests, never inferred from wake time."""
import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .store import Conflict


def recording_times(item, start_ms, end_ms=None):
    value = {
        "recorded_start_at": None, "recorded_end_at": None,
        "recording_started_at": None,
        "capture_revision": item.get("capture_revision"),
        "segment_id": item.get("segment_id"),
        "clock_authority": item.get("clock_authority", "unknown"),
        "time_status": "unknown",
    }
    stamp = item.get("recorded_start_at")
    if not stamp:
        return value
    origin = datetime.fromisoformat(stamp)
    if origin.tzinfo is None or origin.utcoffset() is None:
        return value
    base = item["start_ms"]
    if not base <= start_ms <= item["end_ms"]:
        raise Conflict("recording timestamp is outside its owner manifest")
    if end_ms is not None and not start_ms <= end_ms <= item["end_ms"]:
        raise Conflict("recording time range is outside its owner manifest")
    value.update(
        recorded_start_at=(origin + timedelta(milliseconds=start_ms - base)).isoformat(),
        recorded_end_at=(origin + timedelta(milliseconds=end_ms - base)).isoformat()
        if end_ms is not None else None,
        recording_started_at=(origin - timedelta(milliseconds=base)).isoformat(),
        time_status="owner_manifest",
    )
    # Model-facing clock readings are already computed, not an arithmetic
    # exercise over similarly named UTC timestamps and video offsets.
    zone = ZoneInfo("Asia/Shanghai")
    captured = (origin + timedelta(milliseconds=start_ms - base)).astimezone(zone)
    capture_started = (origin - timedelta(milliseconds=base)).astimezone(zone)
    value["display"] = {
        "timezone": str(zone),
        "content_captured_at": captured.isoformat(),
        "continuous_recording_began_at": capture_started.isoformat(),
    }
    value["time_summary"] = (
        f"本次返回画面/范围起点的实际拍摄时刻（北京时间）：{captured.isoformat()}；"
        f"它所属的整段连续录像开始时刻（北京时间）：{capture_started.isoformat()}。"
        "两者均已换算完成，不要再加视频内偏移；现在/唤醒时间不是这两个拍摄时刻。"
    )
    return value


class PeerRecordingTimes:
    """Compatibility with older owners: paginate their existing metadata API.

    Keep only a bounded, in-memory index of immutable sealed manifests. This
    contains no image bytes or control credentials. Incremental cursors avoid
    rescanning old receipts on every model tool call; old cache misses reread.
    """
    def __init__(self, host):
        self.host = host
        self.entries = OrderedDict()
        self.cursors = {}
        self.lock = asyncio.Lock()

    async def lookup(self, principal, source_ref, references):
        scope = (principal, source_ref)
        found = {ref: self.entries[(*scope, ref)] for ref in references
                 if (*scope, ref) in self.entries}
        if len(found) == len(references):
            return found
        async with self.lock, asyncio.timeout(self.host.settings.peer_timeout_seconds):
            cursor = self.cursors.get(scope, -1)
            # First consume new manifests, then reread evicted historical ones
            # on demand. Stop at the observed cursor, not an unbounded live tail.
            for after, historical in [(cursor, False), (-1, True)]:
                if historical and cursor == -1:
                    break
                while len(found) < len(references):
                    response = await self.host.client.get(
                        f"/harness/input-media/sources/{quote(source_ref, safe='')}/entries",
                        params={"after": after, "limit": 1000},
                        timeout=self.host.settings.peer_timeout_seconds,
                    )
                    response.raise_for_status()
                    page = response.json()["entries"]
                    if not page:
                        break
                    previous = after
                    for item in page:
                        if item["source_ref"] != source_ref or item["sequence"] <= after:
                            raise Conflict("recording metadata page identity/order changed")
                        after = item["sequence"]
                        if item["kind"] != "media" or item.get("recording_state") == "growing":
                            continue
                        ref = item["media_ref"]
                        # The raw reader separately authorizes and verifies
                        # actual bytes; metadata does not override availability.
                        selected = {key: item.get(key) for key in (
                            "media_ref", "source_ref", "start_ms", "end_ms", "recorded_start_at",
                            "capture_revision", "segment_id", "clock_authority")}
                        selected["sha256"] = item.get("facts", {}).get("sha256")
                        self.entries[(*scope, ref)] = selected
                        self.entries.move_to_end((*scope, ref))
                        while len(self.entries) > 2048:
                            self.entries.popitem(last=False)
                        if ref in references:
                            found[ref] = selected
                    if not historical:
                        self.cursors[scope] = after
                    if after <= previous or len(page) < 1000 or (historical and after >= cursor):
                        break
            return found


def peer_times(host):
    if not hasattr(host, "_peer_recording_times"):
        host._peer_recording_times = PeerRecordingTimes(host)
    return host._peer_recording_times


async def annotate_markers(host, principal, value):
    """Keep original paging/selection semantics; add clocks to exact media IDs."""
    import httpx

    markers = value.get("markers", [])
    by_source = {}
    for marker in markers:
        if marker.get("source_ref") and marker.get("media_ref"):
            by_source.setdefault(marker["source_ref"], set()).add(marker["media_ref"])
    manifests = {}
    for source, refs in by_source.items():
        try:
            manifests.update(await peer_times(host).lookup(principal, source, refs))
        except (httpx.HTTPError, TimeoutError):
            pass  # Unknown stays unknown, never reuse the event/wake clock.
    mapped = {}
    for marker in markers:
        item = manifests.get(marker.get("media_ref"))
        marker["recording_time"] = recording_times(
            item, marker["start_ms"],
            marker["end_ms"] if marker["end_ms"] <= item["end_ms"] else None) \
            if item else {"time_status": "unknown"}
        if marker.get("id"):
            mapped[marker["id"]] = marker["recording_time"]
    for candidate in value.get("candidates", []):
        for marker in candidate.get("markers", []):
            marker["recording_time"] = mapped.get(marker["marker_id"], {"time_status": "unknown"})
    value["time_basis"] = "recording_time is owner-recorded wall time; start_ms/end_ms are offsets, not time of day"
    return value
