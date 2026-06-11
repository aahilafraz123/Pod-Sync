# Pod-Sync — Agent Build Spec

> Drop this file into Windsurf and say: "Build Pod-Sync according to this spec."
> Everything the agent needs is here. Do not deviate from the architecture.

---

## What is Pod-Sync

Pod-Sync is a team coordination tool for AI-native development pods. It solves a specific problem: when your whole team develops with agentic AI tools, individual velocity is high but shared context collapses. Nobody knows what anyone else's agent did today, what proposals are in flight, or who is actively working on what.

Pod-Sync fixes this with three things:

- A **skill layer** (markdown files) that teaches IDE agents the full status and OpenSpec protocol
- An **MCP server** (`server.py`) that gives those agents reliable, atomic execution
- A **web UI** (`localhost:7823`) that is the primary reading surface — a live team dashboard

The skill is the brain. The MCP is the hands. The web UI is the eyes.

**This is not a hosted service. It is a cloneable repo that installs itself and runs entirely on each teammate's machine.**

---

## Core concepts — read before building anything

### Two-store data architecture

Pod-Sync uses two parallel stores, always written together:

| Store | File | Purpose |
|-------|------|---------|
| Structured | `team-status/entries.json` | Ground truth. All entries with full metadata. Queryable. |
| Archive | `team-status/archive/YYYY-W##.json` | Entries older than 90 days, grouped by ISO week. |

**There is no markdown log file.** The MD file was eliminated. `entries.json` is the database. The web UI is the reading surface. The agent reads from `entries.json` directly when asked — never from markdown.

### Entry types

Every record in `entries.json` has a `type` field. Two types exist:

- `"status"` — end-of-day status log written by `log_status()`
- `"openspec_proposal"` — OpenSpec event written by `log_openspec_event()`

Both types live in the same array, follow the same archival rules, and render as cards in the web UI.

### The logging branch standard

Pod-Sync enforces a **`logging` branch standard** across all project repos. This branch is a permanent side channel for team coordination artifacts. It never gets merged into `main`. It never contains feature code.

The `logging` branch in every project repo contains:
- OpenSpec proposals and documents (`openspec/` folder)
- Any other coordination artifacts specific to that repo

When Pod-Sync writes to a project repo, it always writes to the `logging` branch — never `main`, never a feature branch. This is non-negotiable and enforced in code, not just convention.

### OpenSpec dual-write

When a teammate creates or updates an OpenSpec proposal:

1. The full proposal document is committed to the **project repo's `logging` branch**
2. A lightweight event record is written to **Pod-Sync's `entries.json`**

The proposal lives in the project repo. Its existence is visible in the Pod-Sync team dashboard. Both writes happen atomically as part of the same skill invocation.

### Rolling archive

`entries.json` is a rolling 90-day window. Entries older than 90 days are moved to weekly archive files (`team-status/archive/YYYY-W##.json`) by the `archive_old_entries()` function, which runs on every write. Nothing is ever deleted. Archive files are committed to the repo and travel with it on fresh clones.

---

## Repo name and location

```
repo name: pod-sync
recommended clone location: ~/tools/pod-sync
```

The teammate clones it once, runs `./install.sh` once, and then never opens the folder again. The tool is available globally in their IDE from any project repo. The web UI is always at `localhost:7823` because `server.py` starts when the IDE spawns it as an MCP subprocess.

---

## Full repo structure

Build exactly this. No extras.

```
pod-sync/
├── install.sh                          ← entry point, run once per machine
├── server.py                           ← MCP server + local HTTP API
├── requirements.txt                    ← mcp, fastapi, uvicorn
├── skills/
│   ├── pod-sync-update.md              ← skill: log a status entry
│   ├── pod-sync-read.md                ← skill: read entries and presence
│   └── pod-sync-openspec.md            ← skill: OpenSpec dual-write flow
├── web-ui/
│   └── index.html                      ← team dashboard + setup wizard
├── team-status/
│   ├── entries.json                    ← rolling 90-day entry store
│   └── archive/                        ← weekly archive files, auto-created
│       └── .gitkeep
├── heartbeat/
│   └── presence.json                   ← auto-written by server.py on startup
└── README.md
```

