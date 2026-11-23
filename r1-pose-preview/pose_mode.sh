#!/bin/bash
set -euo pipefail

project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
servo="$project/servo"
python="$HOME/yolo_test/p1-yolo-test/.venv/bin/python"
diagnostics="$servo/diagnostics"
pidfile="$diagnostics/pose_preview.pid"
preview="$servo/pose_preview.py"
export R1CAM_W=3840 R1CAM_H=2160 LD_LIBRARY_PATH=/usr/share/cix/lib

preview_owned() {
    [[ -f "$pidfile" ]] || return 1
    local pid
    read -r pid < "$pidfile"
    [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]] || return 1
    tr '\0' '\n' < "/proc/$pid/cmdline" | grep -Fxq -- "$preview"
}

preview_ready() {
    curl -fsS --max-time 1 http://127.0.0.1:8796/status >/dev/null 2>&1
}

start_preview() {
    mkdir -p "$diagnostics"
    if preview_owned && preview_ready; then return; fi
    if preview_ready; then
        echo '8796 already has a preview service. Free it before starting this instance.' >&2
        return 1
    fi
    nohup "$python" -u "$preview" > "$diagnostics/pose_preview.log" 2>&1 < /dev/null &
    printf '%s\n' "$!" > "$pidfile"
    for attempt in {1..30}; do
        if preview_ready; then return; fi
        sleep 0.2
    done
    tail -12 "$diagnostics/pose_preview.log" >&2
    return 1
}

stop_preview() {
    if ! preview_owned; then return; fi
    if fuser -s /dev/ttyUSB0; then
        echo 'Finish the active arm session before switching camera modes.' >&2
        return 1
    fi
    kill -TERM "$(cat "$pidfile")"
    for attempt in {1..30}; do
        if ! preview_ready; then return; fi
        sleep 0.2
    done
    echo 'The preview is still using the camera.' >&2
    return 1
}

mode=$1
shift
case "$mode" in
    --pose-preview) start_preview ;;
    --pose-stop-preview) stop_preview ;;
    --pose-check)
        start_preview
        "$python" "$servo/build_gesture_arm.py"
        "$python" "$servo/gesture_trial.py" ;;
    --pose)
        start_preview
        exec "$python" -u "$servo/gesture_trial.py" --live "$@" ;;
    *) exit 2 ;;
esac
