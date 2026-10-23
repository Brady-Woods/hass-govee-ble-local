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
#   5. README.md's supported-models table matches scripts/gen_supported_models.py (only
#      when the library installed is the floor release, which the table describes)
#
# CI additionally runs hassfest + HACS validation (see .github/workflows).
#
# manifest.json declares a semver RANGE for the library (>=floor,<next-major): only a
# major release may break the API. Home Assistant won't upgrade an installed version
# that still satisfies the range, so users can be on the floor long after newer
# releases exist. LIB_VERSION picks which end is tested:
#   floor  (default) - exactly the range's minimum (the oldest version a user may have)
#   newest           - the newest release in the range (what a fresh install gets)
# CI runs both. In a dev checkout (no CI) with ../govee-ble-local present, that working
# tree is installed instead - unless FROM_MANIFEST=1 - and must still satisfy the range.
#
# Env overrides: PYTHON (default python3), VENV (default .venv-check),
#                COV_MIN (default 95), FROM_MANIFEST (default unset),
#                LIB_VERSION (floor | newest, default floor).
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
# Set when the floor release is what gets installed: README.md's model table describes it.
FLOOR_RELEASE=""
REQ="$(python -c 'import json;print(json.load(open("custom_components/govee_ble_local/manifest.json"))["requirements"][0])')"
if [ -d ../govee-ble-local ] && [ -z "${CI:-}" ] && [ -z "${FROM_MANIFEST:-}" ]; then
  echo "   ../govee-ble-local working tree ($(git -C ../govee-ble-local rev-parse --short HEAD 2>/dev/null || echo '?')) - set FROM_MANIFEST=1 to test the pin itself"
  python -m pip install --quiet ../govee-ble-local
else
  case "${LIB_VERSION:-floor}" in
    floor)
      # The range's minimum, exactly: the integration must work on the oldest version it allows.
      SPEC="$(python - "$REQ" <<'PY'
import sys

from packaging.requirements import Requirement

req = Requirement(sys.argv[1])
floors = [s.version for s in req.specifier if s.operator == ">="]
assert len(floors) == 1, f"manifest requirement {req} needs exactly one '>=' floor"
print(f"{req.name}=={floors[0]}")
PY
)"
      FLOOR_RELEASE=1 ;;
    newest) SPEC="$REQ" ;;
    *) echo "LIB_VERSION must be 'floor' or 'newest', got '${LIB_VERSION}'" >&2; exit 2 ;;
  esac
  echo "   $SPEC  (LIB_VERSION=${LIB_VERSION:-floor}, manifest: $REQ)"
  python -m pip install --quiet --upgrade "$SPEC"
fi
python - "$REQ" <<'PY'
import importlib.metadata
import sys

from packaging.requirements import Requirement

req = Requirement(sys.argv[1])
installed = importlib.metadata.version(req.name)
assert req.specifier.contains(installed, prereleases=True), (
    f"installed {req.name} {installed} does not satisfy the manifest requirement {req}"
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

echo ">> README model table (floor release only)"
if [ -z "$FLOOR_RELEASE" ]; then
  echo "   skipped: README.md lists the floor release's models, and this run tests another" \
    "govee-ble-local (LIB_VERSION=floor checks it; in a dev checkout, with FROM_MANIFEST=1)"
# A build of a local tree (it carries a direct_url.json) can have the floor's version
# number without being the floor release, so the version alone doesn't say.
elif python -c '
import importlib.metadata
import sys

sys.exit(importlib.metadata.distribution("govee-ble-local").read_text("direct_url.json") is not None)
'; then
  python scripts/gen_supported_models.py --check
  echo "   README.md lists the floor release's models"
else
  echo "   skipped: the govee-ble-local in $VENV is a local build, not the floor release" \
    "(pip kept it: its version number is the floor's); a fresh VENV installs the release"
fi

echo ""
echo "ALL QUALITY GATES PASSED (hass-govee-ble-local)"
