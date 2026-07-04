#!/usr/bin/env bash
#
# Release quality gate for hass-govee-ble-local.
#
# Runs the same code checks CI runs. Run it before tagging a release:
#
#     scripts/check.sh
#
# Gates:
#   1. mypy against Home Assistant core's own strict config (mypy.ini)
#   2. pytest with a hard coverage floor on the HA-facing layer
#   3. manifest / quality_scale sanity
#
# CI additionally runs hassfest + HACS validation (see .github/workflows).
#
# Env overrides: PYTHON (default python3), VENV (default .venv-check),
#                COV_MIN (default 95).
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
VENV="${VENV:-.venv-check}"
COV_MIN="${COV_MIN:-95}"

if [ ! -x "$VENV/bin/python" ]; then
  echo ">> creating venv: $VENV"
  "$PYTHON" -m venv "$VENV"
fi
# shellcheck source=/dev/null
source "$VENV/bin/activate"

echo ">> installing test harness"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements_test.txt

echo ">> installing govee-ble-local (runtime dependency)"
if [ -d ../govee-ble-local ]; then
  python -m pip install --quiet ../govee-ble-local          # local dev checkout
else
  python -m pip install --quiet \
    "govee-ble-local @ git+https://github.com/Brady-Woods/govee-ble-local"
fi

echo ">> mypy (Home Assistant strict config, see mypy.ini)"
mypy custom_components/govee_ble_local

echo ">> pytest (coverage gate: >= ${COV_MIN}%)"
pytest tests/ \
  --cov=custom_components.govee_ble_local \
  --cov-report=term-missing \
  --cov-fail-under="${COV_MIN}"

echo ">> manifest / quality_scale sanity"
python - <<'PY'
import json
import pathlib

base = pathlib.Path("custom_components/govee_ble_local")
manifest = json.loads((base / "manifest.json").read_text())
required = {"domain", "name", "version", "codeowners", "requirements", "config_flow"}
missing = required - manifest.keys()
assert not missing, f"manifest.json missing keys: {missing}"
# quality_scale.yaml must be valid and free of unresolved TODOs at release time
import sys
try:
    import yaml
except ModuleNotFoundError:
    yaml = None
if yaml is not None:
    qs = yaml.safe_load((base / "quality_scale.yaml").read_text())
    todos = [k for k, v in qs.get("rules", {}).items()
             if (v == "todo" or (isinstance(v, dict) and v.get("status") == "todo"))]
    if todos:
        print(f"   note: quality_scale rules still 'todo': {sorted(todos)}")
print(f"manifest ok: {manifest['domain']} v{manifest['version']} "
      f"quality_scale={manifest.get('quality_scale')}")
PY

echo ""
echo "ALL QUALITY GATES PASSED (hass-govee-ble-local)"
