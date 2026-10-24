#!/usr/bin/env bash
# Resolve a nightly matrix cell (min | stable | beta | dev) to a Home Assistant
# Core git ref (a tag or branch name) and print it on stdout.
#
#   min     tag 2026.11.0 if it exists, else the `rc` branch
#   stable  the newest release tag (YYYY.M.P, no beta)
#   beta    the newest YYYY.M.PbN tag that is newer than the stable release,
#           else the `rc` branch
#   dev     `dev`
#
# CORE_TAGS_FILE=<file> reads the tag list (one tag name per line) instead of
# querying GitHub, so the logic can be tested offline.
set -euo pipefail

cell="${1:?usage: ci_resolve_ref.sh <min|stable|beta|dev>}"
MIN_TAG="2026.11.0"
CORE_URL="https://github.com/home-assistant/core"

tags() {
  if [[ -n "${CORE_TAGS_FILE:-}" ]]; then
    cat "$CORE_TAGS_FILE"
  else
    git ls-remote --tags --refs "$CORE_URL" | sed 's#.*refs/tags/##'
  fi
}

all_tags="$(tags)"
stable="$(grep -E '^[0-9]{4}\.[0-9]+\.[0-9]+$' <<<"$all_tags" | sort -V | tail -n 1 || true)"

case "$cell" in
  min)
    if grep -qx "$MIN_TAG" <<<"$all_tags"; then echo "$MIN_TAG"; else echo "rc"; fi
    ;;
  stable)
    [[ -n "$stable" ]] || { echo "no stable tag found" >&2; exit 1; }
    echo "$stable"
    ;;
  beta)
    beta="$(grep -E '^[0-9]{4}\.[0-9]+\.[0-9]+b[0-9]+$' <<<"$all_tags" | sort -V | tail -n 1 || true)"
    # A beta is only useful while it is ahead of the newest stable release.
    if [[ -n "$beta" ]] && [[ "${beta%%b*}" != "$stable" ]] \
      && [[ "$(printf '%s\n%s\n' "${beta%%b*}" "$stable" | sort -V | tail -n 1)" == "${beta%%b*}" ]]; then
      echo "$beta"
    else
      echo "rc"
    fi
    ;;
  dev)
    echo "dev"
    ;;
  *)
    echo "unknown cell: $cell" >&2
    exit 2
    ;;
esac
