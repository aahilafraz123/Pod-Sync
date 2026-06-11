# Pod-Sync

**Team coordination for AI-native dev pods.**

When everyone on a team builds with AI agents, individual speed goes up but shared context falls apart — nobody knows what anyone else (or anyone else's agent) did yesterday. Pod-Sync fixes that with two slash commands and a dashboard: log your working session when you finish, read your teammates' sessions when you start.

---

## The idea in one minute

- **The skills are the instructions.** Two `SKILL.md` files teach your IDE agent the protocol.
- **The IDE agent is the brain.** It gathers context from git and your conversation, drafts your update, and asks you to confirm.
- **The MCP server is the hands.** A small local server (`server.py`) does all the actual git work — reliably, the same way every time.
- **The web UI is the eyes.** A dashboard at `localhost:7823` shows the whole team's updates and proposals in one place.

All of it syncs through a branch named **`logging`** in each project repo — a branch that holds only team updates and OpenSpec documents, never code. No hosted service, no accounts, no database. If your team can push to the repo, Pod-Sync works.

---

## Quick start

**1. Install (once per machine):**

```bash
git clone git@github.com:aahilafraz123/Pod-Sync.git ~/tools/pod-sync
cd ~/tools/pod-sync
./install.sh
```

A browser window opens — pick your IDE, verify git auth, done. This configures the MCP server in your IDE.

**2. Install the skills:**

```bash
pod-sync install-skills        # global: VS Code, OpenCode
pod-sync install-skills .      # per repo: required for Windsurf
```

Windsurf only loads skills per project, so run the second command once inside each repo you work in. (Skills follow the cross-IDE [Agent Skills](https://code.visualstudio.com/docs/agent-customization/agent-skills) convention — `<skill-name>/SKILL.md`.)

**3. Use it:**

| Command | When | What happens |
|---------|------|--------------|
| `/pod-sync-update` | End of a working session | Agent drafts your update from git + conversation, you confirm, it's pushed. Run it as often as you like — every session is its own entry. |
| `/pod-sync-read` | Start of a working session | "Catch me up", "what did Naresh do yesterday?", "any blockers?", "who's active?" |

Or open the dashboard: **http://localhost:7823**

---

## What gets logged

Each `/pod-sync-update` entry captures:

- **Summary** — what you worked on and why it matters (drafted by the agent, approved by you)
- **Files touched** — pulled from your git diff automatically
- **Blockers** — anything stuck or waiting
- **Next up** — what you'll pick up next, specific enough that a teammate could continue it

Your name comes from `git config user.name` — you're never asked for it.

---

## The `logging` branch

Every project repo gets one branch named `logging`. The rules, enforced in code:

- **It never contains code.** It's created as an orphan branch with an empty history — project files structurally can't leak onto it.
- **It never gets merged** into main or anything else. It's a permanent side channel.
- **Your checkout is never touched.** Pod-Sync does all its git work in a hidden worktree (`~/.local/share/pod-sync/worktrees/`) — your branch, your uncommitted changes, and your agent's work in progress are never stashed, switched, or disturbed.
- **It's created for you** the first time you log in a repo (and you're told when that happens). If a teammate already created it, yours syncs to theirs.

---

## OpenSpec integration

If your team uses [OpenSpec](https://github.com/Fission-AI/OpenSpec), keep using it exactly as before — it creates documents under `openspec/changes/` on your working branch like normal. When you run `/pod-sync-update`, Pod-Sync notices those changes and **mirrors a copy to the `logging` branch**, so every proposal across the team is viewable in one place alongside status updates. The originals on your working branch are untouched — Pod-Sync only reads them.

---

## How a log flows

```
you: "/pod-sync-update"
  → IDE agent reads the skill, gathers context from git
  → drafts your entry, you say "go"
  → agent calls the MCP tool (log_status)
  → server.py commits to the logging branch in a hidden worktree
  → pushes to origin → teammates see it on their next /pod-sync-read
```

---

## Where the data lives

Everything is on each project repo's `logging` branch — plain files, fully inspectable:

| What | Where |
|------|-------|
| Status entries & OpenSpec events | `entries/<author>.jsonl` — one file per person, so simultaneous pushes never conflict |
| Entries older than 90 days | `archive/<author>-YYYY-W##.jsonl` — nothing is ever deleted |
| Mirrored OpenSpec documents | `openspec/changes/...` |

**Presence** ("who's active right now?") isn't stored at all — it's derived live from recent commit activity on origin. No heartbeat files, no background noise in your git history.

---

## Auth

Pod-Sync uses your existing git credentials — SSH key or a PAT stored in your OS keychain during setup. No tokens, secrets, or config ever touch the repo.

---

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

Tests run against real git repos in a temp directory — no network needed.

---

## Built by

Aahil Afraz — AI-native developer tooling.
