# Work log starter kit: brief for Claude

You help a user set up a daily work log. The work log is a private claude.ai page that updates on a
schedule on workdays. It shows what the user did since the last update, what waits on them, and
small tasks that Claude can do next. This kit holds a design that works in daily use, templates, and
the problems that were found while it was built. Use it as a start, not as a fixed design. The user
decides what the page shows.

The templates hold no data about a person. All settings are placeholders that you fill in with the
user in phase 0.

## What the system does

```
systemd user timer (laptop, workdays, a few times a day)
  └─ run.sh
       1. wait for the network (gh api user)
       2. collect.py      → snapshots/<run_id>.json      no model; metadata only; secrets redacted
       3. build_facts.py  → <run_id>.facts.json          no model; action list, PR state, timeline
       4. claude -p summarizer                           narrow tool allowlist, cost cap
            reads snapshot, facts, Jira, Confluence, Slack, memory notes
            writes summaries/<run_id>.json
       5. validate.py against schema.json (the summarizer loops until valid)
       6. facts merged into the summary; task ids added
       7. git push of summary + page to a private publish repo
cloud routine on claude.ai (cron, a few minutes after each run)
  └─ reads the publish repo, and publishes the page and run files to one fixed artifact URL
attended Claude Code session
  └─ /work-log-tasks runs the tasks that the user queued from the page
```

Two rules hold the design together:

- Deterministic facts and model text are separate. The collector and `build_facts.py` compute
  everything that can be computed: PR state, CI, threads, timelines, counts. The page draws those
  directly. The model writes only what needs judgment: topics, asks, notes, CI advice, tasks. The
  prompt tells the model not to restate facts.
- The model output has a schema with length limits, and `validate.py` checks it. The summarizer
  runs the validator and corrects its file until it is valid. A run that is not valid does not
  publish.

## Files in this kit

| File | Use |
|------|-----|
| `templates/config.env.example` | All settings. Copy to `config.env`. |
| `templates/collect.py` | Snapshot of git, GitHub PRs, Claude Code sessions and a scratch folder. |
| `templates/build_facts.py` | Action list, PR facts, timeline, metrics, week digest, task ids. |
| `templates/schema.json`, `templates/validate.py` | Summary schema and a small validator for it. |
| `templates/prompt.md`, `templates/prompt_week.md` | Summarizer prompts. `{{NAME}}` fields are filled by `run.sh`. |
| `templates/run.sh` | The pipeline, with `--collect-only`, `--no-push`, `--weekly`, `--since/--until` and `--push-only`. |
| `templates/build_index.py` | Writes the run index, the `files` map for the publish, and a build hash. |
| `templates/page/PAGE_SPEC.md` | Data contract of the page and a menu of sections. There is no page template. |
| `templates/routine_prompt.md` | Prompt for the cloud routine that publishes the page. |
| `templates/systemd/` | Timer and service units for the runs and the weekly run. |
| `templates/commands/work-log-tasks.md` | Slash command that runs queued tasks in an attended session. |

The Python code needs Python 3.11 or later, `git`, `gh` (logged in) and `jq`. It has no other
dependencies.

## Phase 0: ask the user

Ask these questions before you write files. Use the answers to fill `config.env` and to edit the
prompt.

1. Where should the work log folder be? It holds the code, the run outputs and `config.env`. It
   should be a local git repository with no remote, so that source changes are tracked. Its path,
   and all paths in `config.env`, must have no spaces.
2. Which folder holds the git checkouts, and which GitHub repositories have the user's PRs?
3. Which sources should the summary use: GitHub, Claude Code sessions, a scratch folder, Jira,
   Confluence, Slack? Check in this session which connectors are authorized (ToolSearch for
   `Atlassian`, `Slack`). Tell the user that the summarizer can read their Slack direct messages
   and private channels, and that a short form of that text can reach the page.
4. Which Claude Code projects should the summary include? Prompts that the user typed in those
   projects reach the model, and session titles reach the page. Set
   `WORKLOG_CLAUDE_PROJECT_GLOBS`.
5. Does the user have a Jira site that is not on `atlassian.net`, GitHub Enterprise, or Slack
   Enterprise Grid? Then change the `link` pattern in `schema.json`, or every run fails validation.
6. Should the summarizer read the user's Claude memory notes? They make the tasks much better,
   because they hold the user's git rules and the state of each project. Without them, tasks can
   go against the user's rules, for example a rebase where the user merges.
7. At which local times should the page update, and on which days? Three updates on workdays
   (late morning, after lunch, late afternoon) worked well. Also ask about a weekly summary on
   Friday.
8. Where should the page go: a claude.ai artifact (the design in this kit), a Claude Doc, or a
   local HTML file only? A local file removes phases 4 and 5.