`.gitignore` must include:
```
.venv/
__pycache__/
*.pyc
```

Everything else, including all files in `team-status/` and `heartbeat/`, gets committed.

---

## Layer 1 — install.sh

The only thing a teammate runs manually. Single command, no arguments.

### What it does in order

1. Checks Python 3.9+ is available. If not, prints install instructions and exits cleanly.
2. Creates virtualenv at `pod-sync/.venv/`
3. Activates it, runs `pip install -r requirements.txt -q`
4. Resolves the **absolute path** of the repo on this machine
5. Starts `server.py` on `localhost:7823` in HTTP mode (not stdio — stdio is for the IDE, HTTP is for the browser)
6. Opens `http://localhost:7823` in the default browser (`open` macOS, `xdg-open` Linux, `start` Windows)
7. The web UI handles setup from here. install.sh waits for the completion signal.
8. On completion signal: writes MCP config to IDE global settings, copies skills to IDE global skills dir, prints success and exits.

### MCP config written per IDE

**Windsurf** → `~/.codeium/windsurf/mcp_settings.json`
**VS Code** → `~/Library/Application Support/Code/User/mcp.json` (macOS) / `~/.config/Code/User/mcp.json` (Linux) / `%APPDATA%\Code\User\mcp.json` (Windows)
**OpenCode** → `~/.config/opencode/config.json`

MCP entry (absolute paths baked in at install time):
```json
{
  "mcpServers": {
    "pod-sync": {
      "command": "/ABSOLUTE/PATH/TO/pod-sync/.venv/bin/python",
      "args": ["/ABSOLUTE/PATH/TO/pod-sync/server.py", "--stdio"]
    }
  }
}
```

**Always merge into existing config. Never overwrite the whole file.**

### Skills written per IDE

```
Windsurf:  ~/.codeium/windsurf/memories/
VS Code:   ~/.vscode/skills/
OpenCode:  ~/.config/opencode/skills/
```

Copy all three files from `skills/` into the IDE's global skills directory.

---

## Layer 2 — server.py

`server.py` runs in two modes depending on how it is invoked:

- `python server.py --stdio` — MCP stdio mode, spawned by the IDE as a subprocess
- `python server.py --http` — HTTP mode, started by install.sh and kept running for the web UI

Both modes share the same tool logic. The `--http` flag wraps the same functions in FastAPI endpoints instead of MCP tool handlers.

### Dependencies

```
requirements.txt:
mcp
fastapi
uvicorn
```

Three dependencies. No vector DB. No embedding model. No ChromaDB.

### Startup behavior (both modes)

1. `REPO_ROOT = pathlib.Path(__file__).parent.resolve()` — always resolves correctly regardless of clone location
2. Ensures `team-status/entries.json` exists. If not, creates it with `[]`.
3. Ensures `team-status/archive/` exists.
4. Ensures `heartbeat/presence.json` exists. If not, creates it with `{}`.
5. Writes/updates the current user's heartbeat entry (author from `git config user.name`, branch from `git branch --show-current`, timestamp now)
6. Pushes heartbeat silently (fail silently — heartbeat is best-effort)
7. Starts background heartbeat thread (updates every 10 minutes, pushes silently)
8. Registers MCP tools (stdio mode) or FastAPI routes (HTTP mode)

### Git operations

All git operations run inside `REPO_ROOT`. The server never touches any other repo's git state except via `ensure_logging_branch()` which is explicitly scoped to a caller-provided path.

Standard write sequence for Pod-Sync repo:
```
git pull --rebase origin main
[write to entries.json and/or presence.json]
git add team-status/ heartbeat/
git commit -m "[type]: [author] — [date]"
git push origin main
```

On any push failure, inspect stderr for `"SAML"`, `"SSO"`, `"authentication"`, `"403"`, `"Permission denied"` and return a specific actionable error message. Never return a raw Python traceback as a tool response.

