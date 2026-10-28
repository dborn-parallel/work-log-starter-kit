#!/usr/bin/env python3
"""Write a metadata snapshot of local and GitHub work for one time window.

The snapshot holds commit subjects, branch and worktree state, PR state, Claude Code session digests
and changed files in a scratch folder. It holds no diffs and no tool outputs. Every string goes
through `redact` before the snapshot is written. Settings come from `WORKLOG_*` environment
variables, which `run.sh` loads from `config.env`. `--scrub <file>` applies the same redaction to
another JSON file, such as a summary.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

HOME = Path.home()
HERE = Path(__file__).resolve().parent
REPOS_ROOT = Path(os.environ.get("WORKLOG_REPOS_ROOT", HOME / "src")).expanduser()
DEV_DIR = Path(os.environ["WORKLOG_DEV_DIR"]).expanduser() if os.environ.get("WORKLOG_DEV_DIR") else None
CLAUDE_PROJECTS = Path(os.environ.get("WORKLOG_CLAUDE_PROJECTS", HOME / ".claude/projects")).expanduser()
CLAUDE_PROJECT_GLOBS = os.environ.get("WORKLOG_CLAUDE_PROJECT_GLOBS", "*").split()
GITHUB_REPOS = os.environ.get("WORKLOG_GITHUB_REPOS", "").split()
MAIN_REF = os.environ.get("WORKLOG_MAIN_REF", "origin/main")
STATE_PATH = HERE / "state.json"
SNAPSHOT_DIR = HERE / "snapshots"
TASK_RESULTS_PATH = HERE / "task_results.json"

DEFAULT_WINDOW = dt.timedelta(hours=24)
MAX_WINDOW = dt.timedelta(days=7)
BRANCH_STALE_DAYS = 30
PROMPT_CHARS = 300
PROMPTS_PER_SESSION = 8
FILES_PER_ITEM = 15
THREAD_CHARS = 280
BODY_CHARS = 700
ACTIVE_GAP = dt.timedelta(minutes=20)
BOT_AUTHORS = ("github-actions",)
FAILED = ("FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE")
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "target", ".mypy_cache"}
NOISE_PREFIXES = (
    "<ide_",
    "<system-reminder",
    "<task-notification",
    "<command-",
    "<local-command",
    "Caveat:",
    "[Request interrupted",
)
SECRET_PATTERNS = [
    re.compile(p)
    for p in (
        r"gh[pousr]_[A-Za-z0-9]{20,}",
        r"github_pat_[A-Za-z0-9_]{20,}",
        r"sk-ant-[A-Za-z0-9_-]{10,}",
        r"(?:xox[abprs]|xapp)-[A-Za-z0-9-]{10,}",
        r"AKIA[0-9A-Z]{16}",
        r"AIza[0-9A-Za-z_-]{35}",
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
        r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@",
        r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/-]{16,}=*",
        r"(?i)(?:password|passwd|passphrase|secret|token|api[_-]?key)[\"']?(?:\s*[:=]|\s+is\b)\s*\S+",
        r"https://hooks\.slack\.com/services/\S+",
        r"\b(?:glpat-|npm_|ya29\.)[A-Za-z0-9._-]{16,}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
        r"\b(?![0-9a-f]{40}\b)[0-9a-f]{32,64}\b",
        r"(?<![A-Za-z0-9/+])(?=[A-Za-z0-9/+]*\d)(?=[A-Za-z0-9/+]*[A-Z])(?=[A-Za-z0-9/+]*[a-z])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])",
        r"\b(?=[A-Za-z0-9+]*\d)(?=[A-Za-z0-9+]*[A-Z])(?=[A-Za-z0-9+]*[a-z])[A-Za-z0-9+]{40,}={0,2}",
    )
]


def redact(text: str) -> str:
    """Replace substrings that look like credentials with `[redacted]`."""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def scrub(value):
    """Return `value` with `redact` applied to every string in it."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    return value


def parse_time(value: str) -> dt.datetime:
    """Parse an ISO 8601 time. A time without an offset is local time."""
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.astimezone()


