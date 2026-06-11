---
name: pod-sync-openspec
description: "Use this skill when a teammate creates, updates, or archives an OpenSpec
proposal. Triggers: '/opsx:new', '/opsx:continue', '/opsx:archive', 'create a spec
proposal', 'update the proposal', 'archive the spec'. Do NOT use for status logging —
that is pod-sync-update. Do NOT use for reading — that is pod-sync-read. THIS
SKILL WRITES. It calls the log_openspec_event() MCP tool which performs a dual-write:
committing the proposal to a project repo's logging branch and logging an event to
Pod-Sync's entries.json."
---

# Pod-Sync — OpenSpec Skill

> **THIS IS A WRITE-ONLY SKILL.**
> It calls `log_openspec_event()` to commit proposal files to a project repo's `logging`
> branch and log the event to Pod-Sync's `entries.json`.
> It does not read entries. It does not summarize. It does not query.
> If the user wants to READ the log or view proposals, STOP — use `pod-sync-read`.
> If the user wants to LOG a daily status update, STOP — use `pod-sync-update`.

---

## PERMISSION BOUNDARY

```
ALLOWED:
  - Call log_openspec_event() MCP tool
  - Help the user draft proposal content in conversation
  - Present a confirmation preview before calling the tool
  - Determine repo_path from the current workspace root

FORBIDDEN — NEVER DO ANY OF THESE:
  - Writing files to disk directly (the MCP tool handles all file writes)
  - Running git commit, git push, git add, git checkout, git stash (the MCP tool handles all git operations)
  - Reading entries.json to display past entries (that is pod-sync-read)
  - Calling log_status() (that is pod-sync-update)
  - Calling read_status() or read_presence() (that is pod-sync-read)
  - Modifying any file on the filesystem outside of the MCP tool's scope
```

If the user's request crosses this boundary, name the correct skill and stop.

---

## Execution Model — Two Layers

- **This file** defines the protocol: how to gather proposal info, confirm with the user, and call the tool.
- **The MCP tool** handles execution: `log_openspec_event()` does the dual-write (project repo `logging` branch + Pod-Sync `entries.json`).

Never write to disk or run git directly. Always call `log_openspec_event()`.

---

## HARD RULES

1. **Never write to disk directly.** All writes — proposal files, git commits, pushes, entries.json — go through `log_openspec_event()`. You call the tool. That is all.
2. **Never run git commands that modify state.** No `git commit`, `git push`, `git add`, `git checkout`, `git stash`. The MCP tool handles every git operation, including branch switching, stashing, committing, and pushing.
3. **Never read entries to display them.** If the user asks "show me the proposals" mid-conversation, tell them to use `pod-sync-read`. Do not switch roles.
4. **Never invent proposal content.** If the user has not provided or confirmed the content, do not fabricate it. Ask.
5. **Confirm before calling the tool.** Show the user exactly what will be sent to `log_openspec_event()`. Wait for explicit approval.
6. **One operation per call.** Do not batch multiple proposals or multiple event types into one invocation.

---

## Trigger Commands

| Command | Event Type | Meaning |
|---------|-----------|---------|
| `/opsx:new` | `proposal_created` | Create a new OpenSpec proposal |
| `/opsx:continue` | `proposal_updated` | Update an existing proposal |
| `/opsx:archive` | `proposal_archived` | Archive a completed or abandoned proposal |

Natural-language equivalents also trigger this skill: "create a spec proposal", "update the proposal", "archive the spec", etc.

---

## Execution Phases

```
PHASE 1 → Help the user draft the proposal (conversation only)
PHASE 2 → Gather tool parameters from context
PHASE 3 → Confirm with user — show exactly what will be sent
PHASE 4 → Call log_openspec_event() MCP tool
PHASE 5 → Report result
```

Complete each phase before moving to the next. Do not skip phases.

---

## PHASE 1 — Draft the Proposal

> Reminder: NO WRITES IN THIS PHASE. No tool calls. No file writes. No git commands.
> The MCP tool `log_openspec_event()` handles all writes later in Phase 4.

**Goal:** Help the user shape their proposal content in conversation.

Proposals live under `openspec/changes/[folder-name]/` in the project repo. Work with the user to define:

- The folder name (kebab-case, descriptive — e.g. `rate-limit-overhaul`)
- The proposal document content (markdown, structured however the team prefers)

For **`/opsx:new`**: Help draft the full proposal document from scratch.
For **`/opsx:continue`**: Discuss what needs to change and help revise the document.
For **`/opsx:archive`**: Confirm which proposal to archive and whether a final note is needed.

Do not write the proposal to disk yourself. The MCP tool does that.

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

## PHASE 2 — Gather Tool Parameters

> Reminder: STILL READ-ONLY. No tool calls yet. No writes.
> Everything collected here will be sent to `log_openspec_event()` in Phase 4.

**Goal:** Determine all parameters needed for the tool call.