### Helper: `atomic_write(path, data)`

All JSON writes use atomic write — write to `.tmp` file, then `os.replace()` to target. This prevents partial writes if the process is interrupted.

```python
def atomic_write(path: Path, data: list | dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)
```

### Helper: `archive_old_entries()`

Runs after every write to `entries.json`. Fast no-op if nothing is old enough to archive.

```python
def archive_old_entries():
    cutoff = datetime.now() - timedelta(days=90)
    entries = load_entries()

    to_keep = [e for e in entries if parse_date(e["date"]) >= cutoff]
    to_archive = [e for e in entries if parse_date(e["date"]) < cutoff]

    if not to_archive:
        return

    by_week = {}
    for entry in to_archive:
        week = datetime.strptime(entry["date"], "%Y-%m-%d").strftime("%G-W%V")
        by_week.setdefault(week, []).append(entry)

    for week, week_entries in by_week.items():
        archive_path = REPO_ROOT / "team-status" / "archive" / f"{week}.json"
        existing = json.loads(archive_path.read_text()) if archive_path.exists() else []
        atomic_write(archive_path, existing + week_entries)

    atomic_write(REPO_ROOT / "team-status" / "entries.json", to_keep)
```

### Helper: `ensure_logging_branch(repo_path: Path)`

Runs before any git write to a project repo. Ensures the `logging` branch exists and is checked out. Never touches `main`.

```python
def ensure_logging_branch(repo_path: Path):
    branches = subprocess.run(
        ["git", "branch", "--list", "logging"],
        cwd=repo_path, capture_output=True, text=True
    ).stdout.strip()

    if "logging" not in branches:
        subprocess.run(
            ["git", "checkout", "-b", "logging"],
            cwd=repo_path, check=True, capture_output=True
        )
    else:
        subprocess.run(
            ["git", "checkout", "logging"],
            cwd=repo_path, check=True, capture_output=True
        )
```

### Helper: `load_entries(include_archive=False, weeks=None)`

```python
def load_entries(include_archive=False, weeks=None):
    path = REPO_ROOT / "team-status" / "entries.json"
    entries = json.loads(path.read_text()) if path.exists() else []

    if include_archive and weeks:
        for week in weeks:
            ap = REPO_ROOT / "team-status" / "archive" / f"{week}.json"
            if ap.exists():
                entries += json.loads(ap.read_text())

    return entries
```

---

## Layer 3 — MCP tools

### `log_status`

```python
@mcp.tool()
def log_status(
    summary: str,
    files_touched: list[str] = [],
    blockers: str = "None",
    next_up: str = ""
) -> str:
    """
    Log your end-of-day status entry to the shared team log.
    Author is auto-detected from git config — never ask the user for their name.
    summary: 2-4 sentences, past tense, specific. Synthesize from conversation context.
    files_touched: list of file paths modified today. Can be empty list.
    blockers: anything blocking progress, or 'None'.
    next_up: what you are picking up tomorrow. Specific enough a teammate could continue it.
    """
```

**Write sequence:**
1. Build entry dict:
```python
{
    "id": str(uuid4()),
    "type": "status",
    "author": git_config_username(),
    "date": today_iso(),           # "2026-06-11"
    "week": iso_week(),            # "2026-W24"
    "timestamp": now_iso(),        # "2026-06-11T16:42:00"
    "summary": summary,
    "files_touched": files_touched,
    "blockers": blockers,
    "next_up": next_up,
    "archived": False
}
```
2. Check if author already has an entry for today. If yes, return a message asking whether to append as mid-day update or replace. Do not overwrite silently.
3. `git pull --rebase origin main` on Pod-Sync repo
4. Append entry to `entries.json` via `atomic_write()`
5. `archive_old_entries()`
6. Commit and push to Pod-Sync repo `main`
7. Return `"✅ Logged and pushed. (author, date)"`

---

### `read_status`

