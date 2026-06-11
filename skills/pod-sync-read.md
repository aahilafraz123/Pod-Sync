---
name: pod-sync-read
description: "Use this skill ONLY when a team member wants to READ, VIEW, or GET CONTEXT
from the team log. Triggers: 'what did [person] do today', 'catch me up', 'check the
log', 'what happened yesterday', 'any blockers', 'what is [person] working on', 'who
is active', 'any openspec proposals this week', 'show me the latest entries'. Do NOT
use to write, log, or add anything — that is pod-sync-update or pod-sync-openspec.
THIS SKILL IS READ ONLY. It will never write to disk, modify any file, or run git
commit, git push, or git add under any circumstances."
---

# Pod-Sync — Status Read Skill

> **THIS IS A READ-ONLY SKILL.**
> It calls `read_status()` and `read_presence()` to retrieve and present team data.
> It does not write files. It does not commit. It does not push. It does not modify
> the filesystem in any way, ever, for any reason.
> If the user wants to LOG or WRITE a status entry, STOP — use `pod-sync-update`.
> If the user wants to create an OpenSpec proposal, STOP — use `pod-sync-openspec`.

---

## PERMISSION BOUNDARY

```
ALLOWED:
  - Call read_status(who, when, query) MCP tool
  - Call read_presence() MCP tool
  - Format and present results to the user
  - Mention the web dashboard at localhost:7823 for richer viewing

FORBIDDEN — NEVER DO ANY OF THESE:
  - Writing to any entry file or any file at all
  - Running git commit, git push, git add
  - Calling log_status() or log_openspec_event()
  - Modifying, creating, or deleting any file on disk
  - Directly reading entry files (entries/*.jsonl) from the filesystem (use the MCP tool)
```

If the user's request crosses this boundary, name the correct skill and stop.

---

## HARD RULES

1. **This skill is strictly READ ONLY.** No writes. No commits. No pushes. No file creation. No file modification. No exceptions.
2. **All reads go through the MCP tools.** Call `read_status()` or `read_presence()`. Do not read entry files directly from disk. The MCP tool syncs with origin to ensure fresh data.
3. **Never fabricate entries.** If no data exists for a query, say so plainly. Do not generate plausible-sounding status entries.
4. **Never switch to writing.** If the user says "actually, log my status" mid-conversation, tell them that requires `pod-sync-update` and stop. Do not cross the boundary.
5. **Two tools, two purposes.** `read_status()` is for entries (status logs and openspec events). `read_presence()` is for who is online right now. Do not use one when you need the other.

---

## Execution Phases

```
PHASE 1 → Understand what the user is asking for
PHASE 2 → Call the correct MCP tool(s)
PHASE 3 → Present the output in the right format
```

Every phase is read-only. No phase writes anything.

---

## PHASE 1 — Understand the Query

> Reminder: READ ONLY. This phase interprets the request. No tool calls yet.

**Goal:** Determine exactly what to retrieve before calling anything.

Resolve these dimensions from the user's request:

| Dimension | Options |
|-----------|---------|
| **Who** | A specific team member (partial name match OK) / `"all"` |
| **When** | `"today"` / `"yesterday"` / `"this week"` / `"last week"` / `"latest"` / `"YYYY-MM-DD"` / `"YYYY-MM-DD:YYYY-MM-DD"` range |
| **What** | Status entries / OpenSpec entries / both / presence only |
| **Query** | Optional keyword filter (e.g. "rate limiting", "blockers") |

### Defaults when the request is vague

If the user says "catch me up", "check the log", or something equally open-ended:

```
who   = "all"
when  = "latest"
query = ""
```

This returns the most recent entry per author, both types.

### Presence vs. status

- "Who's online?", "Who's active?", "Before standup" --> call `read_presence()`. Do NOT call `read_status()`.
- "What did X do today?" --> call `read_status()`. Do NOT call `read_presence()`.
- "Catch me up and who's online?" --> call both.

**Do not ask clarifying questions unless the request is genuinely ambiguous.** Make a reasonable inference and proceed.

---

## Repo context — collect this silently before calling any tool

Pass `repo_path` as the absolute path to the current workspace root. Determine
it from the workspace — never ask the user for it unless the workspace is
genuinely ambiguous. If you need to confirm which repo you are in,
`git remote get-url origin` is the only read-only command you need.

