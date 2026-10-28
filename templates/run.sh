#!/usr/bin/env bash
# Collects a snapshot, has Claude write the summary of the window, and pushes it to the publish repo,
# from which a cloud routine publishes the work log page.
#
#   run.sh                         one run from the last successful run to now
#   run.sh --since <iso> --until <iso>   one run of a set window, for a backfill
#   run.sh --weekly                also writes the summary of the week that the run is in
#   run.sh --collect-only          writes the snapshot and the facts, and stops
#   run.sh --no-push               writes and validates the summary, and does not push
#   run.sh --push-only <run_id>    pushes an existing summary again
#
# A plain run does nothing on Saturday and Sunday. If WORKLOG_LOG_TO_FILE is set, as the systemd
# units do, the output goes to logs/<year>-<month>.log; otherwise it goes to the terminal.
set -Eeuo pipefail

cd "$(dirname "$(readlink -f "$0")")"
set -a
# shellcheck source=/dev/null
source ./config.env
set +a
if [[ -n "${WORKLOG_PATH:-}" ]]; then
  PATH="$WORKLOG_PATH:$PATH"
fi

readonly ATLASSIAN="mcp__claude_ai_Atlassian_MCP"
readonly SLACK="mcp__claude_ai_Slack__slack"
# Read-only connector tools. Check the exact names with ToolSearch in an attended session, and
# remove the ones you do not use.
readonly CONNECTOR_TOOLS=(
  "${ATLASSIAN}__atlassianUserInfo" "${ATLASSIAN}__getAccessibleAtlassianResources"
  "${ATLASSIAN}__searchJiraIssuesUsingJql" "${ATLASSIAN}__getJiraIssue" "${ATLASSIAN}__search"
  "${ATLASSIAN}__searchConfluence" "${ATLASSIAN}__getConfluenceContent"
  "${SLACK}_search_public_and_private" "${SLACK}_read_thread" "${SLACK}_read_channel"
  "${SLACK}_read_user_profile" "${SLACK}_search_users"
)
# The summarizer runs in this empty folder, because a session can read its working folder with no
# permission rule.
readonly SUMMARIZER_CWD="$PWD/.summarizer-cwd"

on_failure() {
  echo "== $(date -Iseconds) failed at line $1"
  if command -v notify-send >/dev/null; then
    notify-send "Work log run failed" "See $PWD/logs/$(date +%Y-%m).log" || true
  fi
}
trap 'on_failure $LINENO' ERR

# Prints the claude binary. A VS Code-only install has no `claude` on PATH.
claude_bin() {
  command -v claude && return
  {
    find "$HOME"/.vscode/extensions "$HOME"/.vscode-server/extensions "$HOME"/.vscode-insiders/extensions \
      "$HOME"/.cursor/extensions -path '*anthropic.claude-code-*/resources/native-binary/claude' 2>/dev/null || true
  } | sort -V | tail -n 1
}