```python
@mcp.tool()
def read_status(
    who: str = "all",
    when: str = "latest",
    query: str = ""
) -> str:
    """
    Read status and OpenSpec entries from the team log.
    who: 'all' or a partial name match (case-insensitive).
    when: 'latest', 'today', 'yesterday', 'this week', 'last week',
          or an ISO date 'YYYY-MM-DD', or a range 'YYYY-MM-DD:YYYY-MM-DD'.
    query: optional keyword filter applied after date/author filtering.
           Matches against summary, title, files_touched fields.
    Always git pulls before reading to ensure fresh data.
    """
```

**Read sequence:**
1. `git pull --rebase origin main` on Pod-Sync repo
2. Determine if `when` requires archive data. If the date range extends beyond 90 days, calculate which ISO weeks are needed and pass them to `load_entries(include_archive=True, weeks=[...])`
3. Filter by `who` (partial case-insensitive match on `author` field)
4. Filter by `when` (parse date range, filter by `date` field)
5. If `query` provided, filter by keyword match across `summary`, `title`, `files_touched`
6. If no entries match, return `"No entries found for that query."` — never fabricate
7. Format and return results. Type-aware formatting:
   - `"status"` entries → author, date, summary, blockers, next_up
   - `"openspec_proposal"` entries → author, date, title, repo, branch, status

---

### `log_openspec_event`

```python
@mcp.tool()
def log_openspec_event(
    title: str,
    repo: str,
    repo_path: str,
    event_type: str = "proposal_created",
    openspec_path: str = "openspec/changes/",
    notes: str = ""
) -> str:
    """
    Called after an OpenSpec proposal is created or updated in a project repo.
    Performs two writes:
      1. Ensures the proposal is committed to the logging branch of the project repo
      2. Writes a lightweight event record to Pod-Sync entries.json

    title: human-readable proposal title e.g. "Alert Engine Rate Limiting"
    repo: repo name e.g. "dashboard-repo"
    repo_path: absolute path to the project repo on this machine
    event_type: "proposal_created", "proposal_updated", "proposal_archived"
    openspec_path: path within repo to the OpenSpec change folder
    notes: optional one-line note about the proposal
    """
```

**Write sequence:**

**Write 1 — project repo, logging branch:**
1. `ensure_logging_branch(Path(repo_path))`
2. `git add openspec/` in project repo
3. `git commit -m "openspec: [title] ([event_type])"` in project repo
4. `git push origin logging` in project repo
5. If any step fails, return specific error — do not proceed to Write 2

**Write 2 — Pod-Sync repo, entries.json:**
1. Build event dict:
```python
{
    "id": str(uuid4()),
    "type": "openspec_proposal",
    "author": git_config_username(),
    "date": today_iso(),
    "week": iso_week(),
    "timestamp": now_iso(),
    "title": title,
    "repo": repo,
    "branch": "logging",
    "path": openspec_path,
    "event_type": event_type,
    "notes": notes,
    "archived": False
}
```
2. `git pull --rebase origin main` on Pod-Sync repo
3. Append to `entries.json` via `atomic_write()`
4. `archive_old_entries()`
5. Commit and push to Pod-Sync repo `main`
6. Return `"✅ OpenSpec event logged. Proposal committed to [repo]/logging. Event visible in team dashboard."`

---

### `read_presence`

```python
@mcp.tool()
def read_presence() -> str:
    """
    Show who on the team is currently active (has Pod-Sync running).
    Reads heartbeat/presence.json. Active = last_seen within 30 minutes.
    Useful to call before standups.
    Always git pulls before reading.
    """
```

**Read sequence:**
1. `git pull --rebase origin main` on Pod-Sync repo
2. Read `heartbeat/presence.json`
3. For each entry, compute minutes since `last_seen`
4. Split into active (≤30 min) and away (>30 min)
5. Return formatted output:
```
Active now:
  Naresh  — last seen 4 min ago  — feat/alert-engine
  Aahil   — last seen 1 min ago  — feat/dashboard-v2

Away:
  (nobody)
```

---

## Layer 4 — web-ui/index.html

