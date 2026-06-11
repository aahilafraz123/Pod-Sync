---
name: pod-sync-update
description: "Use this skill ONLY when a team member explicitly wants to WRITE or LOG
a new status entry. Triggers: 'log my work', 'write my status', 'end of day update',
'log what I did today', 'add my entry', 'daily standup log'. Do NOT use for reading,
summarizing, or checking what others did — that is pod-sync-read. Do NOT use for
OpenSpec proposals — that is pod-sync-openspec. THIS SKILL WRITES. It calls the
log_status() MCP tool which writes to entries.json and pushes to git."
---

# Pod-Sync — Status Update Skill

> **THIS IS A WRITE-ONLY SKILL.**
> It calls `log_status()` to append a structured entry to `entries.json` and push it.
> It does not read entries. It does not summarize. It does not query.
> If the user wants to READ the log, STOP — use `pod-sync-read` instead.

---

## PERMISSION BOUNDARY

```
ALLOWED:
  - Call log_status() MCP tool
  - Run git commands to collect context (read-only git operations)
  - Present a draft to the user for confirmation

FORBIDDEN:
  - Reading entries.json to display past entries (that is pod-sync-read)
  - Summarizing or presenting other people's work
  - Writing directly to entries.json or any file (the MCP tool handles all writes)
  - Running git commit, git push, git add (the MCP tool handles all git writes)
  - Touching any file outside the MCP tool's scope
```

If the user's request crosses this boundary, tell them which skill to use and stop.

---

## HARD RULES

1. **Never write to disk directly.** All writes go through `log_status()`. The MCP tool handles `entries.json`, archival, git commit, and git push. You call the tool. That is all.
2. **Never read entries to display them.** If the user asks "what did I log yesterday?" mid-conversation, tell them to use `pod-sync-read`. Do not switch roles.
3. **Never ask the user for their name.** Author is auto-detected by `log_status()` from `git config user.name`. If git config is not set, the MCP tool will return an error — surface it.
4. **Never invent information.** If a field cannot be determined from git or conversation context, leave it empty or mark it unknown. Do not guess.
5. **Confirm before calling the tool.** Show the user what will be logged. Wait for explicit approval.
6. **One entry per call.** Do not batch multiple days or multiple authors into one invocation.

---

## Execution Phases

```
PHASE 1 → Collect context (git data + conversation)
PHASE 2 → Draft the entry and confirm with user
PHASE 3 → Call log_status() MCP tool
PHASE 4 → Report result
```

Complete each phase before moving to the next. Do not skip phases.

---

## PHASE 1 — Collect Context

> Reminder: THIS PHASE IS READ-ONLY COLLECTION. No writes happen here.

**Goal:** Gather field data from the environment. The agent reads this from git automatically — the user should not need to type answers to a form.

Run these to collect context:

```bash
git config user.name                          # Author (used by MCP tool, not by you)
git log --since="midnight" --oneline          # Work done today
git diff --name-status HEAD                   # Uncommitted changes
git diff --name-status origin/HEAD..HEAD      # Committed changes today
git branch --show-current                     # Current branch
```

### Field extraction

| Field | How to determine it |
|-------|---------------------|
| summary | Synthesize from commit messages + diff + conversation. 2-4 sentences, past tense, specific. |
| files_touched | From git diff output. List of file paths modified today. Can be empty. |
| blockers | Stash entries, uncommitted WIP, TODO/FIXME in touched files, or anything the user mentions. "None" if clean. |
| next_up | From conversation context, open TODOs, or user's stated plans. Specific enough a teammate could continue it. |

### When a field cannot be determined

Do not omit it silently. Do not fabricate. Mark it:

```
Not detected — fill in if needed, or leave empty.
```

---

## Repo context — collect this silently before calling any tool

Before calling any MCP tool, run these git commands in the current workspace
and use the output to populate the call. Never ask the user for any of this.

1. `git remote get-url origin`     → determines which repo this is
2. `git branch --show-current`     → current branch (for context only, not logged)
3. `git log --since="midnight" --oneline`  → today's commits, use to help write summary
4. `git diff --name-status HEAD`   → files touched today, use for files_touched param

Pass `repo_path` as the absolute path to the current workspace root.
The MCP tool handles branch switching, reading, writing, and pushing.
Never switch branches manually. Never write to entries.json directly.

---

## PHASE 2 — Draft and Confirm

> Reminder: THIS PHASE IS STILL READ-ONLY. No tool calls yet. No writes.

**Goal:** Present the collected data to the user for approval before anything is written.

Show this and wait for a response:

```
Here's what I'll log:

  Summary:   [synthesized from git + conversation]
  Files:     [list, or "none detected"]
  Blockers:  [detected, or "None"]
  Next up:   [detected, or "not specified"]

Say "go" to submit, or correct anything above.
```

**Do not call `log_status()` until the user confirms.**

If the user corrects something, update the draft and re-present it before proceeding.

---

## PHASE 3 — Call the MCP Tool

> THIS IS THE ONLY PHASE THAT WRITES. Everything goes through one tool call.

Call `log_status()` with the confirmed fields:

```
log_status(
    summary="[confirmed summary]",
    repo_path="[absolute path to workspace root]",
    files_touched=["path/to/file1", "path/to/file2"],
    blockers="[confirmed blockers or 'None']",
    next_up="[confirmed next_up]"
)
```

The MCP tool handles everything from here:
- Auto-detects author from `git config user.name`
- Generates entry ID, date, week, timestamp
- Checks for duplicate entry today (returns prompt to append or replace)
- Pulls latest from remote
- Appends to `entries.json` via atomic write
- Runs `archive_old_entries()` to maintain the 90-day rolling window
- Commits and pushes to the Pod-Sync repo

**You do not run git add, git commit, or git push. The tool does.**

---

## PHASE 4 — Report Result

> Reminder: BACK TO READ-ONLY. This phase only reports what the tool returned.

If the tool returns success, confirm to the user:

```
Status logged and pushed.
```

If the tool returns an error:
- **Auth/SSO error:** Tell the user to re-authorize their SSH key or PAT. Link: https://github.com/settings/ssh
- **Duplicate entry for today:** The tool will ask whether to append or replace. Present the question to the user and call the tool again with their choice.
- **Any other error:** Surface the tool's error message. Do not retry without the user's input.

---

## Edge Cases

| Situation | Action |
|-----------|--------|
| User already logged today | The MCP tool detects this. Ask the user: append as mid-day update, or replace the earlier entry? |
| User says "just log it" with no detail | Ask for a one-sentence summary minimum. Do not log an empty entry. |
| User asks to see their previous entry | That is a READ operation. Tell them to use `pod-sync-read`. Do not read entries.json yourself. |
| Push fails with SSO/auth error | Surface the error. Link to https://github.com/settings/ssh. Do not retry silently. |
| Git config user.name is not set | The MCP tool will return an error. Tell the user to run `git config --global user.name "Their Name"`. |

---

## What This Skill Does NOT Do

These are explicit exclusions. If you find yourself doing any of these, you are in the wrong skill.

- Read or display past entries (use `pod-sync-read`)
- Summarize what the team did (use `pod-sync-read`)
- Check who is active / online (use `pod-sync-read` with `read_presence()`)
- Create or update OpenSpec proposals (use `pod-sync-openspec`)
- Write directly to `entries.json` or any file on disk
- Run `git commit`, `git push`, `git add` directly
