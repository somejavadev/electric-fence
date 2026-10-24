#!/usr/bin/env bash
# Daily watch on Home Assistant Core: list PRs merged into dev in the last 24
# hours that touch code Electric Fence depends on, and post one comment on the
# pinned issue labelled `upstream-watch` (created if missing). Posts nothing
# when no PR matches.
#
# Watched paths: homeassistant/auth/**, homeassistant/components/{http,auth,
# websocket_api,persistent_notification,config}/**, homeassistant/helpers/http.py,
# homeassistant/config_entries.py.
#
# Needs `gh` with GITHUB_TOKEN (GH_TOKEN) and `issues: write`.
# DRY_RUN=1 prints the gh write calls instead of making them.
# UPSTREAM_FIXTURE=<file> replaces the GitHub reads with a JSON file:
#   {"pulls": [<closed-PR list as returned by the API>],
#    "files": {"<number>": ["<changed path>", ...]}}
# UPSTREAM_FAIL_FILES=<number> makes the changed-files call for that PR fail (tests).
# UPSTREAM_NOW=<ISO-8601 UTC> overrides "now" (for tests).
set -euo pipefail

now="${UPSTREAM_NOW:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
since="$(date -u -d "$now - 24 hours" +%Y-%m-%dT%H:%M:%SZ)"

api() {
  gh api "$@"
}

if [[ -n "${UPSTREAM_FIXTURE:-}" ]]; then
  pulls="$(jq '.pulls' "$UPSTREAM_FIXTURE")"
else
  pulls="$(api "repos/home-assistant/core/pulls?state=closed&base=dev&sort=updated&direction=desc&per_page=100")"
fi

changed_files() {
  local number="$1"
  if [[ "${UPSTREAM_FAIL_FILES:-}" == "$number" ]]; then
    echo "simulated failure fetching files of PR ${number}" >&2
    return 1
  fi
  if [[ -n "${UPSTREAM_FIXTURE:-}" ]]; then
    jq -r --arg n "$number" '.files[$n][]?' "$UPSTREAM_FIXTURE"
  else
    api "repos/home-assistant/core/pulls/${number}/files?per_page=100" --paginate --jq '.[].filename'
  fi
}

matches() {
  case "$1" in
    homeassistant/auth/* | \
      homeassistant/components/http/* | \
      homeassistant/components/auth/* | \
      homeassistant/components/websocket_api/* | \
      homeassistant/components/persistent_notification/* | \
      homeassistant/components/config/* | \
      homeassistant/helpers/http.py | \
      homeassistant/config_entries.py) return 0 ;;
  esac
  return 1
}

lines=""
while IFS=$'\x1f' read -r number title url; do
  [[ -n "$number" ]] || continue
  # Capture first: a failure inside `< <(...)` would be swallowed by set -e.
  files="$(changed_files "$number")"
  hits=""
  while IFS= read -r file; do
    if matches "$file"; then
      hits+="${hits:+, }\`${file}\`"
    fi
  done <<<"$files"
  if [[ -n "$hits" ]]; then
    # Titles are untrusted: no backtick can end the code span, so no @mention,
    # #reference or markdown inside it is live.
    title="${title//\`/\'}"
    lines+="- [#${number}](${url}) \`${title}\`: ${hits}"$'\n'
  fi
done < <(jq -r --arg since "$since" \
  '.[] | select(.merged_at != null and .merged_at >= $since)
   | [.number, (.title | gsub("[\\r\\n\u001f]"; " ")), .html_url] | join("\u001f")' <<<"$pulls")

if [[ -z "$lines" ]]; then
  echo "No watched Home Assistant PRs merged since ${since}."
  exit 0
fi

body="$(printf 'Home Assistant Core PRs merged into `dev` since %s that touch code Electric Fence depends on:\n\n%s' "$since" "$lines")"

gh_() {
  if [[ "${DRY_RUN:-}" == "1" ]]; then
    printf 'DRY_RUN: gh'
    printf ' %q' "$@"
    printf '\n'
  else
    gh "$@"
  fi
}

if [[ "${DRY_RUN:-}" == "1" ]]; then
  gh_ issue list --label upstream-watch --state open --limit 1 --json number
  issue="${DRY_RUN_EXISTING_ISSUE:-}"
else
  issue="$(gh issue list --label upstream-watch --state open --limit 1 --json number --jq '.[0].number // empty')"
fi

if [[ -z "$issue" ]]; then
  gh_ label create upstream-watch --color 1D76DB \
    --description "Upstream Home Assistant changes to review" --force
  if [[ "${DRY_RUN:-}" == "1" ]]; then
    gh_ issue create --title "Upstream watch" --label upstream-watch \
      --body "Daily list of Home Assistant Core PRs that touch code Electric Fence depends on."
    issue="<new issue>"
  else
    url="$(gh issue create --title "Upstream watch" --label upstream-watch \
      --body "Daily list of Home Assistant Core PRs that touch code Electric Fence depends on.")"
    issue="${url##*/}"
    gh issue pin "$issue" || true
  fi
fi

gh_ issue comment "$issue" --body "$body"
