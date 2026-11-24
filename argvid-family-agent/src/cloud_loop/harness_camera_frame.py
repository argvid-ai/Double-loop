"""Bounded preview worker: no recording, no vision inference, no URL in logs."""

import json
import sys
from pathlib import Path

import av


def main():
    value = json.loads(Path(sys.argv[1]).read_text())
    uri = value["device"] if value["kind"] == "usb" else value["url"]
    options = {"rtsp_transport": "tcp"} if value["kind"] == "rtsp" else {}
    with av.open(
        uri,
        format="video4linux2" if value["kind"] == "usb" else None,
        options=options,
        timeout=(5, 5),
    ) as stream:
        frame = next(stream.decode(video=0))
        image = frame.to_image()
        image.thumbnail((960, 960))
        image.save(value["output"])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(1)
