#!/bin/sh
set -eu
cd "$(dirname "$0")"
umask 077
action=${1:-start}
profile=${ARGVID_PROFILE:-profiles/harness.local.json}
case "$action" in start|stop|status) ;; *) echo "用法: sh start.sh [start|status|stop]" >&2; exit 2;; esac
command -v uv >/dev/null 2>&1 || { echo "请先安装 uv。" >&2; exit 1; }
[ -r "$profile" ] || { echo "请按 README 创建本机 profile，并配置模型地址和模型 ID。" >&2; exit 1; }
if [ "$action" = start ]; then
    : "${CLOUD_MODEL_API_KEY:?请设置自己的 CLOUD_MODEL_API_KEY}"
    uv sync --locked
    uv run --no-sync python - "$profile" <<'PY'
import json, sys
from urllib.parse import urlsplit
with open(sys.argv[1], encoding="utf-8") as f:
    profile = json.load(f)
for side in ("cloud", "edge"):
    settings = profile[side]
    url = urlsplit(settings.get("model_base_url") or profile.get("model_base_url", ""))
    local = url.hostname in {"localhost", "127.0.0.1", "::1"}
    if (not url.hostname or url.hostname.endswith(".invalid") or
            (url.scheme != "https" and not (local and url.scheme == "http")) or
            url.username or url.password or url.query or url.fragment or not settings.get("model")):
        sys.exit("请配置自己的 HTTPS 模型地址与模型 ID；本机模型也可使用回环 HTTP。")
PY
    uv run --no-sync cloud-loop harness start --side edge --profile "$profile" >/dev/null
    if ! uv run --no-sync cloud-loop harness start --side cloud --profile "$profile" >/dev/null; then
        echo "主服务未能启动；使用 sh start.sh status 查看两端状态，或用 sh start.sh stop 停止本配置。" >&2
        exit 1
    fi
    echo "双服务已启动。可用 sh start.sh status 查看健康状态。"
else
    for side in cloud edge; do
        uv run --no-sync cloud-loop harness "$action" --side "$side" --profile "$profile"
    done
fi
