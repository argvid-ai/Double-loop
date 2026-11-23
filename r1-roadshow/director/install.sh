#!/usr/bin/env bash
set -euo pipefail
director_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
director_python=${R1_DIRECTOR_PYTHON:-"$HOME/yolo_test/p1-yolo-test/.venv/bin/python"}
: "${R1_VLM_ENDPOINT:?Set R1_VLM_ENDPOINT to the VLM service URL}"
: "${R1_VLM_START_SCRIPT:?Set R1_VLM_START_SCRIPT to the installed model startup script}"
[[ "$R1_VLM_ENDPOINT" =~ ^https?://[^[:space:]]+$ ]] || {
    printf 'R1_VLM_ENDPOINT must be an http(s) URL without spaces\n' >&2
    exit 1
}
[[ "$R1_VLM_START_SCRIPT" = /* && "$R1_VLM_START_SCRIPT" != *$'\n'* ]] || {
    printf 'R1_VLM_START_SCRIPT must be an absolute path without newlines\n' >&2
    exit 1
}
test -x "$director_python"
test -f "$R1_VLM_START_SCRIPT"
"$director_python" - "$director_root" <<'PY'
from pathlib import Path
import json,sys
root=Path(sys.argv[1])
sys.path.insert(0,str(root))
from run import load_manifest
config=root/'config.json'
assert json.loads(config.read_text())['mode']=='dry-run'
load_manifest(json.loads(config.read_text()),config)
print('Checked simulation plan hashes and dry-run configuration')
PY
mkdir -p "$HOME/.config/systemd/user"
printf 'R1_VLM_ENDPOINT=%s\nR1_VLM_START_SCRIPT=%s\n' \
    "$R1_VLM_ENDPOINT" "$R1_VLM_START_SCRIPT" > "$HOME/.config/r1-director.env"
chmod 600 "$HOME/.config/r1-director.env"
systemctl --user stop r1-director.service 2>/dev/null || true
# Replace the temporary model service from initial bring-up, if present.
if systemctl --user show r1-director-vlm.service -p FragmentPath --value | grep -q '/transient/'; then
    systemctl --user stop r1-director-vlm.service
    systemctl --user reset-failed r1-director-vlm.service 2>/dev/null || true
fi
install -m 644 "$director_root/r1-director.service" "$director_root/r1-director-vlm.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now r1-director-vlm.service
for director_attempt in {1..30}; do
    if curl -fsS --max-time 1 "${R1_VLM_ENDPOINT%/}/health" >/dev/null; then
        systemctl --user enable --now r1-director.service
        systemctl --user is-active r1-director-vlm.service r1-director.service
        exit 0
    fi
    sleep 1
done
printf 'VLM health check timed out; inspect journalctl --user -u r1-director-vlm\n' >&2
exit 1