A single self-contained HTML file. Vanilla HTML/CSS/JS only. No frameworks. No CDN calls. Served by `server.py` in HTTP mode on `localhost:7823`.

`server.py` exposes these HTTP endpoints consumed by the web UI:

```
GET  /api/entries          → returns entries.json array (current only, no archive)
GET  /api/presence         → returns presence.json
GET  /api/search?q=...     → keyword search across entries
GET  /api/detect-ides      → runs which windsurf/code/opencode, returns detected IDEs
POST /api/test-ssh         → tests SSH key against git remote
POST /api/store-pat        → stores PAT in OS keychain via git credential approve
POST /api/complete-setup   → writes MCP config + copies skills, signals install.sh
```

### Two modes of the web UI

The web UI detects whether setup has been completed (checks for presence of MCP config entry) and shows the appropriate screen.

**Mode A — Setup wizard** (first run, no MCP config found)

Step 1: IDE selector
- Three large cards: Windsurf, VS Code, OpenCode
- Calls `/api/detect-ides` on load, auto-selects detected IDE if only one found
- Shows "Detected: Windsurf" label under cards

Step 2: Git authentication
- Two options: SSH key (recommended) / Personal access token
- SSH path: calls `/api/test-ssh`, shows green checkmark or error with link to `https://github.com/settings/ssh` and Re-test button
- PAT path: password input, posts to `/api/store-pat`, stores in OS keychain only — never in any file
- Both paths end with a green checkmark confirming git access works

Step 3: Completion
- Calls `/api/complete-setup` with IDE selection
- Server writes MCP config, copies skills
- Shows success screen with usage examples
- Automatically transitions to Mode B (dashboard) after 3 seconds

**Mode B — Team dashboard** (setup complete)

Layout:
```
┌─────────────────────────────────────────────────────┐
│  Pod-Sync                          [search bar]     │
├──────────────────┬──────────────────────────────────┤
│  Presence        │  Team feed                       │
│                  │                                  │
│  Naresh  ● now   │  ┌──────────────────────────┐   │
│  feat/alert-eng  │  │ Naresh  · today           │   │
│                  │  │ [status]                  │   │
│  Aahil   ● now   │  │ Worked on rate limiting.. │   │
│  feat/dashboard  │  └──────────────────────────┘   │
│                  │                                  │
│                  │  ┌──────────────────────────┐   │
│  [who hasn't     │  │ Naresh  · today           │   │
│   logged today]  │  │ [openspec proposal]       │   │
│                  │  │ Alert Engine Rate Lim..   │   │
│                  │  │ dashboard-repo / logging  │   │
│                  │  └──────────────────────────┘   │
└──────────────────┴──────────────────────────────────┘
```

- Presence panel: polls `/api/presence` every 60 seconds. Green dot = active (≤30 min). Gray = away.
- Presence panel also shows who has NOT logged a status entry today (gray names with warning indicator)
- Team feed: loads `/api/entries` on page load, sorted newest first
- Two card templates: `status` cards (summary, blockers, next_up) and `openspec_proposal` cards (title, repo, branch, event_type)
- Search bar: calls `/api/search?q=...` on input with 300ms debounce, replaces feed with results
- No pagination — the 90-day rolling window keeps entry count bounded (~100-900 entries max)

---

## Layer 5 — skills/pod-sync-update.md

