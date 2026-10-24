#!/usr/bin/env bash
# Report a nightly failure against a new Home Assistant as a GitHub issue.
#
# Usage: report_upstream_break.sh <cell> <core-version-or-sha> <junit.xml>...
#
# Finds the open issue labelled `upstream-break` titled "Upstream break: <cell>"
# and comments on it, or creates it. The body lists the failing test node IDs
# (from the JUnit files), the Core version or SHA, and a link to the run.
#
# Needs `gh` with GITHUB_TOKEN (GH_TOKEN) and `issues: write`.
# DRY_RUN=1 prints the gh calls instead of making them; DRY_RUN_EXISTING_ISSUE=<n>
# pretends that issue number is the open one (to exercise the comment path).
set -euo pipefail

cell="${1:?usage: report_upstream_break.sh <cell> <core-version-or-sha> <junit.xml>...}"
core="${2:?missing core version or sha}"
shift 2
(($# >= 1)) || { echo "missing junit.xml" >&2; exit 2; }

title="Upstream break: ${cell}"
if [[ -n "${GITHUB_REPOSITORY:-}" && -n "${GITHUB_RUN_ID:-}" ]]; then
  run_url="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
else
  run_url="(not running in GitHub Actions)"
fi

failing="$(python3 -I - "$@" <<'PY'
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def node_id(classname: str, name: str) -> str:
    """Turn a JUnit classname.name back into a pytest node ID."""
    parts = classname.split(".")
    # The longest dotted prefix that is a file is the module; the rest are classes.
    for i in range(len(parts), 0, -1):
        path = Path(*parts[:i]).with_suffix(".py")
        if path.is_file():
            return "::".join([path.as_posix(), *parts[i:], name])
    return "::".join(["/".join(parts), name])


seen: list[str] = []
for file in sys.argv[1:]:
    try:
        root = ET.parse(file).getroot()
    except (OSError, ET.ParseError):
        continue
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            nid = node_id(case.get("classname", ""), case.get("name", ""))
            if nid not in seen:
                seen.append(nid)
print("\n".join(seen))
PY
)"

MAX_LISTED=100
MAX_BODY_BYTES=60000 # GitHub rejects bodies over 65,536 characters
if [[ -n "$failing" ]]; then
  total="$(grep -c '' <<<"$failing")"
  failing_md="$(head -n "$MAX_LISTED" <<<"$failing" | sed 's/^/- `/; s/$/`/')"
  if ((total > MAX_LISTED)); then
    failing_md+=$'\n'"- … and $((total - MAX_LISTED)) more"
  fi
else
  failing_md="_No failing tests in the JUnit results: the job failed before or outside the tests (install or collection error). See the run._"
fi

body="$(cat <<BODY
Nightly cell \`${cell}\` failed against Home Assistant Core \`${core}\`.

Failing tests:

${failing_md}

Run: ${run_url}
BODY
)"

# The body goes to gh through a file (no argument-size limit) and is capped in
# bytes at a UTF-8 character boundary, whatever the locale.
body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT
printf '%s' "$body" | python3 -I -c '
import sys
limit = int(sys.argv[1])
data = sys.stdin.buffer.read()
if len(data) > limit:
    note = "\n\n_(truncated: body too long)_".encode()
    data = data[: limit - len(note)].decode("utf-8", errors="ignore").encode() + note
sys.stdout.buffer.write(data)
' "$MAX_BODY_BYTES" >"$body_file"

gh_() {
  if [[ "${DRY_RUN:-}" == "1" ]]; then
    local shown=() file=""
    while (($#)); do
      if [[ "$1" == "--body-file" ]]; then
        file="$2"
        shown+=("$1" "<body>")
        shift
      else
        shown+=("$1")
      fi
      shift
    done
    printf 'DRY_RUN: gh'
    printf ' %q' "${shown[@]}"
    printf '\n'
    if [[ -n "$file" ]]; then
      cat "$file"
      echo
    fi
  else
    gh "$@"
  fi
}

if [[ "${DRY_RUN:-}" == "1" ]]; then
  gh_ issue list --label upstream-break --state open --limit 100 --json number,title
  existing="${DRY_RUN_EXISTING_ISSUE:-}"
else
  existing="$(gh issue list --label upstream-break --state open --limit 100 --json number,title \
    | jq -r --arg title "$title" 'map(select(.title == $title)) | .[0].number // empty')"
fi

if [[ -n "$existing" ]]; then
  gh_ issue comment "$existing" --body-file "$body_file"
else
  gh_ label create upstream-break --color B60205 \
    --description "Nightly CI failed against a newer Home Assistant" --force
  gh_ issue create --title "$title" --label upstream-break --body-file "$body_file"
fi
