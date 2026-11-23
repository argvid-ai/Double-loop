#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
runner=${R1_PLAYER:-"$root/build/r1_play"}
calibration=${R1_CALIBRATION:-"$HOME/r1-tracker/r1-7a_sdk/config/joint_calib_right.txt"}
port=${R1_PORT:-/dev/ttyUSB0}
camera=
for device in /dev/v4l/by-id/*C60E*video-index0; do
    if [[ -e "$device" ]]; then camera=$device; break; fi
done

case "${1:-check}" in
    check)
        printf 'Board: %s\n' "$(uname -m)"
        "$runner" --help
        printf 'Calibration file and SHA-256\n'
        sha256sum -- "$calibration"
        if [[ -c "$port" ]]; then
            printf 'Arm serial: %s\n' "$port"
        else
            printf 'Arm serial not detected: %s\n' "$port"
        fi
        if [[ -n "$camera" ]]; then
            printf 'Camera: %s\n' "$camera"
            if command -v fuser >/dev/null && fuser -s "$camera"; then
                printf 'Camera is in use. Reuse the current view or close its capture application before opening a new preview.\n'
            fi
            v4l2-ctl -d "$camera" --list-formats-ext | awk '/MJPG|3840x2160|1920x1080/'
        else
            printf 'C60E capture device not detected\n'
        fi
        ;;
    snapshot|pan|pan-record|pan-wide-record)
        if [[ ! -c "$port" ]]; then
            printf 'Arm serial not detected: %s\n' "$port" >&2
            exit 1
        fi
        exec python3 "$root/field/session.py" "$1" --runner "$runner" --port "$port" --calibration "$calibration"
        ;;
    *)
        printf 'Usage: %s [check|snapshot|pan|pan-record|pan-wide-record]\n' "$0" >&2
        exit 2
        ;;
esac