```markdown
---
name: pod-sync-update
description: "Use this skill when a teammate wants to LOG or WRITE their status for
today. Triggers: 'log my status', 'end of day update', 'write my standup', 'add my
entry', 'log what I did today'. Do NOT use for reading — that is pod-sync-read.
Do NOT use for OpenSpec — that is pod-sync-openspec."
---

# Pod-Sync — Status Update Skill

## Execution model

Two layers:
- This file defines the protocol: what to collect, how to think, edge cases.
- The MCP tool handles execution: `log_status()` writes entries.json and pushes.

Never write to disk or run git directly. Always call `log_status()`.

## What to collect

Have a conversation, synthesize context, then call the tool.
Do NOT ask for each field like a form.

| Field | How to get it |
|-------|---------------|
| summary | 2-4 sentences past tense. What was worked on, why it matters. Specific. |
| files_touched | From git diff or what the user mentions. Can be empty. |
| blockers | Anything stuck or waiting. "None" if clean. |
| next_up | What picks up tomorrow. Specific enough a teammate could continue it. |

## Confirmation step

Before calling log_status(), show the user a brief preview:

"Here's what I'll log:
  Summary: [...]
  Blockers: [...]
  Next up: [...]
  Say 'go' to submit or correct anything."

Do not log without confirmation.

## Author detection

Never ask the user their name. Auto-detected by the MCP tool from git config.

## Edge cases

- User already logged today: ask whether to append as mid-day update or replace.
- User says "just log it" with no detail: ask for a one-sentence summary minimum.
- Push fails with SSO/auth error: tell the user to re-authorize their SSH key or PAT.
  Link: https://github.com/settings/ssh
```

---

## Layer 6 — skills/pod-sync-read.md

```markdown
---
name: pod-sync-read
description: "Use this skill when a teammate wants to READ, VIEW, or GET CONTEXT from
the team log. Triggers: 'what did [person] do today', 'catch me up', 'check the log',
'what happened yesterday', 'any blockers', 'what is [person] working on', 'who is
active', 'any openspec proposals this week'. Do NOT use to write — that is
pod-sync-update or pod-sync-openspec. READ ONLY."
---

# Pod-Sync — Status Read Skill

## Execution model

READ ONLY. Never writes files, never runs git commit or push.
- To read entries: call `read_status(who, when, query)`
- To check presence: call `read_presence()`

## Resolve the query first

| Dimension | Options |
|-----------|---------|
| Who | Specific name (partial match ok) / 'all' |
| When | today / yesterday / this week / last week / latest / YYYY-MM-DD |
| What | status entries / openspec entries / both (default both) |
| Query | optional keyword for filtering |

Default when vague: who=all, when=latest, returns both types.

## Output formats

**Quick catch-up** (vague, multiple authors):
```
[Name]  (logged [date] at [time])
[2-3 sentence synthesis of their status]

[Name]  · [openspec proposal]
[title] — [repo]/logging
```

**Presence check** ("who's active", before standup):
Call `read_presence()`. Do not call `read_status()` for this.

**Focused answer** (specific question):
Answer in 1-2 sentences, cite source entry id and date.

## Rules

- Never fabricate entries. If nothing matches, say so plainly.
- read_status() always git pulls first — data is always fresh.
- Surface unresolved blockers appearing across multiple entries.
- Note if someone has not logged in 2+ working days.
- The web UI at localhost:7823 is the richer reading surface —
  mention it when the user asks for a visual view.
```

---

## Layer 7 — skills/pod-sync-openspec.md

