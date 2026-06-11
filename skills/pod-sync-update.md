---
name: pod-sync-update
description: "Invoked via /pod-sync-update (or when the user explicitly asks to
log their status, e.g. 'log my work', 'end of day update', 'wrap up my day',
'standup notes'). Logs the user's status — and mirrors any OpenSpec changes —
to the project repo's logging branch through the Pod-Sync MCP tools. This is
the WRITE flow. For reading the team log or checking presence, use
/pod-sync-read instead."
---

# Pod-Sync — Update Skill

Turn today's work into a structured status entry on the project repo's
`logging` branch, and mirror any OpenSpec change documents alongside it.
All writes go through two MCP tools: `log_status()` and `log_openspec_event()`.
The tools work in a hidden Pod-Sync worktree — the user's branch, checkout,
and uncommitted changes are never touched, by you or by the tools.

## Boundaries

ALLOWED:
- Read-only git commands to collect context
- Calling `log_status()` and `log_openspec_event()`
- Presenting a draft and waiting for the user's confirmation

FORBIDDEN:
- Writing any file, or running `git add`, `commit`, `push`, `checkout`, `stash`
- Reading or summarizing the team log (that is /pod-sync-read — say so and stop)
- Asking the user for their name (the tool auto-detects it from `git config user.name`)
- Inventing field content that isn't in git or the conversation
- Logging without the user's explicit confirmation

If the Pod-Sync MCP tools are not available in this session, stop and tell the
user to run `./install.sh` from the pod-sync repo. Do not simulate the tools
with git commands.

## Procedure

**1. Collect context** (read-only, silent — never ask the user for any of this):

```bash
git log --since="midnight" --oneline      # today's commits → summary
git diff --name-status HEAD               # uncommitted changes → files_touched
git stash list                            # stashed WIP → possible blockers
git status --porcelain -- openspec/       # OpenSpec changes to mirror
```

`repo_path` is the absolute path to the current workspace root.

**2. Draft the entry** from git output plus conversation context:

| Field | How to fill it |
|-------|----------------|
| summary | 2-4 sentences, past tense, specific. What was worked on and why it matters. |
| files_touched | File paths from the diff. Can be empty. |
| blockers | Stashed WIP, stuck work, anything the user mentions. "None" if clean. |
| next_up | What picks up tomorrow — specific enough that a teammate could continue it. |

If a field cannot be determined, mark it "not detected" in the draft — do not
fabricate. If `openspec/changes/` folders were created or modified, include
them in the draft as "also mirror OpenSpec change: [folder]".

**3. Confirm** — show the draft and wait:

```
Here's what I'll log:
  Summary:   [...]
  Files:     [...]
  Blockers:  [...]
  Next up:   [...]
  OpenSpec:  [change folders to mirror, or "none"]
Say "go" to submit, or correct anything above.
```

Do not call any tool until the user approves. Re-present after corrections.

**4. Call the tools:**

```
log_status(summary=..., repo_path=..., files_touched=[...], blockers=..., next_up=...)
```

Then, for each confirmed OpenSpec change folder:

```
log_openspec_event(title=..., repo=..., repo_path=...,
                   event_type="proposal_created" | "proposal_updated" | "proposal_archived",
                   openspec_path="openspec/changes/[folder]/", notes=...)
```

`log_openspec_event` copies the documents from the working tree to the logging
branch — they keep living on the user's working branch as normal. OpenSpec's
own workflow creates the documents; Pod-Sync only mirrors them.

**5. Report the result** — relay the tool's message. On success: "Status logged
and pushed."

## Edge cases

| Situation | Action |
|-----------|--------|
| User already logged today | The tool says so. Ask: append (`mode="update"`) or replace (`mode="replace"`)? Call again with their choice. |
| "Just log it" with no detail | Ask for a one-sentence summary minimum. Do not log an empty entry. |
| Auth/SSO push failure | Surface the tool's message; it links to re-authorization. Do not retry silently. |
| "Entry saved locally but push failed" | Tell the user; the next successful log will push it. |
| `git config user.name` not set | Tool returns an error — tell the user to set it. |
| Ambiguous which OpenSpec folder changed | Ask the user which to mirror. Do not guess. |
| User asks to read the log mid-flow | That is /pod-sync-read. Say so and stop. |
