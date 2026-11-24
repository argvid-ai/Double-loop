#!/bin/sh
set -eu
cd "$(dirname "$0")/argvid-family-agent"
exec sh start.sh "$@"
