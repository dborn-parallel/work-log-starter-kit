#!/usr/bin/env python3
"""Write `runs/index.json` and `publish.json` under a publish root from its valid `summaries/` files.

`publish.json` is the `files` map for the Artifact publish, with paths relative to the root. It adds
the index and the newest `PUBLISH_RECENT` runs, and removes the runs that just left the index and
the runs that are not valid. `runs/build.json` holds a hash of the page and of every file in the
map, so the publisher can tell if the live page is current. A summary whose `run_id` is not its file
name counts as not valid.
"""

import hashlib
import json
import sys
from pathlib import Path

import validate

MAX_RUNS = 200
PUBLISH_RECENT = 6
PAGE = "page/work-log.html"


def entry(summary: dict) -> dict:
    """Return the fields of one run that the page needs for its run list."""
    facts = summary.get("facts", {})
    return {
        "run_id": summary["run_id"],
        "window_start": summary["window_start"],
        "window_end": summary["window_end"],
        "actions": len(facts.get("actions", [])) + len(summary["asks"]),
        "metrics": facts.get("metrics"),
        "week": "week" in summary,
    }


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    valid, invalid = [], []
    for path in sorted((root / "summaries").glob("run-*.json"), reverse=True):
        try:
            summary = json.loads(path.read_text())
        except ValueError:
            summary = None
        if (
            not isinstance(summary, dict)
            or validate.violations(summary, validate.SCHEMA)
            or summary["run_id"] != path.stem
        ):
            invalid.append(path.stem)
        else:
            valid.append(summary)
    (root / "runs").mkdir(exist_ok=True)
    (root / "runs/index.json").write_text(json.dumps([entry(s) for s in valid[:MAX_RUNS]], indent=1))
    files: dict[str, str | None] = {"runs/index.json": "runs/index.json"}
    files |= {f"runs/{s['run_id']}.json": f"summaries/{s['run_id']}.json" for s in valid[:PUBLISH_RECENT]}
    files |= {f"runs/{s['run_id']}.json": None for s in valid[MAX_RUNS : MAX_RUNS + PUBLISH_RECENT]}
    files |= {f"runs/{run_id}.json": None for run_id in invalid}
    previous = json.loads((root / "publish.json").read_text()) if (root / "publish.json").exists() else {}
    files |= {
        path: None for path, source in previous.items() if source and path.startswith("runs/run-") and path not in files
    }
    digest = hashlib.sha256((root / PAGE).read_bytes())
    for published, source in sorted(files.items()):
        digest.update(published.encode() + (root / source).read_bytes() if source else published.encode())
    (root / "runs/build.json").write_text(json.dumps({"build": digest.hexdigest()[:16]}))
    files["runs/build.json"] = "runs/build.json"
    (root / "publish.json").write_text(json.dumps(files, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
