"""Read-only local device discovery. Enumeration never means successful capture."""

from __future__ import annotations

import fcntl
import os
import platform
import re
import struct
from pathlib import Path


def linux_devices(*, sys_root=Path("/sys"), proc_root=Path("/proc"), dev_root=Path("/dev")):
    devices = []
    for entry in sorted((sys_root / "class/video4linux").glob("video*")):
        node = dev_root / entry.name
        name = (entry / "name").read_text().strip() if (entry / "name").exists() else entry.name
        record = {
            "id": "v4l2:" + entry.name,
            "kind": "video",
            "driver": "v4l2",
            "address": str(node),
            "name": name,
            "enumerated": node.exists(),
            "capture_verified": False,
            "capture_capable": None,
            "connection": "unknown",
        }
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
            try:
                data = bytearray(104)
                fcntl.ioctl(fd, 0x80685600, data, True)  # VIDIOC_QUERYCAP; no streaming ioctl.
                caps, device_caps = struct.unpack_from("II", data, 84)
                effective = device_caps if caps & 0x80000000 else caps
                bus = bytes(data[48:80]).split(b"\0")[0].decode(errors="replace")
                record.update(
                    capture_capable=bool(effective & (1 | 0x1000)),
                    streaming_capable=bool(effective & 0x04000000),
                    connection="usb" if bus.startswith("usb-") else "platform",
                    bus=bus,
                )
            finally:
                os.close(fd)
        except OSError as exc:
            record["probe_error"] = type(exc).__name__
        devices.append(record)
    pcm = proc_root / "asound/pcm"
    if pcm.exists():
        for line in pcm.read_text().splitlines():
            match = re.match(r"(\d+)-(\d+):\s*(.*?)\s*:\s*.*?capture\s+(\d+)", line)
            if match and int(match[4]) > 0:
                address = f"hw:{int(match[1])},{int(match[2])}"
                devices.append(
                    {
                        "id": "alsa:" + address,
                        "kind": "audio",
                        "driver": "alsa",
                        "address": address,
                        "name": match[3],
                        "enumerated": True,
                        "capture_capable": True,
                        "capture_verified": False,
                    }
                )
    return devices


def discover_inputs():
    system = platform.system()
    return {
        "platform": system,
        "devices": linux_devices() if system == "Linux" else [],
        "scan_supported": system == "Linux",
        "automatic_binding": False,
        "capture_started": False,
        "phones": {
            "android": "paired_browser_camera_microphone",
            "iphone": "paired_browser_camera_microphone",
            "requires_secure_browser_context": True,
            "requires_user_permission": True,
            "physical_verification": False,
        },
        "ptz": {
            "real_driver_installed": False,
            "note": "Only explicit simulation exists; no real motion is inferred.",
        },
        "note": "Device nodes and capability flags are discovery evidence only. Actual frames/audio and phone permissions must be verified after binding.",
    }
