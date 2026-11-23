#!/usr/bin/env bash
set -euo pipefail
director_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
director_python=${R1_DIRECTOR_PYTHON:-"$HOME/yolo_test/p1-yolo-test/.venv/bin/python"}
export LD_LIBRARY_PATH="/usr/share/cix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
exec "$director_python" -u "$director_root/run.py" "$@"
