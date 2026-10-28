# Work log starter kit

This kit helps Claude Code set up a daily work log for you: a private claude.ai page that updates a
few times on each workday. It shows your commits, Claude sessions, PRs, review requests, Jira issues
and Slack asks since the last update, and small tasks that Claude can do next.

## How to start

1. Clone this repository to your laptop.
2. Open Claude Code in this folder.
3. Send this prompt:

   > Read START_HERE.md and help me set up my work log. Start with phase 0.

Claude asks you some questions, then builds the system in phases. You check the result of each
phase before the next one starts.

## What you need

- Linux with systemd (the timers are systemd user units).
- Python 3.11 or later, `git`, `jq`, and the GitHub CLI `gh`, logged in.
- Claude Code, logged in.
- For Jira, Confluence or Slack data: those connectors authorized in your claude.ai settings.
- For the cloud publisher: a private GitHub repository under your account, and access to Claude
  Code routines on claude.ai.

## Cost

One summary run cost USD 0.85 to 1.40 on the default model, with a cap of USD 4 for each run.
Three runs on each workday cost about USD 13 to 21 each week.
