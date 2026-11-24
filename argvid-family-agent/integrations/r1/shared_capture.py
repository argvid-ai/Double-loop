"""Read-only R1 input adapter. It never opens a camera, sends controls, or saves images."""

import base64
import hashlib
import ipaddress
import json
import time
import urllib.parse
import urllib.request


class SharedFrames:
    def __init__(self, endpoint, max_age_seconds=8):
        parsed = urllib.parse.urlsplit(endpoint)
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            local = False
        if (
            parsed.scheme != "http" or not local or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or not parsed.path.startswith("/harness/shared-recording/")
            or not parsed.path.endswith("/frame")
        ):
            raise ValueError("shared input must be a loopback recording-frame endpoint")
        self.endpoint = endpoint
        self.max_age_seconds = max_age_seconds
        self.last_identity = None
        self.metadata = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def read(self):
        with self.opener.open(self.endpoint, timeout=5) as response:
            content = response.read(8 * 1024**2 + 1)
        if len(content) > 8 * 1024**2:
            raise ValueError("shared image response exceeds bound")
        value = json.loads(content)
        raw = base64.b64decode(value.pop("content_base64"), validate=True)
        if value.get("input_kind") != "saved_recording":
            raise ValueError("input is not a saved recording")
        if value.get("simulated") is not False:
            raise ValueError("live R1 input requires real recorded camera frames")
        if hashlib.sha256(raw).hexdigest() != value["image_sha256"]:
            raise ValueError("shared image checksum differs")
        age = time.time() - value["captured_at"]
        if not -1 <= age <= self.max_age_seconds:
            raise ValueError("shared frame is stale")
        identity = (value["capture_revision"], value["media_ref"], value["frame_time_ms"])
        if identity == self.last_identity:
            return None
        self.last_identity = identity
        self.metadata = value
        return time.monotonic() - max(age, 0), raw
