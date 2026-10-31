#!/usr/bin/env bash
# Daily watch on Home Assistant Core: list commits on `dev` since the last
# report that touch code Electric Fence depends on, and post one comment on the
# pinned issue labelled `upstream-watch` (created if missing). Posts nothing
# when no commit matches.
#
# Reads Core's git history (a blobless shallow clone), not the pulls API: Core
# merges 200+ PRs on busy days, and one page of the pulls API covered less than
# a day, so watched PRs were silently missed.
#
# Each comment ends with a hidden marker holding the last Core commit it
# covered. The next run starts after that commit, so runs neither overlap nor
# leave gaps. Without a marker (first run, or older comments) it starts at the
# last comment's time, else 24 hours ago, and never more than MAX_DAYS back.
#
# Watched paths are the Core code behind Electric Fence's runtime contracts
# (custom_components/electric_fence/contracts.py); see WATCHED below. Keep the
# two in step when a contract is added or removed.
#
# Output never links Core PRs (see the link comment below), so the watch adds
# nothing to Home Assistant's PR timelines.
#
# Needs git, jq and `gh` with GITHUB_TOKEN (GH_TOKEN) and `issues: write`.
# DRY_RUN=1 prints the gh write calls instead of making them.
# Test hooks:
#   UPSTREAM_CORE_DIR=<repo>       use this repository instead of cloning Core;
#                                  the watched branch is its HEAD
#   UPSTREAM_LAST_COMMENT=<file>   JSON {"body": ..., "created_at": ...} of the
#                                  issue's last comment, or `null`
#   UPSTREAM_NOW=<ISO-8601 UTC>    overrides "now"
set -euo pipefail

CORE_URL="https://github.com/home-assistant/core"
MAX_DAYS=30
MAX_PRS=150
MAX_FILES=8
MAX_BODY_BYTES=60000 # GitHub rejects bodies over 65,536 characters
MARKER_PREFIX="<!-- upstream-watch:through="

WATCHED=(
  # Login, auth providers, tokens and the login flow.
  homeassistant/auth/
  homeassistant/components/auth/
  homeassistant/data_entry_flow.py
  # Ban manager, request handling and the websocket API.
  homeassistant/components/http/
  homeassistant/helpers/http.py
  homeassistant/components/websocket_api/
  homeassistant/components/persistent_notification/
  # Settings changes: config entries, users and core config. Only the config
  # modules whose commands Electric Fence attributes, not all of `config`.
  homeassistant/config_entries.py
  homeassistant/core.py
  homeassistant/core_config.py
  homeassistant/const.py
  homeassistant/components/config/__init__.py
  homeassistant/components/config/auth.py
  homeassistant/components/config/auth_provider_homeassistant.py
  homeassistant/components/config/config_entries.py
  homeassistant/components/config/core.py
  # System settings areas.
  homeassistant/components/network/
  homeassistant/components/analytics/
  homeassistant/components/backup/__init__.py
  homeassistant/components/backup/const.py
  homeassistant/components/backup/websocket.py
  # Apps (Supervisor) and the Marketplace.
  homeassistant/components/hassio/__init__.py
  homeassistant/components/hassio/addon_manager.py
  homeassistant/components/hassio/http.py
  homeassistant/components/hassio/update.py
  homeassistant/components/hassio/websocket_api.py
  homeassistant/components/marketplace/
  # The panel.
  homeassistant/components/frontend/__init__.py
  homeassistant/components/panel_custom/
)

now="${UPSTREAM_NOW:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
floor="$(date -u -d "$now - ${MAX_DAYS} days" +%Y-%m-%dT%H:%M:%SZ)"

gh_() {
  if [[ "${DRY_RUN:-}" == "1" ]]; then
    printf 'DRY_RUN: gh'
    printf ' %q' "$@"
    printf '\n'
  else
    gh "$@"
  fi
}

# 1. The watch issue and its last comment.
if [[ "${DRY_RUN:-}" == "1" ]]; then
  issue="${DRY_RUN_EXISTING_ISSUE:-}"
else
  issue="$(gh issue list --label upstream-watch --state open --limit 1 --json number --jq '.[0].number // empty')"
fi

if [[ -n "${UPSTREAM_LAST_COMMENT:-}" ]]; then
  last="$(cat "$UPSTREAM_LAST_COMMENT")"
elif [[ -n "$issue" && "${DRY_RUN:-}" != "1" ]]; then
  # --paginate concatenates pages; slurp them and keep the newest comment.
  last="$(gh api "repos/{owner}/{repo}/issues/${issue}/comments?per_page=100" --paginate \
    | jq -s 'add // [] | last // null | if . then {body, created_at} else null end')"
else
  last="null"
fi

through="$(jq -r --arg p "$MARKER_PREFIX" \
  '(.body // "") | [scan("\($p)([0-9a-f]{40}) -->")] | last | .[0] // empty' <<<"$last")"
since="$(jq -r '.created_at // empty' <<<"$last")"
[[ -n "$since" ]] || since="$(date -u -d "$now - 24 hours" +%Y-%m-%dT%H:%M:%SZ)"
[[ "$since" > "$floor" ]] || since="$floor"

