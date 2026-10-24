#!/usr/bin/env bash
#
# Release quality gate for hass-govee-ble-local.
#
# Runs the same code checks CI runs. Run it before tagging a release:
#
#     scripts/check.sh
#
# Gates:
#   1. ruff lint (ruff.toml), the integration, its tests and tools/
#   2. mypy against Home Assistant core's own strict config (mypy.ini), the integration and
#      the field-test harness (tools/ha_field_test)
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
# A library checkout that no longer tracks its generated Kaitai classes
# (src/govee_ble_local/_generated/) builds them from spec/*.ksy on every install, which
# needs the JVM kaitai-struct-compiler 0.11 on PATH (or KSC=/path/to/it); without it the
# gate stops before installing. Released wheels and sdists ship the classes, so CI and
# FROM_MANIFEST=1 need no compiler.
#
# Env overrides: PYTHON (default python3), VENV (default .venv-check),
#                COV_MIN (default 95), FROM_MANIFEST (default unset),
#                LIB_VERSION (floor | newest, default floor),
#                KSC (the JVM Kaitai compiler, for a library checkout that generates it).
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
VENV="${VENV:-.venv-check}"
COV_MIN="${COV_MIN:-95}"

# Print the JVM kaitai-struct-compiler 0.11.x that ../govee-ble-local's build would use
# (the candidates its tools/gen_kaitai.sh tries), or nothing.
find_jvm_ksc() {
  local cand path ver
  for cand in ${KSC:+"$KSC"} kaitai-struct-compiler ksc; do
    path="$(command -v "$cand" 2>/dev/null)" || continue
    ver="$("$path" --version 2>/dev/null | head -n 1)" || continue
    case "$ver" in
      "kaitai-struct-compiler 0.11" | "kaitai-struct-compiler 0.11."*)
        echo "$path ($ver)"
        return 0
        ;;
    esac
  done
}

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
  # Untracked generated classes = the checkout builds them at install time. No silent
  # fallback to the manifest pin: this branch exists to test the working tree.
  if ! git -C ../govee-ble-local ls-files --error-unmatch \
    src/govee_ble_local/_generated/govee_ble_frame.py >/dev/null 2>&1; then
    if ! KSC_FOUND="$(find_jvm_ksc)" || [ -z "$KSC_FOUND" ]; then
      {
        echo "error: ../govee-ble-local generates its Kaitai classes at build time and needs the"
        echo "       JVM kaitai-struct-compiler 0.11 (brew install kaitai-struct-compiler;"
        echo "       Debian/Ubuntu: bash ../govee-ble-local/scripts/install_ksc.sh), or set"
        echo "       FROM_MANIFEST=1 to test the released pin instead."
      } >&2
      exit 1
    fi
    echo "   generates its Kaitai classes at build time with $KSC_FOUND"
  fi
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
ruff check custom_components tests brands tools

echo ">> mypy (Home Assistant strict config, see mypy.ini)"
mypy custom_components/govee_ble_local
# The field-test harness (docs/field-test-ha.md): a dev tool, held to the same bar.
mypy tools/ha_field_test

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