```markdown
---
name: pod-sync-openspec
description: "Use this skill when a teammate creates, updates, or archives an OpenSpec
proposal. Triggers: '/opsx:new', '/opsx:continue', '/opsx:archive', 'create a spec
proposal', 'update the proposal', 'write an openspec for'. This skill wraps the
OpenSpec workflow with the Pod-Sync dual-write flow. Always use this instead of
running OpenSpec commands directly."
---

# Pod-Sync — OpenSpec Skill

## What this skill does

When you create or update an OpenSpec proposal, two things must happen:

1. The full proposal is committed to the **project repo's `logging` branch**
2. A lightweight event is written to **Pod-Sync's entries.json** so it appears
   in the team dashboard

This skill ensures both writes happen. Never do one without the other.

## The logging branch standard

Every project repo has a `logging` branch. This is a permanent side channel.
It never gets merged into main. It only contains:
- OpenSpec proposals and documents (openspec/ folder)
- Any other coordination artifacts for that repo

The MCP tool enforces this. You do not need to manually manage the branch.

## Execution flow

### Creating a new proposal (/opsx:new)

1. Run the OpenSpec new-proposal workflow as normal — generate proposal.md
2. Identify:
   - title: human-readable name of the proposal
   - repo: name of the current project repo
   - repo_path: absolute path to the current project repo
   - openspec_path: path to the new change folder
3. Call:
   log_openspec_event(
     title="[proposal title]",
     repo="[repo name]",
     repo_path="[absolute path]",
     event_type="proposal_created",
     openspec_path="openspec/changes/[folder-name]/",
     notes="[one line summary if useful]"
   )
4. The tool handles:
   - Switching to logging branch in the project repo
   - Committing and pushing the proposal there
   - Writing the event to Pod-Sync entries.json
   - Pushing to Pod-Sync repo

### Updating a proposal (/opsx:continue)

Same flow. event_type = "proposal_updated".

### Archiving a proposal (/opsx:archive)

Same flow. event_type = "proposal_archived".

## What repo_path means

repo_path is the absolute filesystem path to the project repo being worked in,
NOT the pod-sync repo. Example: "/Users/aahil/comcast/dashboard-repo"

The agent can determine this from the current workspace root.

## Edge cases

- logging branch does not exist: the MCP tool creates it automatically.
- Uncommitted changes in project repo before switching branch: stash first,
  switch branch, unstash — the tool handles this.
- Push to logging branch fails: report the error clearly. Do not write to
  Pod-Sync entries.json until the project repo write succeeds.
- User is already on logging branch: proceed normally, no branch switch needed.
```

---

## Layer 8 — Initial files

### team-status/entries.json

```json
[]
```

### team-status/archive/.gitkeep

Empty file. Ensures the archive directory is tracked by git on fresh clone.

### heartbeat/presence.json

```json
{}
```

### README.md

````markdown
# Pod-Sync

> Team coordination for AI-native dev pods.
> Status logs, OpenSpec proposals, presence — all in one place.
> One command to install. Works from any repo you're in.

## Install

```bash
git clone git@github.com:YOUR_ORG/pod-sync.git ~/tools/pod-sync
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

Aahil + Naresh, DevX Protect @ Comcast.
Part of the AI Native Pod workflow.
````

---

## Hard constraints for the agent

Read all of these before writing a single line of code.

1. **No markdown log file.** `team-status.md` does not exist. `entries.json` is the database. The web UI is the reading surface. Do not create any `.md` file in `team-status/`.

2. **No credentials ever touch the repo.** No `.env`. No config file with tokens. No hardcoded paths. OS keychain only via `git credential approve`.

3. **Absolute paths in MCP config are generated at install time** per machine. Never use relative paths in the IDE MCP config entry.

4. **server.py runs in two modes** — `--stdio` for MCP (spawned by IDE) and `--http` for web UI (started by install.sh). The same tool logic serves both. A single `if args.mode == "stdio"` branch at the bottom determines which transport to start.

5. **server.py resolves its own location at startup** via `pathlib.Path(__file__).parent.resolve()`. Never assumes a working directory.

6. **Skills reference MCP tools by name only.** Never by file path, never by direct git commands. Skills = protocol knowledge. MCP = execution.

7. **install.sh merges into existing IDE config files.** Never overwrites. Read existing JSON, insert `pod-sync` key, write back.

8. **Heartbeat pushes fail silently.** A failed heartbeat push must never crash or block server.py in either mode.

9. **Git pull before every read.** `read_status()` and `read_presence()` always pull before returning data.

10. **Git error messages surface as clean tool responses.** Never raw Python tracebacks. Check stderr for SSO/auth signals and return actionable messages.

11. **`ensure_logging_branch()` runs before every write to a project repo.** No exceptions. The logging branch standard is enforced in code.

12. **OpenSpec Write 1 must succeed before Write 2.** If the project repo commit/push fails, do not write to `entries.json`. Return the error. The two writes are sequential with a hard dependency.

13. **`archive_old_entries()` runs after every write.** It is a no-op when nothing is old enough. It must never block or error — wrap in try/except and log to stderr if it fails.

14. **`atomic_write()` is used for all JSON writes.** No direct `file.write()` calls on `entries.json` or archive files.

15. **The web UI is vanilla HTML/CSS/JS.** No React, Vue, Svelte, or any framework. No npm. No CDN calls. Served from `server.py`'s HTTP mode directly.

---

## Implementation order

Build in this exact order. Each step must be independently testable before moving on.

```
1.  repo scaffold — all folders, .gitkeep, .gitignore
2.  requirements.txt
3.  team-status/entries.json (empty array)
4.  heartbeat/presence.json (empty object)
5.  server.py — core helpers only:
      atomic_write(), load_entries(), archive_old_entries(),
      ensure_logging_branch(), git_config_username(), heartbeat writer
