# Work log page: data contract and section menu

The page is one HTML file, `page/work-log.html`, published as a claude.ai Artifact. It has no data
in its source. It fetches published files next to itself at load time:

| Published path        | Content                                                               |
|-----------------------|-----------------------------------------------------------------------|
| `runs/index.json`     | Array of run entries, newest first: `run_id`, `window_start`, `window_end`, `actions`, `metrics`, `week` |
| `runs/<run_id>.json`  | One summary, as in `schema.json`, plus `facts` from `build_facts.py` |
| `runs/build.json`     | `{"build": "<hash>"}`. Only the publisher uses it                      |

`build_index.py` publishes only the newest 6 runs. The page must show a clear note, not an error,
when a run in the index has no published file.

## Run picker

A picker selects a day. The page fetches every run of that day and merges them:

- State that changes over time comes from the last run of the day: open PRs, review queue, Jira
  issues, tasks, action list, metrics.
- Events are joined across the runs of the day, with duplicates removed: topics (keep each run's
  summary, newest first), asks, notes, figures, merges, commits, sessions.
- A run with a `week` field adds a week section at the top.

## Sections to choose from

Ask the owner which sections they want and in which order. Start small. The list below is the set
that proved useful; each one names its data.

1. Actions: `facts.actions` plus `asks`. One row per action with its age, newest last. Stale rows
   (`stale: true`) muted.
2. Timeline of the day: lanes for topics (`topics[].branches`, `sessions`, `prs` assign events to
   lanes), then for each other session title, then for each other branch. Session active spans as
   bars, commits as ticks, merges as markers. Topic summary and items as hover text on the lane
   label.
3. Open PRs: `facts.my_prs`. PR number, title (hover shows the body), age, CI state, open threads,
   pending reviewers, failed checks with `ci_advice` cause and fix. Drafts in their own fold.
4. Review queue: `facts.review_queue`, oldest request first.
5. Jira issues: `jira`, grouped by `category`, with `change` when present.
6. Easy tasks for Claude: `tasks` by group. Each task shows `title`, `why`, `needs_ok`, its links,
   a Copy prompt button, and a Queue checkbox (see below).
7. One-off figures: `figures` (`bars`, `progress`, `tree`).
8. Source errors: `source_errors`. This section is not optional. Show it near the top when it is not
   empty, because `summarizer` in it means that the run can be incomplete, and other keys mean that a
   source or a connector failed.

Some preferences from earlier use, to offer as a start: one column of text, dense rows, no hero
numbers or decorative tiles, items older than 14 days muted and moved below, PR titles with "DO NOT
MERGE" or "DNM" left out.

## Text from other people

PR titles and bodies, thread excerpts, Jira text and Slack asks are written by other people. Put all
summary and facts text into the page with `textContent` or an equivalent that escapes it. Never use
`innerHTML` or a template that does not escape. Text that runs as HTML on this page can write to its
database, and so it can add tasks to the queue. Use only `https://` links from the data.

## Viewer state

Data that the viewer writes from the page goes in the artifact database (the `db` capability), not
in published files. Load the `artifact-capabilities` skill before you write this part.

- Pins: a checkbox on a row writes `flags/<id>`. Pinned rows show first. Use stable ids, such as
  `pr-<number>`, `jira-<key>`, `branch-<name>`.
- Task queue: the Queue checkbox writes `tasks/<id>` with `{id, title, prompt, run_id}`. The
  `/work-log-tasks` command reads and deletes these documents. It rejects a document whose task is
  not in a run of the local `summaries/` folder, because any person who can write to the database
  can add a document.
- Set the `db` capability so that only the owner can write. The `artifact-capabilities` skill tells
  how.

Declare the `db` capability once, from an attended session. Later publishes from the cloud routine
leave out `capabilities`, so the page keeps it.

## Checks

- Render a local copy of the publish repo in headless Chrome (`google-chrome --headless
  --screenshot`) before the first publish, in light and dark mode, and at phone width.
- Load the page with an empty `runs/index.json` and with a run that has every field empty.
