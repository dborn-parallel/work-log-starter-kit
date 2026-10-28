#!/usr/bin/env python3
"""Write the facts of a snapshot: the action list, PR states, timeline events and metrics.

The page draws its standard figures from these facts. No model writes them.

Usage:
  build_facts.py <snapshot> <facts out>
  build_facts.py --task-ids <summary>
  build_facts.py --week <week start> <summaries dir> <digest out>
"""

import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

import validate
from collect import parse_time

PUSH_LOOKBACK = dt.timedelta(hours=24)
STALE_AFTER = dt.timedelta(days=14)
FAILING_CI = ("FAILURE", "ERROR")
SKIPPED_TITLE = re.compile(r"do\s*not\s*merge|\bDNM\b", re.IGNORECASE)


def shown(pr: dict) -> bool:
    """Return whether a PR belongs in the summary. A title that says not to merge it removes it."""
    return not SKIPPED_TITLE.search(pr["title"])


def stale(pr: dict, end: dt.datetime) -> bool:
    """Return whether a PR had no activity in the `STALE_AFTER` before `end`."""
    return end - parse_time(pr["updated"]) > STALE_AFTER


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def review_requested_at(pr: dict, login: str) -> str | None:
    """Return the time of the newest review request to `login`, or `None` if the PR has none."""
    times = [r["at"] for r in pr["review_requests"] if r["reviewer"] == login]
    return max(times, default=None)


def review_actions(queue: list[dict], login: str, end: dt.datetime) -> list[dict]:
    rows = [
        {
            "action": "review",
            "text": f"#{pr['number']} {pr['title']}{' (draft)' if pr['draft'] else ''}, from {pr['author']}",
            "link": pr["url"],
            "since": review_requested_at(pr, login) or pr["created"],
            "stale": stale(pr, end),
        }
        for pr in queue
    ]
    return sorted(rows, key=lambda r: r["since"])


def fix_actions(prs: list[dict], end: dt.datetime) -> list[dict]:
    rows = []
    for pr in prs:
        problems = []
        if pr["ci"] in FAILING_CI:
            problems.append("CI fails")
        if pr["mergeable"] == "CONFLICTING":
            problems.append("merge conflict")
        if problems and not pr["draft"]:
            rows.append(
                {
                    "action": "fix",
                    "text": f"#{pr['number']} {pr['title']}: {', '.join(problems)}",
                    "link": pr["url"],
                    "since": pr["updated"],
                    "stale": stale(pr, end),
                }
            )
    return rows


def reply_actions(prs: list[dict], end: dt.datetime) -> list[dict]:
    return [
        {
            "action": "reply",
            "text": f"#{pr['number']} {pr['title']}: {plural(pr['unresolved_threads'], 'open thread')}",
            "link": pr["url"],
            "since": pr["updated"],
            "stale": stale(pr, end),
        }
        for pr in prs
        if pr["unresolved_threads"] and not pr["draft"]
    ]


def push_actions(git: list[dict], start: dt.datetime) -> list[dict]:
    """Return a push action for each branch with unpushed commits and a commit in the window or in `PUSH_LOOKBACK` before it."""
    recent = start - PUSH_LOOKBACK
    return [
        {
            "action": "push",
            "text": f"{repo['repo']}:{b['branch']}: {plural(b['unpushed_commits'], 'commit')} not on GitHub",
            "since": b["last_commit"],
        }
        for repo in git
        for b in repo["branches"]
        if b["unpushed_commits"] and parse_time(b["last_commit"]) >= recent
    ]


def commit_actions(git: list[dict]) -> list[dict]:
    return [
        {
            "action": "commit",
            "text": f"{Path(w['path']).name} ({w['branch'] or 'detached'}): {plural(w['uncommitted_files'], 'changed file')}",
        }
        for repo in git
        for w in repo["worktrees"]
        if w.get("uncommitted_files")
    ]


def timeline(snapshot: dict) -> dict:
    """Return the dated events that a timeline figure draws."""
    commits = [
        {"t": c["committed"], "sha": c["sha"], "subject": c["subject"], "branches": c["branches"]}
        for repo in snapshot["git"] or []
        for c in repo["commits"]
    ]
    sessions = [
        {
            "id": s["session"],
            "start": s["first"],
            "end": s["last"],
            "spans": s["active_spans"],
            "title": s["title"],
            "branches": s["edited_branches"],
        }
        for s in snapshot["sessions"] or []
    ]
    merges = [
        {"t": pr["merged"], "number": pr["number"], "title": pr["title"], "url": pr["url"], "branch": pr["branch"]}
        for pr in (snapshot["github"] or {}).get("my_merged_in_window", [])
    ]
    return {"commits": sorted(commits, key=lambda c: c["t"]), "sessions": sessions, "merges": merges}