The MCP tools read from the repo's `logging` branch through a hidden Pod-Sync
worktree — they never switch branches or touch the user's working tree, and
neither should you.

---

## PHASE 2 — Call the MCP Tool(s)

> Reminder: READ ONLY. These tools only retrieve data. They never write.

### For entry queries

```
read_status(
    repo_path="[absolute path to workspace root]",
    who="all" or "partial name",
    when="latest" or "today" or "YYYY-MM-DD" or "YYYY-MM-DD:YYYY-MM-DD",
    query="optional keyword"
)
```

The tool handles:
- Syncing the logging branch from origin before reading (data is always fresh;
  if origin is unreachable it returns last known data with a warning)
- Loading every author's entry file (`entries/<author>.jsonl`, current 90-day window)
- Loading from archive files if the date range extends beyond 90 days
- Filtering by author, date, and keyword
- Returning both `status` and `openspec_proposal` entry types

### For presence queries

```
read_presence(
    repo_path="[absolute path to workspace root]"
)
```

The tool handles:
- Fetching the latest refs from origin (read-only — never touches the working tree)
- Deriving presence from recent commit activity (last push per author, all branches)
- Classifying each teammate as active (pushed within 30 min) or away
- Returning a formatted presence list

There is no heartbeat file — presence reflects who has actually pushed
commits recently, which in an AI-native pod (agents commit frequently)
tracks real activity closely.

---

## PHASE 3 — Present the Output

> Reminder: READ ONLY. This phase formats and displays results. Nothing else.

Pick the output mode that best matches the query. Do not mix modes unprompted.

---

### Mode A — Quick Catch-Up
_Use when: vague request, "catch me up", "latest status", multiple authors_

```
[AUTHOR]  (logged [DATE] at [TIME])
[2-3 sentence synthesis of their status]

[AUTHOR]  (logged [DATE] at [TIME])
[openspec proposal] — [title] — [repo]/logging
```

One block per person. Newest first.

---

### Mode B — Full Entry
_Use when: user asks to see the full entry, wants raw detail, asks about a specific date_

Present the full entry as returned by the tool. Keep all fields intact. Do not paraphrase or omit sections.

---

### Mode C — Focused Answer
_Use when: user asks a specific question ("any blockers?", "what branch is X on?")_

Answer directly in 1-2 sentences. Cite the source:

```
[Direct answer]

Source: [AUTHOR] — [DATE]
```

---

### Mode D — Presence
_Use when: "who's active", "who's online", "before standup"_

Present exactly what `read_presence()` returns:

```
Active now:
  [Name]  — last seen [N] min ago  — [branch]

Away:
  [Name]  — last seen [N] hours ago
```

---

### Flags to Surface After Output

After delivering the main output, scan results for these and include only if present:

- **Unresolved blockers** appearing across multiple entries without resolution
- **Logging gaps** of 2+ working days for any author
- **Who has NOT logged today** (compare active teammates against today's entries)

Keep flags to 1-3 lines. Do not manufacture concerns from clean logs.

If the user wants a richer visual view, mention the web dashboard at `localhost:7823`.

---

## Edge Cases

| Situation | Action |
|-----------|--------|
| No entries match the query | Say so plainly: "No entries found for [who] [when]." Do not fabricate. |
| Only one author has entries | Show their data. Note that others have not logged. |
| User asks to write/log mid-conversation | Stop. Tell them: "Logging requires the pod-sync-update skill." Do not switch. |
| Query spans beyond 90 days | `read_status()` handles archive loading automatically when given date ranges. |
| Tool returns an error | Surface the error message. Do not retry without user input. |
| User asks "what did I log?" | This is a valid read. Call `read_status(who=[their name], when="latest")`. |

---

## What This Skill Does NOT Do

These are explicit exclusions. If you find yourself doing any of these, you have crossed the boundary.

- Log a status entry (use `pod-sync-update`)
- Create, update, or archive an OpenSpec proposal (use `pod-sync-openspec`)
- Write to any entry file or any other file
- Run `git commit`, `git push`, `git add`
- Modify the filesystem in any way
- Read entry files directly from disk (always use the MCP tool)
