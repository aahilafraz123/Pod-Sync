---
name: pod-sync-read
description: "Invoked via /pod-sync-read (or when the user explicitly asks to
read the team log, e.g. 'catch me up', 'what did [person] do', 'any blockers',
'who's active', 'any openspec proposals this week'). Reads team status,
OpenSpec events, and presence from the project repo's logging branch through
the Pod-Sync MCP tools. STRICTLY READ-ONLY. To log anything, use
/pod-sync-update instead."
---

# Pod-Sync — Read Skill

Retrieve and present team data from the project repo's `logging` branch.
Two MCP tools: `read_status()` for entries (status logs and OpenSpec events),
`read_presence()` for who is active right now. Both are read-only and never
touch the user's working tree.

## Boundaries

ALLOWED:
- Calling `read_status()` and `read_presence()`
- Formatting and presenting results
- Mentioning the web dashboard at localhost:7823 for a richer view

FORBIDDEN:
- Calling `log_status()` or `log_openspec_event()`, or writing any file
- Running `git add`, `commit`, `push`, `checkout`, `stash`
- Reading entry files (`entries/*.jsonl`) directly from disk — always use the tool
- Fabricating entries. If nothing matches, say so plainly.

If the user asks to log something mid-conversation, that is /pod-sync-update —
say so and stop. If the Pod-Sync MCP tools are not available in this session,
stop and tell the user to run `./install.sh`. Do not simulate the tools with git.

## Procedure

**1. Resolve the query** before calling anything:

| Dimension | Options |
|-----------|---------|
| who | partial name match (case-insensitive) / `"all"` |
| when | `"latest"` / `"today"` / `"yesterday"` / `"this week"` / `"last week"` / `"YYYY-MM-DD"` / `"YYYY-MM-DD:YYYY-MM-DD"` |
| query | optional keyword filter |

Vague request ("catch me up", "check the log") → `who="all", when="latest"`,
which returns the most recent entry per author. Make a reasonable inference
rather than asking clarifying questions, unless genuinely ambiguous.

Pick the right tool:
- "Who's online / active / around?", "what branch is X on?" → `read_presence()`
- "What did X do?", "any blockers?", "openspec proposals?" → `read_status()`
- "Catch me up and who's online?" → both

`repo_path` is the absolute path to the current workspace root — determine it
from the workspace, never ask.

**2. Call the tool(s):**

```
read_status(repo_path=..., who=..., when=..., query=...)
read_presence(repo_path=...)
```

`read_status` syncs from origin before reading (returns last known data with a
warning if offline) and pulls archive files automatically for date ranges older
than 90 days. `read_presence` is derived from recent commit activity on origin —
active means a push within 30 minutes.

**3. Present the output.** Convert timestamps to the user's local time. Pick one
format:

- **Quick catch-up** (vague request, multiple authors): one block per person,
  newest first — author, date, 2-3 sentence synthesis. OpenSpec events as
  `[openspec] — title — repo/logging`.
- **Full entry** (user wants raw detail or a specific date): all fields intact,
  no paraphrasing.
- **Focused answer** (a specific question): 1-2 sentences, then
  `Source: [AUTHOR] — [DATE]`.
- **Presence**: present what `read_presence()` returns, as-is.

After the main output, flag (1-3 lines, only if present): unresolved blockers
recurring across entries, authors with no log for 2+ working days, and who has
not logged today. Do not manufacture concerns from clean logs. Mention the
dashboard at `localhost:7823` if the user wants a visual view.

## Edge cases

| Situation | Action |
|-----------|--------|
| No entries match | "No entries found for [who] [when]." Do not fabricate. |
| Only one author has logged | Show their data; note others have not logged. |
| "What did I log?" | Valid read — `read_status(who=[their name], when="latest")`. |
| Multiple entries per author per day | Normal — one entry per working session. `when="today"` shows all of them; `when="latest"` shows only the newest. |
| Tool notes it created the `logging` branch | Relay that note to the user — no team log existed for this repo yet. |
| Tool returns a sync warning | Present the data and relay the warning. |
| Tool returns an error | Surface it. Do not retry without user input. |