def my_pr(pr: dict, end: dt.datetime) -> dict:
    keep = (
        "number",
        "title",
        "url",
        "branch",
        "body",
        "draft",
        "review_decision",
        "mergeable",
        "ci",
        "created",
        "updated",
    )
    return {key: pr[key] for key in keep} | {
        "unresolved_threads": pr["unresolved_threads"],
        "threads": [t for t in pr["threads"] if not t["resolved"]],
        "failed_checks": pr["failed_checks"],
        "pending_reviewers": pr["pending_reviewers"],
        "stale": stale(pr, end),
    }


def facts(snapshot: dict) -> dict:
    github = snapshot["github"] or {}
    login = github.get("login", "")
    prs = [pr for pr in github.get("my_open_prs", []) if shown(pr)]
    queue = [pr for pr in github.get("review_requested_from_me", []) if shown(pr)]
    git = snapshot["git"] or []
    start = parse_time(snapshot["window"]["start"])
    end = parse_time(snapshot["window"]["end"])
    return {
        "generated": snapshot.get("generated"),
        "task_results": snapshot.get("task_results") or [],
        "actions": review_actions(queue, login, end)
        + fix_actions(prs, end)
        + reply_actions(prs, end)
        + push_actions(git, start)
        + commit_actions(git),
        "my_prs": [my_pr(pr, end) for pr in prs],
        "review_queue": [
            {
                "number": pr["number"],
                "title": pr["title"],
                "url": pr["url"],
                "author": pr["author"],
                "draft": pr["draft"],
                "requested_at": review_requested_at(pr, login) or pr["created"],
                "stale": stale(pr, end),
            }
            for pr in queue
        ],
        "timeline": timeline(snapshot),
        "metrics": {
            "open_prs": len(prs),
            "review_requests": len(queue),
            "ci_failing": sum(pr["ci"] in FAILING_CI for pr in prs),
            "conflicts": sum(pr["mergeable"] == "CONFLICTING" for pr in prs),
            "open_threads": sum(pr["unresolved_threads"] for pr in prs),
        },
    }


def add_task_ids(summary_path: Path) -> None:
    """Give each task of a summary an id made from its prompt, so that the page can queue it."""
    summary = json.loads(summary_path.read_text())
    for task in summary.get("tasks", []):
        task["id"] = hashlib.sha256(task["prompt"].encode()).hexdigest()[:16]
    summary_path.write_text(json.dumps(summary, indent=1))


def week_digest(start: dt.datetime, summaries_dir: Path) -> dict:
    """Return the topics, notes and asks of each earlier run that ended after `start`, and the PRs merged in those runs.

    A summary file that is not valid JSON or does not match the schema is skipped.
    """
    runs, merged = [], {}
    for path in sorted(summaries_dir.glob("run-*.json")):
        try:
            summary = json.loads(path.read_text())
        except ValueError:
            continue
        if validate.violations(summary, validate.SCHEMA) or parse_time(summary["window_end"]) <= start:
            continue
        run = {key: summary[key] for key in ("run_id", "window_start", "window_end")}
        runs.append(run | {key: summary.get(key, []) for key in ("topics", "notes", "asks")})
        for merge in summary.get("facts", {}).get("timeline", {}).get("merges", []):
            merged[merge["number"]] = merge
    return {"start": start.isoformat(), "runs": runs, "merged": sorted(merged.values(), key=lambda m: m["t"])}


def main() -> int:
    if sys.argv[1] == "--task-ids":
        add_task_ids(Path(sys.argv[2]))
        return 0
    if sys.argv[1] == "--week":
        digest = week_digest(parse_time(sys.argv[2]), Path(sys.argv[3]))
        Path(sys.argv[4]).write_text(json.dumps(digest, indent=1))
        return 0
    snapshot = json.loads(Path(sys.argv[1]).read_text())
    Path(sys.argv[2]).write_text(json.dumps(facts(snapshot), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
