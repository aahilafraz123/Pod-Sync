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

This drops three skills into your IDE's global skills directory. Once installed, open your IDE and invoke them directly from the Skills panel:

- **pod-sync-update** — log your end-of-day status. The agent collects context from git automatically, shows you a draft, and pushes after you confirm.
- **pod-sync-read** — read what the team did. Ask for a specific person, a date range, or just catch me up on everyone.
- **pod-sync-openspec** — create or update an OpenSpec proposal. The agent commits it to the logging branch of your current repo and surfaces it in the team dashboard.

Or open the dashboard directly: **http://localhost:7823**

## How it works

```
your IDE agent
  → reads Pod-Sync skill (understands the protocol)
  → calls MCP tool (log_status / read_status / log_openspec_event / read_presence)
  → server.py runs locally on your machine
  → git push/pull syncs with teammates via shared Pod-Sync repo
```

## The logging branch standard

Every project repo you work in gets a `logging` branch. OpenSpec proposals
live there. It never gets merged into main. Pod-Sync enforces this automatically.

## Data

- Status entries and OpenSpec events → `team-status/entries.json`
- Older than 90 days → `team-status/archive/YYYY-W##.json`
- Nothing is ever deleted. Archive is fully searchable.

## Auth

Uses your existing git credentials — SSH key or PAT — stored in your OS
keychain. Setup handles this. Nothing is stored in this repo.

## Built by

Aahil Afraz — AI-native developer tooling.