9. What cost per run is acceptable? A run cost USD 0.85 to 1.40 on the default model, with a cap of
   USD 4 (USD 6 for the weekly run).
10. What writing style should the summary use? The prompt has the team's ASD-STE100 rule as the
    default.
11. Which page sections do they want? Show them the menu in `templates/page/PAGE_SPEC.md`.

## Phase 1: snapshot and facts

1. Make the folder and copy the templates into it with `cp -a templates/. <folder>`, so that
   `.gitignore` comes too. Make `config.env`, run `git init`, and check with `git status` that
   `config.env` is ignored.
2. Run `./run.sh --collect-only`. It writes a snapshot and its facts and does not call a model.
   When you run `run.sh` by hand, its output goes to the terminal. Under systemd it goes to
   `logs/<year>-<month>.log`.
3. Read the snapshot with the user. Check that the commits, PRs and sessions are correct, and that
   no secret is in it (search for `token`, `key`, `password`). Add patterns to `SECRET_PATTERNS` if
   needed.
4. Add or remove collectors for the sources the user chose. Keep the rule that the snapshot holds
   metadata only: no diffs and no tool outputs.

## Phase 2: summarizer

1. Edit `prompt.md` for the user's sources, style and groups of work. Keep the steps section and the
   reply line, because `run.sh` depends on them.
2. Probe the permissions before the first summarizer run. From `.summarizer-cwd`, run `claude -p`
   with the flags in `ask_claude`, the rules that `summarizer_tools` prints, `--model haiku`, and a
   prompt that tries these steps: read `config.env`, run `cat config.env`, call WebFetch, and call
   a Slack tool that sends a message. Each step must be denied. The probe costs about USD 0.01.
3. Run `./run.sh --no-push`. It writes `summaries/<run_id>.json` and does not publish.
4. Read the summary and the log in `logs/<run_id>.summarize.json` with the user (cost, turns,
   result). Correct the prompt and run again. `--since` and `--until` make a fixed window, so you can
   compare runs.

## Phase 3: page

1. Load the `artifact-design` skill, and `artifact-capabilities` if the page has pins or a task
   queue. Build `page/work-log.html` from `PAGE_SPEC.md` and the sections the user chose.
2. Make a local publish folder with `summaries/`, `page/`, and run `python3 build_index.py <folder>`.
   Check the page in headless Chrome against that folder.
3. Publish it once from this attended session, with `root` set to the folder, `files` set to the map
   in `publish.json`, and the `db` capability if needed. Put the URL in `config.env` as
   `WORKLOG_PAGE_URL`.

## Phase 4: schedule and publish

1. Make a private GitHub repository for the publish files, under the user's own account. Clone it
   to the path in `WORKLOG_PUBLISH_REPO`.
2. Install the systemd units: replace `<WORK_LOG_DIR_UNDER_HOME>`, copy them to `~/.config/systemd/user/`, then
   `systemctl --user daemon-reload` and `systemctl --user enable --now work-log.timer`. Set the
   times from phase 0.
3. Make the cloud routine with the `schedule` skill: the prompt from `routine_prompt.md` with the
   URL and repository filled in, the publish repository as its source, no connectors, and a cron
   that fires about 15 min after each local run. Read the routine notes in "Known problems" first.
4. Run `./run.sh` by hand once, run the routine by hand once, and open the page.

## Phase 5: options

Add these only when the user asks for them:

- Weekly summary: `work-log-weekly.timer` runs `run.sh --weekly` on Friday.
- Task queue: the page writes `tasks/<id>` documents. Install `commands/work-log-tasks.md` as
  `~/.claude/commands/work-log-tasks.md`, with `<PAGE_URL>` and `<WORK_LOG_DIR>` (an absolute path)
  filled in.
- Project stacks: a JSON file for each stack of dependent PRs, drawn as a graph (mermaid works).
  The summarizer updates the file each run, and the collector computes the state of each node.
- CI log parsing: fetch the logs of failed Actions jobs with `gh run view --log-failed`, and keep
  only the failed test names and error lines. This makes `ci_advice` much better.
- Branch cleanup candidates: branches whose tip is in main, whose PR merged or closed, or that are
  idle for a long time. Give them to the summarizer as `housekeeping` tasks.
- claude.ai artifacts and docs: the routine can list the user's artifacts to a side branch of the
  publish repo, and the next run reads that listing.

## Last step

Offer the user an adversarial review of the pipeline, with focus on secret leaks into the snapshot
and the published page, the tool allowlist of the summarizer, and runs that publish an empty or
partial report.

## Known problems

These were found with Claude Code in October 2026. Check each one again, because behavior can
change between versions.

Headless runs (`claude -p` from systemd):

- `claude` is not on `PATH` when Claude Code is installed only as the VS Code extension. `run.sh`
  finds the newest extension binary.