| Parameter | How to determine it |
|-----------|---------------------|
| `title` | Human-readable proposal title. Ask the user or infer from the document. |
| `repo` | The project repo name (e.g. `"dashboard-repo"`). Infer from the workspace or ask. |
| `repo_path` | Absolute filesystem path to the project repo root. Determine from the current workspace root. See "What `repo_path` Means" below. |
| `event_type` | One of: `"proposal_created"`, `"proposal_updated"`, `"proposal_archived"`. Determined by the trigger command. |
| `openspec_path` | Relative path within the repo: `"openspec/changes/[folder-name]/"`. |
| `notes` | Optional one-line summary. Can be empty. |

### What `repo_path` Means

`repo_path` is the **absolute filesystem path to the project repo being worked in** — NOT the Pod-Sync repo.

Example: `"/Users/aahil/comcast/dashboard-repo"`

The agent determines this from the current workspace root. Do not ask the user for this unless the workspace is ambiguous.

### When a parameter cannot be determined

Do not omit it silently. Do not fabricate. Mark it:

```
Not detected — please provide.
```

---

## PHASE 3 — Confirm with User

> Reminder: STILL READ-ONLY. No tool calls yet. No writes.
> This is the last checkpoint before `log_openspec_event()` is called.

**Goal:** Show the user exactly what will be sent to the tool. Wait for approval.

Present this and wait for a response:

```
Here's what I'll send to log_openspec_event():

  Event type:     [proposal_created / proposal_updated / proposal_archived]
  Title:          [proposal title]
  Repo:           [repo name]
  Repo path:      [absolute path]
  OpenSpec path:  openspec/changes/[folder-name]/
  Notes:          [one-line summary, or "none"]

The tool will:
  - Switch to the logging branch in [repo name]
  - Commit and push the proposal there
  - Log the event to Pod-Sync entries.json
  - Push to Pod-Sync repo

Say "go" to submit, or correct anything above.
```

**Do not call `log_openspec_event()` until the user confirms.**

If the user corrects something, update the parameters and re-present before proceeding.

---

## PHASE 4 — Call the MCP Tool

> **THIS IS THE ONLY PHASE THAT WRITES.** Everything goes through one tool call: `log_openspec_event()`.

Call `log_openspec_event()` with the confirmed parameters:

```
log_openspec_event(
    title="[proposal title]",
    repo="[repo name]",
    repo_path="[absolute path]",
    event_type="[proposal_created | proposal_updated | proposal_archived]",
    openspec_path="openspec/changes/[folder-name]/",
    notes="[one-line summary or empty]"
)
```

The MCP tool handles everything from here:
- Stashes any uncommitted changes in the project repo if needed
- Switches to the `logging` branch (creates it if it does not exist)
- Writes and commits the proposal files on the `logging` branch
- Pushes to the project repo's remote
- Switches back to the original branch and unstashes
- Writes the event to Pod-Sync's `entries.json`
- Commits and pushes to the Pod-Sync repo

**You do not run git add, git commit, git push, git checkout, or git stash. The tool does.**

---

## PHASE 5 — Report Result

> Reminder: BACK TO READ-ONLY. This phase only reports what the tool returned.

If the tool returns success, confirm to the user:

```
OpenSpec [event_type] logged.
  Proposal committed to [repo]/logging branch.
  Event recorded in Pod-Sync entries.json.
```

If the tool returns an error, handle it:

| Error | Action |
|-------|--------|
| Auth/SSH/SSO error | Tell the user to re-authorize. Link: https://github.com/settings/ssh |
| Push to logging branch failed | Surface the error clearly. Do not write to Pod-Sync entries.json — the tool ensures the project repo write succeeds first. |
| Merge conflict on logging branch | Surface the error. The user may need to resolve manually. |
| Any other error | Surface the tool's error message verbatim. Do not retry without user input. |

---

## Edge Cases

| Situation | Action |
|-----------|--------|
| `logging` branch does not exist | The MCP tool creates it automatically. No action needed from you. |
| Uncommitted changes in project repo | The MCP tool stashes before switching, unstashes after. No action needed from you. |
| User is already on `logging` branch | The tool proceeds normally — no branch switch needed. |
| Push to logging branch fails | Report the error clearly. The tool will NOT write to Pod-Sync entries.json if the project repo write fails. |
| User asks to see existing proposals | That is a READ operation. Tell them to use `pod-sync-read`. Do not read entries.json or the filesystem yourself. |
| User asks to log a daily status | That is a STATUS operation. Tell them to use `pod-sync-update`. Do not call `log_status()`. |
| User says "just archive it" with no context | Ask which proposal to archive. Do not guess. |
| `repo_path` cannot be determined | Ask the user for the absolute path to their project repo. Do not fabricate a path. |

---

## What This Skill Does NOT Do

These are explicit exclusions. If you find yourself doing any of these, you are in the wrong skill.

- Read or display past entries or proposals (use `pod-sync-read`)
- Summarize what the team did (use `pod-sync-read`)
- Check who is active / online (use `pod-sync-read` with `read_presence()`)
- Log a daily status update (use `pod-sync-update`)
- Write directly to `entries.json` or any file on disk
- Write proposal files to the filesystem directly
- Run `git commit`, `git push`, `git add`, `git checkout`, `git stash`, or any git write command
- Modify the filesystem in any way outside of calling `log_openspec_event()`
