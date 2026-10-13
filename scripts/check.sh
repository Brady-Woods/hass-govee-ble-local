#!/usr/bin/env bash
#
# Release quality gate for hass-govee-ble-local.
#
# Runs the same code checks CI runs. Run it before tagging a release:
#
#     scripts/check.sh
#
# Gates:
#   1. ruff lint (ruff.toml)
#   2. mypy against Home Assistant core's own strict config (mypy.ini)
#   3. pytest with hard statement+branch coverage floors on the HA-facing layer
#   4. manifest / quality_scale sanity
#
# CI additionally runs hassfest + HACS validation (see .github/workflows).
#
# The library is installed exactly as manifest.json pins it. In a dev checkout
# (no CI) with ../govee-ble-local present, that working tree is installed
# instead - unless FROM_MANIFEST=1 - and its version must still equal the pin.
#
# Env overrides: PYTHON (default python3), VENV (default .venv-check),
#                COV_MIN (default 95), FROM_MANIFEST (default unset).
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
python -m pip install --quiet "pip==26.2.1"
python -m pip install --quiet -r requirements_test.txt

echo ">> installing govee-ble-local (runtime dependency)"
REQ="$(python -c 'import json;print(json.load(open("custom_components/govee_ble_local/manifest.json"))["requirements"][0])')"
if [ -d ../govee-ble-local ] && [ -z "${CI:-}" ] && [ -z "${FROM_MANIFEST:-}" ]; then
  echo "   ../govee-ble-local working tree ($(git -C ../govee-ble-local rev-parse --short HEAD 2>/dev/null || echo '?')) - set FROM_MANIFEST=1 to test the pin itself"
  python -m pip install --quiet ../govee-ble-local
else
  # Test exactly what ships: the requirement pinned in manifest.json.
  echo "   $REQ"
  python -m pip install --quiet "$REQ"
fi
python - "$REQ" <<'PY'
import importlib.metadata
import sys

from packaging.requirements import Requirement

req = Requirement(sys.argv[1])
installed = importlib.metadata.version(req.name)
assert req.specifier.contains(installed, prereleases=True), (
    f"installed {req.name} {installed} does not satisfy the manifest pin {req}"
)
print(f"   {req.name} {installed} satisfies {req}")
PY

echo ">> ruff (lint, see ruff.toml)"
ruff check custom_components tests brands

echo ">> mypy (Home Assistant strict config, see mypy.ini)"
mypy custom_components/govee_ble_local

echo ">> pytest (coverage gate: >= ${COV_MIN}% statements+branches)"
pytest tests/ \
  --cov=custom_components.govee_ble_local \
  --cov-branch \
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
# A claimed quality-scale tier requires every rule to be done or exempt: any
# open rule (even a Bronze one) caps every tier.
import yaml  # installed with homeassistant

qs = yaml.safe_load((base / "quality_scale.yaml").read_text())
todos = sorted(
    k for k, v in qs.get("rules", {}).items()
    if v == "todo" or (isinstance(v, dict) and v.get("status") == "todo")
)
if manifest.get("quality_scale"):
    assert not todos, (
        f"manifest claims quality_scale={manifest['quality_scale']!r} "
        f"but these rules are still 'todo': {todos}"
    )
print(f"manifest ok: {manifest['domain']} v{manifest['version']} "
      f"quality_scale={manifest.get('quality_scale')}")
PY

echo ""
echo "ALL QUALITY GATES PASSED (hass-govee-ble-local)"