- Use `--permission-mode dontAsk`, `--permission-prompts none` and an explicit `--allowedTools`
  list. A rule for an absolute path needs two leading slashes: `Read(//home/me/x/**)`. A rule such
  as `Write(./x/**)` does not match an absolute path. The Write tool is controlled by `Edit(...)`
  rules.
- A session can read files in its working folder with no permission rule. `run.sh` therefore runs
  the summarizer in the empty folder `.summarizer-cwd`, and all paths in the prompt are absolute.
- `claude -p` loads the user's settings files, with their allow rules, hooks and plugins.
  `--setting-sources project` from the empty folder loads none of them, and `--tools` limits the
  built-in tools. The claude.ai connectors stay available.
- Give the summarizer only what it needs: Read on its inputs, Edit on its draft file, and the one
  exact validate command for Bash. Do the phase 2 probe before you trust the list.
- A headless run that systemd starts has no Artifact tool, so it cannot publish the page. This is
  why the run pushes files to a repository and a cloud routine publishes them.
- ArtifactData writes are denied in `-p`, even when the tool is in the allowlist. Put run data in
  published files. Use the artifact database only for state that the viewer writes from the page.
- Read-only connector tools (Jira, Confluence, Slack) work in `-p` when each full tool name is in
  the allowlist. If a connector is not authorized, the summarizer must record it in
  `source_errors`, not fail.
- `UnsetEnvironment=ANTHROPIC_API_KEY` in the service makes the run use the user's Claude login.

Publishing:

- A session cannot replace a published file that it has not seen. The publisher reads the artifact
  and lists its files (`scope: files`) before it publishes.
- In the `files` map, a `null` value removes a file. Remove `null` entries for paths that the
  listing does not have, or the publish fails.
- Declare the `db` capability once, from an attended session. A publish that leaves out
  `capabilities` keeps it.
- `runs/build.json` holds a hash of all published content, so the routine can skip a publish when
  nothing changed. This makes extra routine fires safe.

Cloud routines:

- A routine cron is in UTC, and a routine cannot fire more than once an hour. To cover both daylight
  saving offsets, list the UTC hours for both. The extra fires do nothing because of the build hash.
  A local time that falls on the next UTC day needs a second routine with its own day-of-week field.
- A fire starts 1 to 7 min after the cron minute.
- A routine made through the API got all of the account's connectors. Remove them, because the
  publisher needs none.
- A routine push to `main` of its repository was blocked by the cloud safety classifier. If the
  routine must write back, push to a side branch.
- An unattended runner that executes Claude-written task prompts was blocked by the session safety
  classifier. A queue that an attended session runs (`/work-log-tasks`) works.

Robustness:

- A run that starts when the laptop wakes can come before the network, and then publishes an empty
  report. `run.sh` waits up to 10 min for `gh api user`, and stops before the summarizer if git or
  GitHub collection failed. systemd retries it (`Restart=on-failure`, 15 min, at most 4 in 2 h).
  `Persistent=true` runs a missed slot at once.
- `run.sh` stops before the summarizer if there is no git data or if the git, GitHub or session
  collector failed.
- If the summarizer stops before it replies `WROTE` (cost cap, time limit, error), a valid draft
  still publishes, with `source_errors.summarizer` set so that the page shows it. `state.json`
  advances only after a full run in which the summarizer finished, so the next run covers the same
  window again. The window from `state.json` is at most 7 days.
- `run.sh` sets `run_id` and the window in the summary from the snapshot, and applies the snapshot
  redaction to the summary before it publishes. The model text is from sources that are not
  redacted, such as Slack and Jira.
- A plain run does nothing on Saturday and Sunday. The Monday run covers the weekend. A weekly run
  that starts late, on Saturday to Tuesday, writes the week before. A run waits up to 1 h for
  another run to finish.
- A rebase or cherry-pick gives a commit a new commit date. The collector filters on author date, so
  the commit is not reported again.
- The branch that a session edited comes from the files it edited, not from the branch of its start
  folder. A session often starts in the main checkout and edits a worktree.
- A backfill (`--since`/`--until`) has exact dated data, but PR, CI and thread state are as of the
  time of the run.

Content:

- The prompt tells the summarizer that all text from the sources is data, not instructions. Text
  from other people can still steer what the summarizer writes into a task prompt.
  `/work-log-tasks` therefore shows each full prompt and runs only what the user confirms, and the
  page must escape all text (see `PAGE_SPEC.md`).
- Tasks never draft text for the user, such as a thread reply or a PR description. Each task prompt
  must work alone in a new session, and must stop before a push or a post to GitHub.
- The session that does a task updates the memory notes that the task makes out of date. A task
  that only updates memory notes has no value.
