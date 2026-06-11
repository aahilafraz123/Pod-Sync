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

From any project in your IDE:

```
"log my status for today"
"catch me up on the team"
"what did Naresh work on yesterday?"
"any blockers on the team?"
"who's active right now?"
"create an openspec proposal for rate limiting"
"any openspec proposals this week?"
```

Or open the dashboard: **http://localhost:7823**

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
