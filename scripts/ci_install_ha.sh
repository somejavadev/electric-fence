#!/usr/bin/env bash
# Install Home Assistant Core at a git ref plus the test tooling, without the
# PyPI pytest-homeassistant-custom-component.
#
# Why: the PyPI plugin has no build for HA 2026.11 (0.13.374 pins 2026.10.3,
# which this repository's tests are not compatible with). The plugin is
# generated from the checked-out Core instead (scripts/build_phcc.py), so CI
# tests the same Core the integration will run on.
#
# Usage: ci_install_ha.sh <core-ref>      (a tag or branch of home-assistant/core)
# DRY_RUN=1 prints the commands without cloning or installing.
#
# Writes CORE_SHA and CORE_VERSION to $GITHUB_ENV (when set) and prints them.
set -euo pipefail

ref="${1:?usage: ci_install_ha.sh <core-ref>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
WORK="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
CORE_DIR="$WORK/ha-core"
PHCC_DIR="$WORK/phcc"
PIP=(python -m pip --disable-pip-version-check)

run() {
  if [[ "${DRY_RUN:-}" == "1" ]]; then
    printf 'DRY_RUN:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

echo "== 1. shallow clone of home-assistant/core at $ref"
run rm -rf "$CORE_DIR" "$PHCC_DIR"
run git clone --depth 1 --branch "$ref" https://github.com/home-assistant/core "$CORE_DIR"
if [[ "${DRY_RUN:-}" == "1" ]]; then
  core_sha="<sha of $ref>"
  core_version="<version of $ref>"
else
  core_sha="$(git -C "$CORE_DIR" rev-parse HEAD)"
  # Version constants are plain assignments; read them without importing Core.
  core_version="$(python - "$CORE_DIR/homeassistant/const.py" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
parts = [re.search(rf"^{n}(?:: Final)? = \"?([^\"\n]+)\"?$", text, re.M).group(1)
         for n in ("MAJOR_VERSION", "MINOR_VERSION", "PATCH_VERSION")]
print(".".join(parts))
PY
)"
fi
echo "CORE_SHA=$core_sha"
echo "CORE_VERSION=$core_version"
if [[ -n "${GITHUB_ENV:-}" ]]; then
  {
    echo "CORE_SHA=$core_sha"
    echo "CORE_VERSION=$core_version"
  } >> "$GITHUB_ENV"
fi

echo "== 2. install Core (with its runtime dependencies)"
run "${PIP[@]}" install "$CORE_DIR"

echo "== 3. generate the test plugin from this Core"
run python "$HERE/build_phcc.py" --core "$CORE_DIR" --out "$PHCC_DIR"

echo "== 4. install the plugin's requirements and the pinned frontend"
run "${PIP[@]}" install -r "$PHCC_DIR/requirements_phcc.txt"
if [[ "${DRY_RUN:-}" == "1" ]]; then
  run "${PIP[@]}" install "<home-assistant-frontend pin from $CORE_DIR/requirements_all.txt>"
else
  frontend="$(grep -E '^home-assistant-frontend==' "$CORE_DIR/requirements_all.txt")"
  "${PIP[@]}" install "$frontend"
fi

echo "== 5. install the generated plugin (no dependencies)"
run "${PIP[@]}" install --no-deps "$PHCC_DIR"

echo "== 6. install the repository's tooling and extra test requirements"
# requirements_test.txt deliberately lists neither the plugin nor Home
# Assistant, so this cannot reinstall PyPI phcc or downgrade Core.
run "${PIP[@]}" install -r "$REPO/requirements_test.txt"

echo "== 7. installed Core version"
run python -c "import homeassistant.const as c; print(c.__version__)"
