#!/usr/bin/env bash
# Print "true" when a Home Assistant Core version is at least the integration's
# minimum (MIN_HA_VERSION in const.py), else "false".
#
# The nightly matrix resolves some cells to an older Core until a new minimum
# is released (for example `rc` is still 2026.10 before the 2026.11 betas).
# Those cells are skipped with a notice instead of failing and opening a false
# `upstream-break` issue.
#
# Usage: ci_supported_core.sh <core-version>   (e.g. 2026.11.0b3, 2026.11.0.dev0)
#        ci_supported_core.sh --print-minimum
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

minimum="$(sed -nE 's/^MIN_HA_VERSION = "([^"]+)"$/\1/p' custom_components/electric_fence/const.py)"
[[ -n "$minimum" ]] || { echo "MIN_HA_VERSION not found in const.py" >&2; exit 2; }

version="${1:?usage: ci_supported_core.sh <core-version> | --print-minimum}"
if [[ "$version" == "--print-minimum" ]]; then
  echo "$minimum"
  exit 0
fi

# Betas and dev builds of the minimum release count as supported: compare the
# release part only (2026.11.0b3 and 2026.11.0.dev0 -> 2026.11.0).
release="$(sed -E 's/^([0-9]+\.[0-9]+\.[0-9]+).*/\1/' <<<"$version")"
if [[ "$(printf '%s\n%s\n' "$minimum" "$release" | sort -V | head -n 1)" == "$minimum" ]]; then
  echo "true"
else
  echo "false"
fi
