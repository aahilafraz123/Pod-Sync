# Pod-Sync

> Team coordination for AI-native dev pods.
> Status logs, OpenSpec proposals, presence — all in one place.
> One command to install. Works from any repo you're in.

## Install

```bash
git clone git@github.com:aahilafraz123/Pod-Sync.git ~/tools/pod-sync
cd ~/tools/pod-sync
./install.sh
```

A browser window opens. Pick your IDE. Authenticate once. Done.

## Usage

Pod-Sync works through skills — structured instruction files that teach your IDE agent the full protocol. After install, run this once from any project repo:

```bash
pod-sync install-skills
```

This drops two skills into your IDE's global skills directory. Invoke them as slash commands from any project repo:

- **/pod-sync-update** — log your working session. The agent collects context from git automatically, shows you a draft, and pushes after you confirm. Run it as often as you like — each session is its own entry. If you created or updated OpenSpec changes, it mirrors those documents to the logging branch in the same flow.
- **/pod-sync-read** — read what the team did. Ask for a specific person, a date range, who's active right now, or just "catch me up".

Or open the dashboard directly: **http://localhost:7823**

## OpenSpec integration

Pod-Sync does not replace [OpenSpec](https://github.com/Fission-AI/OpenSpec) —
you keep using OpenSpec's own workflow, which creates documents under
`openspec/changes/` on your working branch as normal. Pod-Sync mirrors those
documents to the `logging` branch (read-only against your working tree) so the
whole team's proposals are viewable in one place alongside status updates.

## How it works

```
your IDE agent
  → reads Pod-Sync skill (understands the protocol)
  → calls MCP tool (log_status / read_status / log_openspec_event / read_presence)
  → server.py runs locally on your machine
  → git push/pull syncs with teammates via each project repo's logging branch
```

## The logging branch standard

Every project repo you work in gets a `logging` branch — an orphan branch that
contains only coordination data, never project code. Status entries and
OpenSpec proposals live there. It never gets merged into main. Pod-Sync
enforces this automatically.

All Pod-Sync git work happens in a hidden worktree
(`~/.local/share/pod-sync/worktrees/`). Your checkout, your branch, and your
uncommitted changes are never touched.

## Data

All data lives on each project repo's `logging` branch:

- Status entries and OpenSpec events → `entries/<author>.jsonl` (one file per
  author, so teammates never hit git merge conflicts)
- Older than 90 days → `archive/<author>-YYYY-W##.jsonl`
- Mirrored OpenSpec documents → `openspec/changes/...` (originals stay on
  your working branch)
- Nothing is ever deleted. Archive is fully searchable.

Presence ("who's active right now?") is derived from recent commit activity
on origin — no heartbeat files, no background commit noise.

## Auth

Uses your existing git credentials — SSH key or PAT — stored in your OS
keychain. Setup handles this. Nothing is stored in this repo.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

Tests run against real git repos in a temp directory — no network needed.

## Built by

Aahil Afraz — AI-native developer tooling.
