#!/usr/bin/env bash
# systemd calls this launcher once per process, so each can restart independently.
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"
python_bin="${BIODIGESTER_PYTHON:-$project_dir/.venv/bin/python}"

if [[ ! -x "$python_bin" ]]; then
    echo "Python environment missing: $python_bin. Install requirements-pi.txt in .venv first." >&2
    exit 78
fi

export PYTHONUNBUFFERED=1
case "${1:-}" in
    temperature) exec "$python_bin" -m pi.temp ;;
    pressure)
        if [[ -n "${PRESSURE_MODE:-}" ]]; then
            exec "$python_bin" -m pi.pressure --mode "$PRESSURE_MODE"
        else
            exec "$python_bin" -m pi.pressure
        fi
        ;;
    peltier) exec "$python_bin" -m pi.peltier ;;
    controller) exec "$python_bin" controller.py ;;
    dashboard) exec "$python_bin" dashboard.py ;;
    oled) exec "$python_bin" -m pi.oled ;;
    *) echo "Usage: $0 {temperature|pressure|peltier|controller|dashboard|oled}" >&2; exit 78 ;;
esac