# Fills the {{NAME}} fields of a prompt file from NAME=value arguments.
fill() {
  local file=$1 text
  shift
  text=$(<"$file")
  for pair in "$@"; do
    text=${text//"{{${pair%%=*}}}"/"${pair#*=}"}
  done
  printf '%s' "$text"
}

# Runs headless Claude with a prompt, writes the JSON result to a file, and fails unless the reply
# starts with the expected word. Only the built-in tools in --tools exist, and settings files other
# than those of the empty working folder are not loaded.
ask_claude() {
  local result=$1 expect=$2 prompt=$3
  shift 3
  mkdir -p "$SUMMARIZER_CWD"
  (cd "$SUMMARIZER_CWD" && timeout 25m "$claude" -p "$prompt" --permission-mode dontAsk --permission-prompts none \
    --setting-sources project --tools "Read,Edit,Write,Bash,Grep,Glob,ToolSearch" --no-session-persistence \
    --output-format json "$@" </dev/null >"$result")
  jq -r '"cost $" + (.total_cost_usd | tostring) + ", turns " + (.num_turns | tostring) + ": " + .result' "$result"
  jq -e --arg expect "$expect" '.is_error == false and (.result | startswith($expect))' "$result" >/dev/null
}

# Prints the --allowedTools rules of the summarizer. An absolute path needs a second leading slash.
summarizer_tools() {
  local snapshot=$1 facts=$2 draft=$3
  printf '%s\n' "Read(/$snapshot)" "Read(/$facts)" "Read(/$PWD/schema.json)" "Read(/$draft)" "Edit(/$draft)" \
    "Bash(python3 $PWD/validate.py $draft)"
  if [[ -n "$WORKLOG_SOURCE_DIR" ]]; then
    printf '%s\n' "Read(/$WORKLOG_SOURCE_DIR/**)" "Grep(/$WORKLOG_SOURCE_DIR/**)" "Glob(/$WORKLOG_SOURCE_DIR/**)"
  fi
  if [[ -n "$WORKLOG_MEMORY_DIR" ]]; then
    printf '%s\n' "Read(/$WORKLOG_MEMORY_DIR/**)"
  fi
  printf '%s\n' "${CONNECTOR_TOOLS[@]}"
}

# Prints the --disallowedTools rules of the summarizer: files in the source checkout that can hold secrets.
summarizer_denials() {
  if [[ -n "$WORKLOG_SOURCE_DIR" ]]; then
    printf '%s\n' "Read(/$WORKLOG_SOURCE_DIR/**/.env*)" "Read(/$WORKLOG_SOURCE_DIR/.git/**)"
  fi
}

# Writes the summary of a snapshot to `out`. The summarizer writes a draft, and the draft becomes
# `out` only when it is valid. Sets `complete` to false if the summarizer did not reply WROTE.
summarize() {
  local snapshot=$1 run_id=$2 out=$3 facts=${1%.json}.facts.json week=${1%.json}.week.json
  local draft="$PWD/drafts/$run_id.json" prompt budget=$WORKLOG_BUDGET_USD tools denials extra=() partial=0
  mkdir -p drafts
  rm -f "$draft"
  mapfile -t tools < <(summarizer_tools "$snapshot" "$facts" "$draft")
  mapfile -t denials < <(summarizer_denials)
  if ((${#denials[@]})); then
    extra=(--disallowedTools "${denials[@]}")
  fi
  prompt=$(fill prompt.md "RUN_ID=$run_id" "SNAPSHOT=$snapshot" "FACTS=$facts" "OUT=$draft" \
    "VALIDATE=$PWD/validate.py" "SCHEMA=$PWD/schema.json" "LINES=$(wc -l <"$snapshot")" "OWNER=$WORKLOG_OWNER_NAME" \
    "SOURCE=${WORKLOG_SOURCE_DIR:-none}" "MEMORY=${WORKLOG_MEMORY_DIR:-none}" \
    "ATLASSIAN_SITE=${WORKLOG_ATLASSIAN_SITE:-none}")
  if [[ -n "$week_start" ]]; then
    python3 build_facts.py --week "$week_start" summaries "$week"
    prompt+=$(fill prompt_week.md "WEEK_DIGEST=$week" "WEEK_START=$week_start" "OWNER=$WORKLOG_OWNER_NAME")
    tools+=("Read(/$week)")
    budget=$WORKLOG_WEEK_BUDGET_USD
  fi
  if ! ask_claude "$PWD/logs/$run_id.summarize.json" "WROTE $run_id" "$prompt" --max-budget-usd "$budget" \
    --allowedTools "${tools[@]}" "${extra[@]}"; then
    echo "the summarizer did not reply WROTE; the summary is marked as partial"
    partial=1
    complete=false
  fi
  jq --slurpfile snap "$snapshot" --arg partial "$partial" \
    '.run_id = $snap[0].run_id | .window_start = $snap[0].window.start | .window_end = $snap[0].window.end
     | if $partial == "1" then .source_errors.summarizer = "The summarizer stopped before it finished. This run can be incomplete." else . end' \
    "$draft" >"$draft.tmp"
  mv "$draft.tmp" "$draft"
  python3 collect.py --scrub "$draft"
  python3 validate.py "$draft"
  if [[ -n "$week_start" ]] && ! jq -e '.week' "$draft" >/dev/null; then
    echo "the weekly run has no week summary"
    false
  fi
  jq --slurpfile facts "$facts" '. + {facts: $facts[0]}' "$draft" >"$draft.tmp"
  mv "$draft.tmp" "$draft"
  python3 build_facts.py --task-ids "$draft"
  mv "$draft" "$out"
}

push_run() {
  local run_id=$1 repo=$WORKLOG_PUBLISH_REPO
  git -C "$repo" pull --quiet --ff-only || true
  mkdir -p "$repo/summaries" "$repo/page"
  cp "summaries/$run_id.json" "$repo/summaries/"
  cp page/work-log.html "$repo/page/"
  python3 build_index.py "$repo"
  git -C "$repo" add --all
  git -C "$repo" diff --cached --quiet || git -C "$repo" commit --quiet -m "Add $run_id"
  git -C "$repo" push --quiet origin HEAD:main
}

# Waits up to 10 min for GitHub to answer, because a run that starts at wake can come before the network.
wait_for_network() {
  local deadline=$((SECONDS + 600)) attempt=0
  while ((SECONDS < deadline)); do
    timeout 15 gh api user --jq .login >/dev/null 2>&1 && return 0
    attempt=$((attempt + 1))
    echo "network not ready (attempt $attempt)"
    sleep 20
  done
  return 1
}

# Prints the Monday 00:00 that starts the week of a weekly run. A run on Saturday to Tuesday is a
# late run for the week before.
week_start_of_run() {
  local day
  day=$(date -d '-2 days' +%F)
  date -d "$day -$(($(date -d "$day" +%u) - 1)) days" -Iseconds
}

mkdir -p logs summaries
if [[ -n "${WORKLOG_LOG_TO_FILE:-}" ]]; then
  exec >>"logs/$(date +%Y-%m).log" 2>&1
fi
exec 9>run.lock
flock -w 3600 9 || {
  echo "another run is still active after 1 h"
  false
}
echo "== $(date -Iseconds) start $*"

for tool in git gh jq python3 flock timeout; do
  command -v "$tool" >/dev/null || {
    echo "$tool is not on PATH ($PATH); set WORKLOG_PATH in config.env"
    false
  }
done

if [[ "${1:-}" == "--push-only" ]]; then
  push_run "$2"
  echo "== $(date -Iseconds) pushed $2"
  exit 0
fi

window=()
week_start=""
mode=full
while [[ $# -ge 1 ]]; do
  case $1 in
    --since | --until)
      window+=("$1" "$2")
      shift 2
      ;;
    --weekly)
      week_start=$(week_start_of_run)
      shift
      ;;
    --collect-only)
      mode=collect
      shift
      ;;
    --no-push)
      mode=no-push
      shift
      ;;
    *)
      echo "unknown argument $1"
      false
      ;;
  esac
done
if [[ $mode == full && ${#window[@]} -eq 0 && -z "$week_start" && $(date +%u) -gt 5 ]]; then
  echo "no run on Saturday or Sunday; the Monday run covers the weekend"
  exit 0
fi

wait_for_network || {
  echo "GitHub did not answer; stopping so that a retry can make a full report"
  false
}
for repo in "$WORKLOG_REPOS_ROOT"/*/; do
  if [[ -d "$repo/.git" ]]; then
    git -C "$repo" fetch --quiet origin || echo "fetch failed in $repo"
  fi
done
snapshot=$(python3 collect.py "${window[@]}")
if jq -e '(.errors | has("git") or has("github") or has("sessions")) or ((.git // []) | length == 0)' \
  "$snapshot" >/dev/null; then
  echo "the snapshot has no git, GitHub or session data: $(jq -c .errors "$snapshot")"
  false
fi
run_id=$(jq -r .run_id "$snapshot")
window_end=$(jq -r .window.end "$snapshot")
python3 build_facts.py "$snapshot" "${snapshot%.json}.facts.json"
if [[ $mode == collect ]]; then
  echo "== $(date -Iseconds) wrote $snapshot and ${snapshot%.json}.facts.json"
  exit 0
fi

claude=$(claude_bin)
[[ -x "$claude" ]] || {
  echo "no claude binary found"
  false
}
complete=true
summarize "$snapshot" "$run_id" "$PWD/summaries/$run_id.json"
if [[ $mode == full ]]; then
  push_run "$run_id"
fi
if [[ $mode == full && $complete == true && ${#window[@]} -eq 0 ]]; then
  jq -n --arg end "$window_end" '{last_success: $end}' >state.json.tmp
  mv state.json.tmp state.json
fi
echo "== $(date -Iseconds) finished $run_id ($mode, complete: $complete)"