# 2. Core history back to a little before the floor, so the shallow boundary
# commit (which looks like it adds every file) is always outside the range.
if [[ -n "${UPSTREAM_CORE_DIR:-}" ]]; then
  core="$UPSTREAM_CORE_DIR"
else
  core="$(mktemp -d)"
  trap 'rm -rf "$core"' EXIT
  depth="$(date -u -d "$floor - 2 days" +%Y-%m-%dT%H:%M:%SZ)"
  git clone --quiet --filter=blob:none --no-checkout --single-branch --branch dev \
    --shallow-since="$depth" "$CORE_URL" "$core"
fi
head="$(git -C "$core" rev-parse HEAD)"

if [[ -n "$through" ]] && git -C "$core" merge-base --is-ancestor "$through" HEAD 2>/dev/null; then
  range=("${through}..HEAD")
  from="after ${through:0:11}"
else
  range=(--since="$since" HEAD)
  from="since ${since}"
fi

# 3. Matching commits, oldest first, with the watched files each one changed.
# Records: \x1e<sha>\x1f<subject>, then one file per line.
log="$(git -C "$core" log --first-parent --reverse --no-renames --name-only \
  --format=$'\x1e%H\x1f%s' "${range[@]}" -- "${WATCHED[@]}")"

lines=""
count=0
while IFS= read -r -d $'\x1e' record; do
  # A record without a newline has no files (a merge commit): skip it.
  [[ "$record" == *$'\n'* ]] || continue
  header="${record%%$'\n'*}"
  sha="${header%%$'\x1f'*}"
  subject="${header#*$'\x1f'}"
  files="$(grep -v '^$' <<<"${record#*$'\n'}" || true)"
  [[ -n "$files" ]] || continue
  count=$((count + 1))
  ((count <= MAX_PRS)) || continue

  hits=""
  n=0
  while IFS= read -r file; do
    n=$((n + 1))
    if ((n <= MAX_FILES)); then
      file="${file//\`/\'}"
      hits+="${hits:+, }\`${file}\`"
    fi
  done <<<"$files"
  ((n <= MAX_FILES)) || hits+=" and $((n - MAX_FILES)) more"

  # Link the commit, never the PR: this repository is public, and a PR link or
  # `owner/repo#n` reference in a comment adds a "mentioned this" event to the
  # Core PR's timeline. Commits have no timeline, and their page links the PR.
  # Squash merges end in "(#<number>)"; that number is shown in a code span,
  # where GitHub does not turn it into a reference.
  link="[\`${sha:0:11}\`](${CORE_URL}/commit/${sha})"
  if [[ "$subject" =~ ^(.*)\ \(#([0-9]+)\)$ ]]; then
    subject="${BASH_REMATCH[1]}"
    link+=" \`core#${BASH_REMATCH[2]}\`"
  fi
  # Subjects are untrusted: no backtick can end the code span, so no @mention,
  # #reference or markdown inside it is live.
  subject="${subject//\`/\'}"
  lines+="- ${link} \`${subject}\`: ${hits}"$'\n'
done < <(printf '%s\x1e' "$log")

if ((count == 0)); then
  echo "No watched Home Assistant changes on dev ${from}."
  exit 0
fi
((count <= MAX_PRS)) || lines+="- ...and $((count - MAX_PRS)) more"$'\n'

# 4. Post. The body goes through a file and is byte-capped (UTF-8 safe), with
# the marker always kept at the end.
body_file="$(mktemp)"
printf 'Home Assistant Core changes on `dev` %s that touch code Electric Fence depends on:\n\n%s' \
  "$from" "$lines" | python3 -I -c '
import sys
limit = int(sys.argv[1])
data = sys.stdin.buffer.read()
if len(data) > limit:
    note = "\n\n_(truncated: body too long)_".encode()
    data = data[: limit - len(note)].decode("utf-8", errors="ignore").encode() + note
sys.stdout.buffer.write(data)
' "$MAX_BODY_BYTES" >"$body_file"
printf '\n%s%s -->\n' "$MARKER_PREFIX" "$head" >>"$body_file"
[[ "${DRY_RUN:-}" != "1" ]] || cat "$body_file"

if [[ -z "$issue" ]]; then
  gh_ label create upstream-watch --color 1D76DB \
    --description "Upstream Home Assistant changes to review" --force
  if [[ "${DRY_RUN:-}" == "1" ]]; then
    gh_ issue create --title "Upstream watch" --label upstream-watch \
      --body "Daily list of Home Assistant Core changes that touch code Electric Fence depends on."
    issue="<new issue>"
  else
    url="$(gh issue create --title "Upstream watch" --label upstream-watch \
      --body "Daily list of Home Assistant Core changes that touch code Electric Fence depends on.")"
    issue="${url##*/}"
    gh issue pin "$issue" || true
  fi
fi

gh_ issue comment "$issue" --body-file "$body_file"
rm -f "$body_file"
