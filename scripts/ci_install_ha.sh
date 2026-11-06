#!/usr/bin/env bash
# Install Home Assistant Core at a git ref, the pytest-homeassistant package
# from that same checkout, and the test tooling.
#
# Why: the nightly tests Cores that have no PyPI release, such as `rc` and
# `dev`. pytest-homeassistant lives in Core and PyPI's build requires the
# homeassistant of the same version, so a Core from git needs its own copy. For
# the pinned release, install requirements_ha.txt instead.
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
PLUGIN_DIR="$CORE_DIR/packages/pytest-homeassistant"
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
run rm -rf "$CORE_DIR"
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

echo "== 3. install pytest-homeassistant from this Core"
if [[ "${DRY_RUN:-}" != "1" && ! -d "$PLUGIN_DIR" ]]; then
  echo "Core $ref has no packages/pytest-homeassistant" >&2
  exit 1
fi
run "${PIP[@]}" install "$PLUGIN_DIR"

echo "== 4. install Core's pins for what the tests import but the plugin doesn't require"
if [[ "${DRY_RUN:-}" == "1" ]]; then
  run "${PIP[@]}" install "<home-assistant-frontend and aiohasupervisor pins from $CORE_DIR/requirements_all.txt>"
else
  mapfile -t pins < <(grep -E '^(home-assistant-frontend|aiohasupervisor)==' "$CORE_DIR/requirements_all.txt")
  "${PIP[@]}" install "${pins[@]}"
fi

echo "== 5. install the repository's tooling and extra test requirements"
# requirements_test.txt deliberately lists neither the plugin nor Home
# Assistant, so this cannot downgrade Core.
run "${PIP[@]}" install -r "$REPO/requirements_test.txt"

echo "== 6. installed Core version"
run python -c "import homeassistant.const as c; print(c.__version__)"