6.  server.py — log_status() tool, stdio mode only
7.  TEST: python server.py --stdio → call log_status via MCP inspector
          confirm entries.json written, archive_old_entries runs, git push works
8.  server.py — read_status() tool
9.  TEST: call read_status via MCP inspector, confirm git pull + filter works
10. server.py — log_openspec_event() tool
11. TEST: call log_openspec_event, confirm dual write — logging branch in test
          project repo AND entries.json in pod-sync repo
12. server.py — read_presence() tool
13. server.py — HTTP mode (--http flag, FastAPI routes wrapping same tool functions)
14. TEST: python server.py --http → curl /api/entries, /api/presence, /api/search
15. web-ui/index.html — setup wizard (Steps 1-3)
16. TEST: fresh flow — run install.sh, complete wizard, confirm MCP config written
17. web-ui/index.html — team dashboard (presence panel + team feed)
18. TEST: entries visible as cards, presence updates, search works
19. skills/pod-sync-update.md
20. skills/pod-sync-read.md
21. skills/pod-sync-openspec.md
22. install.sh — full flow: venv, pip, start HTTP server, open browser,
                 wait for /api/complete-setup signal, write MCP config,
                 copy skills, exit
23. README.md
24. END-TO-END TEST:
      fresh clone on a second machine
      → ./install.sh
      → browser wizard completes
      → open any project repo in IDE
      → "log my status" → entries.json updated, git pushed
      → "create openspec proposal for X" → logging branch created in project repo,
         event in entries.json, visible in dashboard at localhost:7823
      → open localhost:7823 → both cards visible, presence shows both machines
```

---

## Key design decisions — do not relitigate these

**Why no markdown log file?**
The web UI is the reading surface. `entries.json` is structured and directly queryable. A markdown file would be a redundant generated artifact that creates sync issues. Eliminated entirely.

**Why stdio + HTTP in one server.py and not two files?**
The tool logic is identical in both modes. Two files means two places to update when tool behavior changes. One file with a mode flag keeps the implementation DRY and the behavior consistent between IDE agent calls and web UI calls.

**Why rolling 90-day window + weekly archive instead of deletion?**
Deletion loses history. A full RAG/vector system is overkill for a 2-10 person team (at 10 people logging daily, 90 days = ~900 entries, trivially small). The archive folder grows at ~5KB/week and is fully searchable by keyword. Add vector search later if the team scales significantly.

**Why the logging branch standard?**
In AI-native development, project repos become noisy — agents commit frequently, branches multiply. A dedicated `logging` branch creates a clean, predictable location for coordination artifacts that never conflicts with feature work and never gets merged away.

**Why dual-write for OpenSpec instead of just the project repo?**
The team dashboard is only useful if it shows everything. If OpenSpec proposals only live in project repos on the `logging` branch, a teammate would have to know which repo to look in. The event record in Pod-Sync makes all proposals visible in one place, regardless of which repo they belong to.

**Why not ChromaDB/vector search?**
At the scale of a 2-10 person team, keyword filtering on a 90-day JSON array is fast enough (sub-5ms). ChromaDB adds 200MB of dependencies, a model download, rebuild logic, and two-store sync complexity. The simple solution works. Add it later if needed.

**Why the localhost setup UI instead of just a README?**
The hardest part of install is generating the correct absolute path for the MCP config entry and writing it to the correct OS-specific location without breaking existing config. A human following README instructions will get this wrong. The UI does it correctly every time.