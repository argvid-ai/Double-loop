"""Record an opted-in field shot from the director's existing camera stream."""

import json
import math
import re
import time
from uuid import uuid4


def read_state(path):
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_state(path, value):
    pending = path.with_suffix('.pending')
    pending.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')
    pending.replace(path)


class CaptureRecorder:
    def __init__(self, root, session):
        self.request = root / 'record-request.json'
        self.status = root / 'record-status.json'
        self.folder = session / 'clips'
        self.seen = read_state(self.status).get('id')
        self.writer = None
        self.info = None

    def finish(self, failed=False):
        if self.writer is None:
            return
        self.writer.release()
        self.writer = None
        self.info['state'] = 'saved' if not failed and self.info['frames'] and self.video.is_file() else 'failed'
        write_state(self.video.with_suffix('.json'), self.info)
        write_state(self.status, self.info)

    def frame(self, image, now, fps):
        import cv2

        request = read_state(self.request)
        key = request.get('id')
        if self.writer is not None and (key != self.seen or request.get('op') == 'stop' or now >= self.expires):
            self.finish(failed=key != self.seen or now >= self.expires)
        expires = request.get('expires')
        if (self.writer is None and isinstance(key, str) and re.fullmatch(r'[a-f0-9]{32}', key)
                and key != self.seen and request.get('op') == 'start' and type(expires) in (int, float)
                and math.isfinite(expires) and now < expires <= now + 300):
            self.seen = key
            self.expires = expires
            width = min(image.shape[1], 1280) & ~1
            height = (round(width * image.shape[0] / image.shape[1])) & ~1
            self.size = (width, height)
            self.folder.mkdir(exist_ok=True)
            self.video = self.folder / f'{key}.avi'
            self.info = {'id': key, 'video': str(self.video.resolve()), 'frames': 0, 'fps': fps}
            self.writer = cv2.VideoWriter(str(self.video), cv2.VideoWriter_fourcc(*'MJPG'), fps, self.size)
            if not self.writer.isOpened():
                self.finish()
                return
        if self.writer is not None:
            self.writer.write(cv2.resize(image, self.size, interpolation=cv2.INTER_AREA))
            self.info['frames'] += 1
            self.info['last_frame_monotonic_s'] = now
            if self.info['frames'] == 1:
                self.info.update(state='recording', first_frame_monotonic_s=now)
                write_state(self.status, self.info)


class FieldRecording:
    def __init__(self, root):
        self.request = root / 'record-request.json'
        self.status = root / 'record-status.json'
        self.key = None

    def wait(self, states, deadline, stop=None):
        while time.monotonic() < deadline and (stop is None or not stop.is_set()):
            result = read_state(self.status)
            if result.get('id') == self.key and result.get('state') in states:
                return result
            time.sleep(.05)
        return {}

    def start(self, latest_start, stop):
        now = time.monotonic()
        if now >= latest_start:
            return {}
        self.key = uuid4().hex
        write_state(self.request, {'id': self.key, 'op': 'start', 'expires': now + 240})
        return self.wait({'recording', 'failed'}, min(now + 2.0, latest_start), stop)

    def finish(self):
        if self.key is None:
            return {}
        try:
            write_state(self.request, {'id': self.key, 'op': 'stop'})
            return self.wait({'saved', 'failed'}, time.monotonic() + 2.0)
        finally:
            self.key = None