def run(args: list[str], cwd: Path | None = None) -> str:
    """Run a command and return its stdout, or raise on a non-zero exit."""
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def window_from(state_path: Path, since: str | None, now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """Return the window from `since`, or from the last successful run, to `now`.

    A start from the last successful run is never earlier than `MAX_WINDOW` before `now`. A start
    that is not before `now` raises `ValueError`.
    """
    if since:
        start = parse_time(since)
    else:
        try:
            start = parse_time(json.loads(state_path.read_text())["last_success"])
        except (OSError, ValueError, KeyError, TypeError):
            start = now - DEFAULT_WINDOW
        start = max(start, now - MAX_WINDOW)
    if start >= now:
        raise ValueError(f"the window start {start.isoformat()} is not before its end {now.isoformat()}")
    return start, now


# Git


def git_repos(root: Path) -> list[Path]:
    """Return one checkout per repository under `root`."""
    repos = {}
    for child in sorted(root.iterdir()):
        if (child / ".git").is_dir():
            try:
                common = run(["git", "rev-parse", "--git-common-dir"], cwd=child).strip()
            except RuntimeError:
                continue
            repos.setdefault((child / common).resolve(), child)
    return list(repos.values())


def git_author(repo: Path) -> str:
    return run(["git", "config", "user.email"], cwd=repo).strip()


def has_ref(repo: Path, ref: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo, capture_output=True, check=False
    )
    return result.returncode == 0


def branch_commits(repo: Path, author: str, start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Return the non-merge commits by `author` authored and committed in the window, with the branches that hold them.

    A commit that a rebase or cherry-pick moved into the window keeps its earlier author date, so it
    is not returned again.
    """
    base = MAIN_REF if has_ref(repo, MAIN_REF) else None
    commits: dict[str, dict] = {}
    for branch in run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"], cwd=repo).split():
        args = ["git", "log", f"refs/heads/{branch}", "--no-merges", f"--author={author}"]
        args += [f"--since={start.isoformat()}", f"--until={end.isoformat()}"]
        args += ["--format=%H%x1f%aI%x1f%cI%x1f%s"]
        if base and branch not in ("main", "master"):
            args.append(f"^{base}")
        for line in run(args, cwd=repo).splitlines():
            sha, authored, committed, subject = line.split("\x1f", 3)
            if parse_time(authored) < start:
                continue
            entry = commits.setdefault(
                sha, {"sha": sha[:10], "authored": authored, "committed": committed, "subject": subject, "branches": []}
            )
            entry["branches"].append(branch)
    return sorted(commits.values(), key=lambda c: c["committed"])


def branch_states(repo: Path, now: dt.datetime) -> list[dict]:
    """Return the push state and `MAIN_REF` divergence of each branch with a commit in the last `BRANCH_STALE_DAYS`."""
    fmt = "%(refname:short)%1f%(upstream:short)%1f%(upstream:track)%1f%(committerdate:iso-strict)"
    states = []
    for line in run(["git", "for-each-ref", f"--format={fmt}", "refs/heads/"], cwd=repo).splitlines():
        name, upstream, track, committed = line.split("\x1f")
        if now - parse_time(committed) > dt.timedelta(days=BRANCH_STALE_DAYS):
            continue
        local = f"refs/heads/{name}"
        state = {"branch": name, "last_commit": committed, "upstream": upstream or None, "track": track or None}
        if has_ref(repo, MAIN_REF) and name not in ("main", "master"):
            behind, ahead = run(
                ["git", "rev-list", "--left-right", "--count", f"{MAIN_REF}...{local}"], cwd=repo
            ).split()
            state |= {"ahead_of_main": int(ahead), "behind_main": int(behind)}
        state["unpushed_commits"] = int(
            run(["git", "rev-list", "--count", local, "--not", "--remotes=origin"], cwd=repo)
        )
        states.append(state)
    return states


def worktree_states(repo: Path) -> list[dict]:
    """Return the branch and uncommitted changes of each worktree that exists on disk."""
    states = []
    for block in run(["git", "worktree", "list", "--porcelain"], cwd=repo).strip().split("\n\n"):
        fields = dict(line.split(" ", 1) if " " in line else (line, "") for line in block.splitlines())
        path = Path(fields["worktree"])
        if "bare" in fields or not path.is_dir():
            continue
        state = {"path": str(path), "branch": fields.get("branch", "").removeprefix("refs/heads/") or None}
        try:
            changed = [line[3:] for line in run(["git", "status", "--porcelain"], cwd=path).splitlines()]
            state |= {"uncommitted_files": len(changed), "uncommitted_sample": changed[:FILES_PER_ITEM]}
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            state["error"] = str(error)
        states.append(state)
    return states


def collect_git(start: dt.datetime, end: dt.datetime, errors: dict) -> list[dict]:
    """Return the git state of each repository. A repository that fails is recorded in `errors`."""
    repos = []
    for repo in git_repos(REPOS_ROOT):
        try:
            author = git_author(repo)
            repos.append(
                {
                    "repo": repo.name,
                    "commits": branch_commits(repo, author, start, end),
                    "branches": branch_states(repo, end),
                    "worktrees": worktree_states(repo),
                }
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            errors[f"git:{repo.name}"] = str(error)
    return repos


# GitHub

PR_FIELDS = """
number title url headRefName isDraft state reviewDecision mergeable createdAt updatedAt mergedAt bodyText
author { login }
reviewRequests(first: 20) { nodes { requestedReviewer { ... on User { login } ... on Team { name } } } }
reviewThreads(first: 100) {
  nodes {
    isResolved isOutdated path line
    comments(first: 1) { totalCount nodes { author { login } bodyText createdAt url } }
  }
}
commits(last: 1) {
  nodes {
    commit {
      statusCheckRollup {
        state
        contexts(first: 100) {
          nodes {
            __typename
            ... on CheckRun { name conclusion detailsUrl }
            ... on StatusContext { context state targetUrl }
          }
        }
      }
    }
  }
}
timelineItems(itemTypes: [REVIEW_REQUESTED_EVENT], last: 20) {
  nodes { ... on ReviewRequestedEvent { createdAt requestedReviewer { ... on User { login } } } }
}
"""


def search_prs(query: str) -> list[dict]:
    gql = f"query($q: String!) {{ search(query: $q, type: ISSUE, first: 50) {{ nodes {{ ... on PullRequest {{ {PR_FIELDS} }} }} }} }}"
    data = json.loads(run(["gh", "api", "graphql", "-f", f"query={gql}", "-F", f"q={query}"]))
    return [flatten_pr(node) for node in data["data"]["search"]["nodes"] if node]


def flatten_thread(thread: dict) -> dict:
    first = (thread["comments"]["nodes"] or [{}])[0]
    author = (first.get("author") or {}).get("login")
    return {
        "automated": author in BOT_AUTHORS or str(author).endswith("[bot]"),
        "resolved": thread["isResolved"],
        "outdated": thread["isOutdated"],
        "path": thread["path"],
        "line": thread["line"],
        "author": author,
        "created": first.get("createdAt"),
        "url": first.get("url"),
        "excerpt": " ".join((first.get("bodyText") or "").split())[:THREAD_CHARS],
        "replies": max(0, thread["comments"]["totalCount"] - 1),
    }


def failed_checks(rollup: dict | None) -> list[dict]:
    """Return the name and link of each check in `rollup` that failed."""
    checks = []
    for context in (rollup or {}).get("contexts", {}).get("nodes", []):
        if (context.get("conclusion") or context.get("state")) in FAILED:
            checks.append(
                {
                    "name": context.get("name") or context.get("context"),
                    "url": context.get("detailsUrl") or context.get("targetUrl"),
                }
            )
    return checks


def flatten_pr(node: dict) -> dict:
    rollup = node["commits"]["nodes"][0]["commit"]["statusCheckRollup"] if node["commits"]["nodes"] else None
    threads = [flatten_thread(t) for t in node["reviewThreads"]["nodes"]]
    return {
        "number": node["number"],
        "title": node["title"],
        "url": node["url"],
        "branch": node["headRefName"],
        "author": node["author"]["login"] if node["author"] else None,
        "draft": node["isDraft"],
        "state": node["state"],
        "review_decision": node["reviewDecision"],
        "mergeable": node["mergeable"],
        "ci": rollup["state"] if rollup else None,
        "body": " ".join((node.get("bodyText") or "").split())[:BODY_CHARS],
        "unresolved_threads": sum(not t["resolved"] and not t["automated"] for t in threads),
        "threads": threads,
        "failed_checks": failed_checks(rollup),
        "pending_reviewers": [
            r["requestedReviewer"].get("login") or r["requestedReviewer"].get("name")
            for r in node["reviewRequests"]["nodes"]
            if r["requestedReviewer"]
        ],
        "review_requests": [
            {"reviewer": (event.get("requestedReviewer") or {}).get("login"), "at": event["createdAt"]}
            for event in node["timelineItems"]["nodes"]
            if event
        ],
        "created": node["createdAt"],
        "updated": node["updatedAt"],
        "merged": node["mergedAt"],
    }


def github_time(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_github(start: dt.datetime, end: dt.datetime) -> dict:
    """Return the user's PRs and review requests as of `end`.

    The open PRs are the PRs that were open at `end`: open now, or merged after `end`, and created
    before it. Their CI, threads and review state are the current state.
    """
    since, until = github_time(start), github_time(end)
    repo = " ".join(f"repo:{r}" for r in GITHUB_REPOS) + " is:pr"
    mine = search_prs(f"{repo} is:open author:@me created:<={until}")
    mine += search_prs(f"{repo} author:@me merged:>{until} created:<={until}")
    return {
        "login": run(["gh", "api", "user", "--jq", ".login"]).strip(),
        "my_open_prs": mine,
        "my_merged_in_window": search_prs(f"{repo} author:@me merged:{since}..{until}"),
        "review_requested_from_me": search_prs(f"{repo} is:open user-review-requested:@me created:<={until}"),
        "reviewed_by_me_updated_in_window": search_prs(f"{repo} reviewed-by:@me -author:@me updated:{since}..{until}"),
    }


# Claude Code sessions


def blocks_of(record: dict, kind: str) -> list[dict]:
    """Return the content blocks of type `kind` in a transcript record."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == kind]


def prompt_text(record: dict) -> str | None:
    """Return the text that a person typed in a user record, without IDE and harness blocks."""
    content = (record.get("message") or {}).get("content")
    blocks = [content] if isinstance(content, str) else [b.get("text", "") for b in blocks_of(record, "text")]
    kept = [b.strip() for b in blocks if b.strip() and not b.lstrip().startswith(NOISE_PREFIXES)]
    if not kept:
        return None
    text = re.sub(r"</?pasted_content[^>]*>", "", "\n".join(kept))
    return " ".join(text.split())[:PROMPT_CHARS]


def active_spans(stamps: list[dt.datetime]) -> list[list[str]]:
    """Return the start and end of each run of `stamps` with no gap longer than `ACTIVE_GAP`."""
    spans: list[list[dt.datetime]] = []
    for stamp in sorted(stamps):
        if spans and stamp - spans[-1][1] <= ACTIVE_GAP:
            spans[-1][1] = stamp
        else:
            spans.append([stamp, stamp])
    return [[start.isoformat(), end.isoformat()] for start, end in spans]


def worktree_of(file_path: str, worktrees: dict[str, str | None]) -> str | None:
    """Return the path of the worktree in `worktrees` that holds `file_path`."""
    matches = [wt for wt in worktrees if file_path.startswith(wt.rstrip("/") + "/")]
    return max(matches, key=len, default=None)


def edit_branch(
    file_path: str, cwd: str | None, git_branch: str | None, worktrees: dict[str, str | None]
) -> str | None:
    """Return the branch that held `file_path` when a session edited it.

    A file in the session's start folder takes the branch recorded with the edit. Other files take
    the current branch of the live worktree that holds them.
    """
    if cwd and git_branch and file_path.startswith(cwd.rstrip("/") + "/"):
        return git_branch
    holder = worktree_of(file_path, worktrees)
    return worktrees[holder] if holder else None


def digest_session(path: Path, start: dt.datetime, end: dt.datetime, worktrees: dict[str, str | None]) -> dict | None:
    """Return a digest of the main-thread activity of one transcript in the window, or `None` if it has none.

    `checkout_branches` is the branch of the session's start folder. `edited_branches` are the
    branches that held the files the session edited, at the time of each edit.
    """
    title = None
    prompts: list[str] = []
    prompt_count = 0
    tools: Counter[str] = Counter()
    edited: dict[str, tuple[str | None, str | None]] = {}
    branches: set[str] = set()
    stamps: list[dt.datetime] = []
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            try:
                record = json.loads(line)
                stamp = parse_time(record["timestamp"]) if "timestamp" in record else None
            except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") == "ai-title":
                title = record.get("aiTitle") or title
                continue
            if record.get("isSidechain") or stamp is None or record.get("type") not in ("user", "assistant"):
                continue
            if not start <= stamp < end:
                continue
            stamps.append(stamp)
            branches.update(filter(None, [record.get("gitBranch")]))
            if record["type"] == "assistant":
                for block in blocks_of(record, "tool_use"):
                    tools[str(block.get("name"))] += 1
                    file_path = (block.get("input") or {}).get("file_path")
                    if block.get("name") in ("Edit", "Write", "NotebookEdit") and isinstance(file_path, str):
                        edited.setdefault(file_path, (record.get("cwd"), record.get("gitBranch")))
                continue
            if record.get("isMeta"):
                continue
            text = prompt_text(record)
            if text:
                prompt_count += 1
                if len(prompts) < PROMPTS_PER_SESSION:
                    prompts.append(text)
                else:
                    prompts[-1] = text
    if not stamps:
        return None
    edited_branches = {edit_branch(f, cwd, git_branch, worktrees) for f, (cwd, git_branch) in edited.items()}
    return {
        "session": path.stem,
        "project": path.parent.name,
        "title": title,
        "first": min(stamps).isoformat(),
        "last": max(stamps).isoformat(),
        "active_spans": active_spans(stamps),
        "checkout_branches": sorted(branches),
        "edited_branches": sorted(filter(None, edited_branches)),
        "prompt_count": prompt_count,
        "prompts": prompts,
        "tool_counts": dict(tools.most_common()),
        "edited_files": sorted(f.replace(str(HOME), "~", 1) for f in edited)[:FILES_PER_ITEM],
        "edited_file_count": len(edited),
    }


def collect_sessions(
    projects_dir: Path, start: dt.datetime, end: dt.datetime, worktrees: dict[str, str | None], errors: dict
) -> list[dict]:
    """Return the digest of each transcript with activity in the window. A transcript that fails is recorded in `errors`."""
    digests = []
    paths = {path for pattern in CLAUDE_PROJECT_GLOBS for path in projects_dir.glob(f"{pattern}/*.jsonl")}
    for path in sorted(paths):
        try:
            if dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC) < start:
                continue
            digest = digest_session(path, start, end, worktrees)
        except Exception as error:  # noqa: BLE001
            errors[f"sessions:{path.stem}"] = f"{type(error).__name__}: {error}"
            continue
        if digest:
            digests.append(digest)
    return sorted(digests, key=lambda d: d["first"])


# Scratch folder


def collect_dev(dev_dir: Path, start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Return each top-level folder of `dev_dir`, other than this tool's folder, with files modified in the window."""
    since, until = start.timestamp(), end.timestamp()
    folders = []
    for folder in sorted(p for p in dev_dir.iterdir() if p.is_dir() and p.resolve() != HERE):
        changed = []
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                full = Path(root, name)
                try:
                    mtime = full.stat().st_mtime
                except OSError:
                    continue
                if since <= mtime < until:
                    changed.append((mtime, str(full.relative_to(folder))))
        if changed:
            changed.sort(reverse=True)
            folders.append(
                {
                    "folder": folder.name,
                    "changed_files": len(changed),
                    "latest": dt.datetime.fromtimestamp(changed[0][0]).astimezone().isoformat(),
                    "sample": [name for _, name in changed[:FILES_PER_ITEM]],
                }
            )
    return folders


def guarded(errors: dict, name: str, fn, *args):
    """Return `fn(*args)`, or `None` with the error recorded under `name` if it raises."""
    try:
        return fn(*args)
    except Exception as error:  # noqa: BLE001
        errors[name] = f"{type(error).__name__}: {error}"
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="window start, ISO 8601; default is the last successful run")
    parser.add_argument("--until", help="window end, ISO 8601; default is now")
    parser.add_argument("--out", type=Path, help="snapshot path; default is snapshots/<run_id>.json")
    parser.add_argument("--scrub", type=Path, help="apply `redact` to every string of this JSON file, and stop")
    args = parser.parse_args()
    if args.scrub:
        args.scrub.write_text(json.dumps(scrub(json.loads(args.scrub.read_text())), indent=1))
        return 0

    generated = dt.datetime.now().astimezone().replace(microsecond=0)
    try:
        start, end = window_from(STATE_PATH, args.since, parse_time(args.until) if args.until else generated)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    errors: dict[str, str] = {}
    git = guarded(errors, "git", collect_git, start, end, errors)
    worktrees = {wt["path"]: wt["branch"] for repo in git or [] for wt in repo["worktrees"]}
    snapshot = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "generated": generated.isoformat(),
        "run_id": "run-" + end.strftime("%Y%m%d-%H%M%S"),
        "git": git,
        "github": guarded(errors, "github", collect_github, start, end) if GITHUB_REPOS else None,
        "sessions": guarded(errors, "sessions", collect_sessions, CLAUDE_PROJECTS, start, end, worktrees, errors),
        "dev_folders": guarded(errors, "dev_folders", collect_dev, DEV_DIR, start, end) if DEV_DIR else None,
        "task_results": json.loads(TASK_RESULTS_PATH.read_text())[-20:] if TASK_RESULTS_PATH.exists() else [],
        "errors": errors,
    }
    out = args.out or SNAPSHOT_DIR / f"{snapshot['run_id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(scrub(snapshot), indent=1))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
