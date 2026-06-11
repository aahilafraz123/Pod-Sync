#!/usr/bin/env python3
"""
Pod-Sync MCP Server + Local HTTP API

Two modes:
  python server.py --stdio   → MCP stdio mode (spawned by IDE)
  python server.py --http    → HTTP mode (web UI + API on localhost:7823)

Both modes share identical tool logic.

Data architecture: all data (entries.json, presence.json, archive/) lives on
the `logging` branch of each project repo — NOT in the pod-sync repo. The
pod-sync repo is purely a tool: installer, server, skills.
"""

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from uuid import uuid4

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).parent.resolve()
WEB_UI_PATH = REPO_ROOT / "web-ui" / "index.html"
SKILLS_DIR = REPO_ROOT / "skills"
CONFIG_PATH = pathlib.Path.home() / ".config" / "pod-sync" / "config.json"
PORT = 7823
HEARTBEAT_INTERVAL = 600  # 10 minutes

# ---------------------------------------------------------------------------
# Helpers — file I/O
# ---------------------------------------------------------------------------

def atomic_write(path: pathlib.Path, data):
    """Write JSON atomically — tmp file then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def load_entries(repo_path: pathlib.Path, include_archive=False, weeks=None):
    """Load entries from a project repo's logging branch.
    Assumes we are already on the logging branch."""
    entries_path = repo_path / "entries.json"
    archive_dir = repo_path / "archive"
    entries = json.loads(entries_path.read_text()) if entries_path.exists() else []
    if include_archive and weeks:
        for week in weeks:
            ap = archive_dir / f"{week}.json"
            if ap.exists():
                entries += json.loads(ap.read_text())
    return entries


def archive_old_entries(repo_path: pathlib.Path):
    """Move entries older than 90 days into weekly archive files.
    Operates on a project repo's logging branch. No-op if nothing qualifies."""
    try:
        entries_path = repo_path / "entries.json"
        archive_dir = repo_path / "archive"
        cutoff = datetime.now() - timedelta(days=90)
        entries = json.loads(entries_path.read_text()) if entries_path.exists() else []

        to_keep = [e for e in entries if _parse_date(e.get("date", "")) >= cutoff]
        to_archive = [e for e in entries if _parse_date(e.get("date", "")) < cutoff]

        if not to_archive:
            return

        by_week = {}
        for entry in to_archive:
            week = datetime.strptime(entry["date"], "%Y-%m-%d").strftime("%G-W%V")
            by_week.setdefault(week, []).append(entry)

        for week, week_entries in by_week.items():
            archive_path = archive_dir / f"{week}.json"
            existing = json.loads(archive_path.read_text()) if archive_path.exists() else []
            atomic_write(archive_path, existing + week_entries)

        atomic_write(entries_path, to_keep)
    except Exception as e:
        print(f"[archive] warning: {e}", file=sys.stderr)

# ---------------------------------------------------------------------------
# Helpers — date/time
# ---------------------------------------------------------------------------

def _parse_date(date_str):
    """Parse YYYY-MM-DD to datetime. Returns epoch on failure."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return datetime.min


def today_iso():
    return datetime.now().strftime("%Y-%m-%d")


def iso_week():
    return datetime.now().strftime("%G-W%V")


def now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

# ---------------------------------------------------------------------------
# Helpers — config (registered repos)
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"registered_repos": []}


def register_repo(repo_path: str):
    config = load_config()
    if repo_path not in config["registered_repos"]:
        config["registered_repos"].append(repo_path)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(CONFIG_PATH, config)

# ---------------------------------------------------------------------------
# Helpers — git
# ---------------------------------------------------------------------------

def git_config_username():
    """Read git config user.name. Returns 'Unknown' if not set."""
    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True, text=True, cwd=REPO_ROOT
        )
        name = result.stdout.strip()
        return name if name else "Unknown"
    except Exception:
        return "Unknown"


def _git_run(args, cwd=None, check=False):
    """Run a git command, return CompletedProcess."""
    return subprocess.run(
        ["git"] + args,
        cwd=cwd or REPO_ROOT,
        capture_output=True, text=True,
        check=check
    )


def _git_pull_branch(branch="main", cwd=None):
    """Pull latest from origin for a specific branch. Returns (success, error_msg)."""
    result = _git_run(["pull", "--rebase", "origin", branch], cwd=cwd)
    if result.returncode != 0:
        return False, _classify_git_error(result.stderr)
    return True, ""


def _git_push(branch="main", cwd=None):
    """Push to origin. Returns (success, error_msg)."""
    result = _git_run(["push", "origin", branch], cwd=cwd)
    if result.returncode != 0:
        return False, _classify_git_error(result.stderr)
    return True, ""


def _git_commit_and_push(message, paths=None, branch="logging", cwd=None, retries=2):
    """Stage paths, commit, push. Retries with pull-rebase on push failure."""
    target_cwd = cwd
    if paths:
        for p in paths:
            _git_run(["add", str(p)], cwd=target_cwd)
    else:
        _git_run(["add", "-A"], cwd=target_cwd)

    result = _git_run(["commit", "-m", message], cwd=target_cwd)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "nothing to commit" in stderr or "nothing to commit" in result.stdout:
            return True, ""
        return False, f"Commit failed: {stderr}"

    for attempt in range(retries):
        ok, err = _git_push(branch, cwd=target_cwd)
        if ok:
            return True, ""
        # Push rejected — pull rebase and retry
        pull_ok, pull_err = _git_pull_branch(branch, cwd=target_cwd)
        if not pull_ok:
            return False, f"Push failed and pull-rebase also failed: {pull_err}"

    return False, f"Push failed after {retries} attempts: {err}"


def _get_git_host():
    """Parse the git host from the origin remote URL.
    Handles both SSH (git@host:...) and HTTPS (https://host/...) formats."""
    try:
        url = _git_run(["remote", "get-url", "origin"]).stdout.strip()
        if url.startswith("git@"):
            return url.split("@")[1].split(":")[0]
        elif "://" in url:
            from urllib.parse import urlparse
            return urlparse(url).hostname or "github.com"
    except Exception:
        pass
    return "github.com"


def get_repo_name_from_path(repo_path: pathlib.Path) -> str:
    """Parse the repo name from the git remote URL of a project repo."""
    try:
        url = _git_run(["remote", "get-url", "origin"], cwd=repo_path).stdout.strip()
        # Remove trailing .git suffix properly (not rstrip which strips chars)
        if url.endswith(".git"):
            url = url[:-4]
        return url.split("/")[-1].split(":")[-1]
    except Exception:
        return repo_path.name


def _classify_git_error(stderr):
    """Inspect stderr for auth/SSO signals and return actionable message."""
    host = _get_git_host()
    lower = stderr.lower()
    for signal in ["saml", "sso", "authentication", "403", "permission denied"]:
        if signal in lower:
            return (
                f"Git authentication failed. Re-authorize your credentials:\n"
                f"  SSH: https://{host}/settings/ssh\n"
                f"  PAT: https://{host}/settings/tokens\n"
                f"Raw error: {stderr.strip()}"
            )
    return stderr.strip()


def ensure_logging_branch(repo_path: pathlib.Path):
    """Ensure the 'logging' branch exists and is checked out in a project repo.
    Stashes uncommitted changes, switches to logging, and returns context
    that restore_original_branch() uses to switch back."""
    original_branch = _git_run(["branch", "--show-current"], cwd=repo_path).stdout.strip()

    # Already on logging — nothing to do
    if original_branch == "logging":
        return {"repo_path": repo_path, "original_branch": None, "had_stash": False}

    # Check for uncommitted changes
    status = _git_run(["status", "--porcelain"], cwd=repo_path)
    has_changes = bool(status.stdout.strip())

    if has_changes:
        _git_run(["stash", "push", "-m", "pod-sync-auto-stash"], cwd=repo_path)

    branches = _git_run(["branch", "--list", "logging"], cwd=repo_path).stdout.strip()

    try:
        if "logging" not in branches:
            _git_run(["checkout", "-b", "logging"], cwd=repo_path, check=True)
        else:
            _git_run(["checkout", "logging"], cwd=repo_path, check=True)
    except subprocess.CalledProcessError as e:
        # Failed to switch — restore stash on current branch and raise
        if has_changes:
            _git_run(["stash", "pop"], cwd=repo_path)
        raise RuntimeError(f"Failed to switch to logging branch: {e.stderr}")

    return {"repo_path": repo_path, "original_branch": original_branch, "had_stash": has_changes}


def restore_original_branch(ctx):
    """Switch back to the original branch after a logging branch operation.
    Pops the stash on the original branch (not on logging)."""
    repo_path = ctx["repo_path"]
    original = ctx["original_branch"]
    had_stash = ctx["had_stash"]

    if original is None:
        return

    try:
        _git_run(["checkout", original], cwd=repo_path, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[branch-restore] warning: could not switch back to {original}: {e.stderr}", file=sys.stderr)
        return

    if had_stash:
        _git_run(["stash", "pop"], cwd=repo_path)

# ---------------------------------------------------------------------------
# Helpers — heartbeat (writes to each registered project repo's logging branch)
# ---------------------------------------------------------------------------

def _write_heartbeat_to_repo(repo_path_str: str):
    """Write heartbeat to a single project repo's logging branch. Fails silently."""
    try:
        repo = pathlib.Path(repo_path_str)
        if not repo.exists():
            return

        branch_ctx = ensure_logging_branch(repo)
        try:
            # Pull latest logging branch
            _git_pull_branch("logging", cwd=repo)

            presence_path = repo / "heartbeat" / "presence.json"
            presence_path.parent.mkdir(parents=True, exist_ok=True)
            presence = json.loads(presence_path.read_text()) if presence_path.exists() else {}

            author = git_config_username()

            presence[author] = {
                "last_seen": now_iso(),
                "branch": branch_ctx.get("original_branch") or "logging",
            }
            atomic_write(presence_path, presence)

            # Commit and push with retry logic
            _git_commit_and_push(
                f"heartbeat: {author} — {now_iso()}",
                paths=["heartbeat/"],
                branch="logging",
                cwd=repo
            )
        finally:
            restore_original_branch(branch_ctx)
    except Exception as e:
        print(f"[heartbeat] warning ({repo_path_str}): {e}", file=sys.stderr)


def _heartbeat_loop():
    """Background thread: update heartbeat in all registered repos every 10 minutes."""
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        config = load_config()
        for repo_path in config.get("registered_repos", []):
            _write_heartbeat_to_repo(repo_path)

# ---------------------------------------------------------------------------
# CLI command — install-skills
# ---------------------------------------------------------------------------

def cmd_install_skills(target=None):
    """
    CLI handler for: pod-sync install-skills [.]

    Detects the user's IDE and copies Pod-Sync skill files into the
    appropriate skills directory.

    target=None  → global install, skills available from every repo in that IDE
    target="."   → local install, skills scoped to the current working directory only

    IDE detection order: windsurf → vscode → opencode
    Uses shutil.which() — same cross-platform approach as api_detect_ides.
    """

    # Detect IDE
    ide = None
    if shutil.which("windsurf"):
        ide = "windsurf"
    elif shutil.which("code"):
        ide = "vscode"
    elif shutil.which("opencode"):
        ide = "opencode"

    if target == ".":
        cwd = pathlib.Path.cwd()
        if ide == "windsurf":
            dest = cwd / ".windsurf" / "rules"
        elif ide == "vscode":
            dest = cwd / ".github"
        elif ide == "opencode":
            dest = cwd / ".opencode" / "skills"
        else:
            dest = cwd / ".skills"
        install_scope = f"local ({cwd.name})"
    else:
        if ide == "windsurf":
            dest = pathlib.Path.home() / ".codeium" / "windsurf" / "memories"
        elif ide == "vscode":
            dest = pathlib.Path.home() / ".vscode" / "skills"
        elif ide == "opencode":
            dest = pathlib.Path.home() / ".config" / "opencode" / "skills"
        else:
            print("No supported IDE detected (windsurf, code, opencode).")
            print("Copy the files in skills/ manually to your IDE's skills directory.")
            sys.exit(1)
        install_scope = "global"

    dest.mkdir(parents=True, exist_ok=True)

    installed = []
    for skill_file in SKILLS_DIR.glob("*.md"):
        shutil.copy2(skill_file, dest / skill_file.name)
        installed.append(skill_file.name)

    if not installed:
        print("No skill files found in skills/. Is SKILLS_DIR correct?")
        sys.exit(1)

    print()
    print(f"  Installed {len(installed)} Pod-Sync skills")
    print(f"     IDE:   {ide or 'unknown'}")
    print(f"     Scope: {install_scope}")
    print(f"     Path:  {dest}")
    print()
    for f in installed:
        print(f"     - {f}")
    print()
    if target == ".":
        print("  Skills are active for this repo only.")
        print("  Run without '.' to install globally across all repos.")
        print()
        print(f"  Note: consider adding {dest.name}/pod-sync-*.md to your .gitignore")
        print("  if you don't want to commit these to the project repo.")
    else:
        print("  Skills are now active in every project you open in this IDE.")
        print("  Run 'pod-sync install-skills .' from any project repo to scope them locally instead.")
    print()

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def startup():
    """Run once at server start in both modes.
    No data files to create in the pod-sync repo — data lives in project repos."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Initial heartbeat + loop in background
    def _initial_push_and_loop():
        config = load_config()
        for repo_path in config.get("registered_repos", []):
            _write_heartbeat_to_repo(repo_path)
        _heartbeat_loop()

    t = threading.Thread(target=_initial_push_and_loop, daemon=True)
    t.start()

# ---------------------------------------------------------------------------
# Tool logic — shared by MCP and HTTP modes
# All data operations target the project repo's logging branch.
# ---------------------------------------------------------------------------

def tool_log_status(summary: str, repo_path: str, files_touched: list = None, blockers: str = "None", next_up: str = "") -> str:
    """Log an end-of-day status entry to the project repo's logging branch."""
    if files_touched is None:
        files_touched = []

    author = git_config_username()
    if author == "Unknown":
        return "Error: git config user.name is not set. Run: git config --global user.name \"Your Name\""

    project = pathlib.Path(repo_path)
    if not project.exists():
        return f"Error: repo_path does not exist: {repo_path}"

    repo_name = get_repo_name_from_path(project)
    date = today_iso()

    # Detect replace/update prefix BEFORE the duplicate check
    replace_mode = False
    update_mode = False
    if summary.startswith("[replace] "):
        summary = summary[len("[replace] "):]
        replace_mode = True
    elif summary.startswith("[update] "):
        summary = summary[len("[update] "):]
        update_mode = True

    # Switch to logging branch
    try:
        branch_ctx = ensure_logging_branch(project)
    except RuntimeError as e:
        return f"Error switching to logging branch: {e}"

    try:
        # Pull fresh data
        _git_pull_branch("logging", cwd=project)

        # Ensure data files exist on logging branch
        entries_path = project / "entries.json"
        archive_dir = project / "archive"
        entries_path.parent.mkdir(parents=True, exist_ok=True)
        archive_dir.mkdir(parents=True, exist_ok=True)
        if not entries_path.exists():
            atomic_write(entries_path, [])

        # Check for duplicate entry today — skip when a directive is present
        if not replace_mode and not update_mode:
            entries = load_entries(project)
            existing_today = [e for e in entries if e.get("author") == author and e.get("date") == date and e.get("type") == "status"]
            if existing_today:
                restore_original_branch(branch_ctx)
                return (
                    f"You already have a status entry for today ({date}). "
                    f"Would you like to:\n"
                    f"  1. Append as a mid-day update (call again with summary prefixed with '[update] ')\n"
                    f"  2. Replace the existing entry (call again with summary prefixed with '[replace] ')\n"
                    f"Do not overwrite silently."
                )

        entry = {
            "id": str(uuid4()),
            "type": "status",
            "author": author,
            "repo": repo_name,
            "date": date,
            "week": iso_week(),
            "timestamp": now_iso(),
            "summary": summary,
            "files_touched": files_touched,
            "blockers": blockers,
            "next_up": next_up,
            "archived": False,
        }

        entries = load_entries(project)

        # Remove today's entry if replacing
        if replace_mode:
            entries = [e for e in entries if not (e.get("author") == author and e.get("date") == date and e.get("type") == "status")]

        entries.append(entry)
        atomic_write(entries_path, entries)
        archive_old_entries(project)

        ok, err = _git_commit_and_push(
            f"status: {author} — {date}",
            paths=["entries.json", "archive/"],
            branch="logging",
            cwd=project
        )

        restore_original_branch(branch_ctx)

        if not ok:
            return f"Entry saved locally but push failed: {err}"

        # Auto-register this repo
        register_repo(str(project))

        return f"Logged and pushed to {repo_name}/logging. ({author}, {date})"

    except Exception as e:
        restore_original_branch(branch_ctx)
        return f"Error logging status: {e}"


def tool_read_status(repo_path: str, who: str = "all", when: str = "latest", query: str = "") -> str:
    """Read status and OpenSpec entries from a project repo's logging branch."""
    project = pathlib.Path(repo_path)
    if not project.exists():
        return f"Error: repo_path does not exist: {repo_path}"

    try:
        branch_ctx = ensure_logging_branch(project)
    except RuntimeError as e:
        return f"Error switching to logging branch: {e}"

    try:
        _git_pull_branch("logging", cwd=project)

        # Ensure entries.json exists
        entries_path = project / "entries.json"
        if not entries_path.exists():
            restore_original_branch(branch_ctx)
            return "No entries found — this repo has no status log yet."

        # Determine date range and whether archive is needed
        now = datetime.now()
        date_start = None
        date_end = None
        need_archive = False
        archive_weeks = []

        if when == "today":
            date_start = date_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif when == "yesterday":
            yesterday = now - timedelta(days=1)
            date_start = date_end = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        elif when == "this week":
            date_start = now - timedelta(days=now.weekday())
            date_start = date_start.replace(hour=0, minute=0, second=0, microsecond=0)
            date_end = now
        elif when == "last week":
            date_start = now - timedelta(days=now.weekday() + 7)
            date_start = date_start.replace(hour=0, minute=0, second=0, microsecond=0)
            date_end = date_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
        elif ":" in when:
            parts = when.split(":")
            try:
                date_start = datetime.strptime(parts[0], "%Y-%m-%d")
                date_end = datetime.strptime(parts[1], "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            except (ValueError, IndexError):
                restore_original_branch(branch_ctx)
                return f"Invalid date range format: {when}. Use YYYY-MM-DD:YYYY-MM-DD."
        elif when != "latest":
            try:
                date_start = datetime.strptime(when, "%Y-%m-%d")
                date_end = date_start.replace(hour=23, minute=59, second=59)
            except ValueError:
                restore_original_branch(branch_ctx)
                return f"Invalid date format: {when}. Use YYYY-MM-DD."

        # Check if we need archive data
        if date_start:
            cutoff = now - timedelta(days=90)
            if date_start < cutoff:
                need_archive = True
                end = date_end or now
                seen_weeks = set()
                d = date_start
                while d <= end:
                    w = d.strftime("%G-W%V")
                    if w not in seen_weeks:
                        seen_weeks.add(w)
                        archive_weeks.append(w)
                    d += timedelta(days=1)

        entries = load_entries(project, include_archive=need_archive, weeks=archive_weeks)

        restore_original_branch(branch_ctx)

        # Filter by author
        if who != "all":
            entries = [e for e in entries if who.lower() in e.get("author", "").lower()]

        # Filter by date
        if when == "latest":
            by_author = {}
            for e in sorted(entries, key=lambda x: x.get("timestamp", ""), reverse=True):
                a = e.get("author", "Unknown")
                if a not in by_author:
                    by_author[a] = e
            entries = list(by_author.values())
        elif date_start:
            entries = [
                e for e in entries
                if date_start <= _parse_date(e.get("date", "")) <= (date_end or now)
            ]

        # Keyword filter
        if query:
            q = query.lower()
            entries = [
                e for e in entries
                if q in e.get("summary", "").lower()
                or q in e.get("title", "").lower()
                or q in " ".join(e.get("files_touched", [])).lower()
            ]

        if not entries:
            return "No entries found for that query."

        entries.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

        lines = []
        for e in entries:
            if e.get("type") == "status":
                lines.append(
                    f"{e.get('author', 'Unknown')}  ({e.get('date', '')})\n"
                    f"  {e.get('summary', '')}\n"
                    f"  Blockers: {e.get('blockers', 'None')}\n"
                    f"  Next up: {e.get('next_up', '')}"
                )
            elif e.get("type") == "openspec_proposal":
                lines.append(
                    f"{e.get('author', 'Unknown')}  ({e.get('date', '')})  [openspec]\n"
                    f"  {e.get('title', 'Untitled')}\n"
                    f"  Repo: {e.get('repo', '')} / {e.get('branch', 'logging')}\n"
                    f"  Event: {e.get('event_type', '')}"
                )
            lines.append("")

        return "\n".join(lines).strip()

    except Exception as e:
        try:
            restore_original_branch(branch_ctx)
        except Exception:
            pass
        return f"Error reading status: {e}"


def tool_log_openspec_event(
    title: str,
    repo: str,
    repo_path: str,
    event_type: str = "proposal_created",
    openspec_path: str = "openspec/changes/",
    notes: str = "",
) -> str:
    """Commit proposal to project repo logging branch and log event to entries.json on same branch."""
    author = git_config_username()
    if author == "Unknown":
        return "Error: git config user.name is not set. Run: git config --global user.name \"Your Name\""

    project = pathlib.Path(repo_path)
    if not project.exists():
        return f"Error: repo_path does not exist: {repo_path}"

    # Switch to logging branch
    try:
        branch_ctx = ensure_logging_branch(project)
    except RuntimeError as e:
        return f"Error switching to logging branch: {e}"

    try:
        # Pull latest
        _git_pull_branch("logging", cwd=project)

        # Stage and commit proposal files
        result = _git_run(["add", openspec_path], cwd=project)

        # Also ensure entries.json exists
        entries_path = project / "entries.json"
        if not entries_path.exists():
            atomic_write(entries_path, [])

        # Build event and append to entries.json (same branch, single write)
        event = {
            "id": str(uuid4()),
            "type": "openspec_proposal",
            "author": author,
            "date": today_iso(),
            "week": iso_week(),
            "timestamp": now_iso(),
            "title": title,
            "repo": repo,
            "branch": "logging",
            "path": openspec_path,
            "event_type": event_type,
            "notes": notes,
            "archived": False,
        }

        entries = load_entries(project)
        entries.append(event)
        atomic_write(entries_path, entries)

        # Commit everything together
        _git_run(["add", openspec_path, "entries.json"], cwd=project)
        result = _git_run(
            ["commit", "-m", f"openspec: {title} ({event_type})"],
            cwd=project
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            if "nothing to commit" not in stderr and "nothing to commit" not in stdout:
                restore_original_branch(branch_ctx)
                return f"Error committing to project repo: {stderr}"

        result = _git_run(["push", "origin", "logging"], cwd=project)
        if result.returncode != 0:
            restore_original_branch(branch_ctx)
            return f"Error pushing to project repo logging branch: {_classify_git_error(result.stderr)}"

        restore_original_branch(branch_ctx)

        # Auto-register this repo
        register_repo(str(project))

        return (
            f"OpenSpec event logged. "
            f"Proposal committed to {repo}/logging. "
            f"Event visible in team dashboard."
        )

    except Exception as e:
        try:
            restore_original_branch(branch_ctx)
        except Exception:
            pass
        return f"Error logging openspec event: {e}"


def tool_read_presence(repo_path: str) -> str:
    """Show who on the team is currently active in a specific project repo."""
    project = pathlib.Path(repo_path)
    if not project.exists():
        return f"Error: repo_path does not exist: {repo_path}"

    try:
        branch_ctx = ensure_logging_branch(project)
    except RuntimeError as e:
        return f"Error switching to logging branch: {e}"

    try:
        _git_pull_branch("logging", cwd=project)

        presence_path = project / "heartbeat" / "presence.json"
        presence = json.loads(presence_path.read_text()) if presence_path.exists() else {}

        restore_original_branch(branch_ctx)

        if not presence:
            return "No presence data found. No teammates have started Pod-Sync for this repo yet."

        now = datetime.now()
        active = []
        away = []

        for name, info in presence.items():
            try:
                last_seen = datetime.strptime(info["last_seen"], "%Y-%m-%dT%H:%M:%S")
                delta = now - last_seen
                minutes = int(delta.total_seconds() / 60)
            except (ValueError, KeyError):
                minutes = 9999

            branch = info.get("branch", "unknown")

            if minutes <= 30:
                if minutes < 1:
                    time_str = "just now"
                else:
                    time_str = f"{minutes} min ago"
                active.append(f"  {name}  — last seen {time_str}  — {branch}")
            else:
                if minutes < 60:
                    time_str = f"{minutes} min ago"
                elif minutes < 1440:
                    time_str = f"{minutes // 60}h ago"
                else:
                    time_str = f"{minutes // 1440}d ago"
                away.append(f"  {name}  — last seen {time_str}")

        lines = ["Active now:"]
        lines.extend(active if active else ["  (nobody)"])
        lines.append("")
        lines.append("Away:")
        lines.extend(away if away else ["  (nobody)"])

        return "\n".join(lines)

    except Exception as e:
        try:
            restore_original_branch(branch_ctx)
        except Exception:
            pass
        return f"Error reading presence: {e}"


# ---------------------------------------------------------------------------
# Helper — read repo data for the web UI (switches branch, reads, restores)
# ---------------------------------------------------------------------------

def _read_repo_entries(repo_path_str: str) -> list:
    """Read entries.json from a project repo's logging branch. Returns list."""
    try:
        project = pathlib.Path(repo_path_str)
        if not project.exists():
            return []
        branch_ctx = ensure_logging_branch(project)
        try:
            _git_pull_branch("logging", cwd=project)
            entries_path = project / "entries.json"
            return json.loads(entries_path.read_text()) if entries_path.exists() else []
        finally:
            restore_original_branch(branch_ctx)
    except Exception as e:
        print(f"[read-entries] warning ({repo_path_str}): {e}", file=sys.stderr)
        return []


def _read_repo_presence(repo_path_str: str) -> dict:
    """Read presence.json from a project repo's logging branch. Returns dict."""
    try:
        project = pathlib.Path(repo_path_str)
        if not project.exists():
            return {}
        branch_ctx = ensure_logging_branch(project)
        try:
            _git_pull_branch("logging", cwd=project)
            presence_path = project / "heartbeat" / "presence.json"
            return json.loads(presence_path.read_text()) if presence_path.exists() else {}
        finally:
            restore_original_branch(branch_ctx)
    except Exception as e:
        print(f"[read-presence] warning ({repo_path_str}): {e}", file=sys.stderr)
        return {}


def _build_repo_summary(repo_path_str: str) -> dict:
    """Build summary data for a single repo (used by /api/repos)."""
    project = pathlib.Path(repo_path_str)
    name = get_repo_name_from_path(project)
    entries = _read_repo_entries(repo_path_str)
    presence = _read_repo_presence(repo_path_str)

    today = today_iso()
    now = datetime.now()

    # Count who logged today
    logged_today = set()
    for e in entries:
        if e.get("date") == today and e.get("type") == "status":
            logged_today.add(e.get("author", ""))

    # Count active members
    active_count = 0
    members = set()
    for member_name, info in presence.items():
        members.add(member_name)
        try:
            last_seen = datetime.strptime(info["last_seen"], "%Y-%m-%dT%H:%M:%S")
            if (now - last_seen).total_seconds() <= 1800:
                active_count += 1
        except (ValueError, KeyError):
            pass

    # Also add authors from entries as members
    for e in entries:
        if e.get("author"):
            members.add(e["author"])

    # Last log timestamp
    last_ts = ""
    if entries:
        sorted_entries = sorted(entries, key=lambda x: x.get("timestamp", ""), reverse=True)
        last_ts = sorted_entries[0].get("timestamp", "")

    return {
        "name": name,
        "path": repo_path_str,
        "active_count": active_count,
        "logged_today": len(logged_today),
        "last_log_timestamp": last_ts,
        "members": sorted(list(members)),
    }

# ---------------------------------------------------------------------------
# Background HTTP dashboard (spawned as daemon thread in stdio mode)
# ---------------------------------------------------------------------------

def _start_http_background():
    """Spawn the HTTP dashboard in a daemon thread (used during stdio mode)."""
    def _run():
        try:
            from fastapi import FastAPI
            from fastapi.responses import HTMLResponse, JSONResponse
            import uvicorn

            app = FastAPI(title="Pod-Sync")

            @app.get("/", response_class=HTMLResponse)
            async def serve_ui():
                if WEB_UI_PATH.exists():
                    return HTMLResponse(WEB_UI_PATH.read_text())
                return HTMLResponse("<h1>Pod-Sync</h1><p>web-ui/index.html not found.</p>")

            @app.get("/api/repos")
            async def api_repos():
                config = load_config()
                repos = []
                for rp in config.get("registered_repos", []):
                    repos.append(_build_repo_summary(rp))
                return JSONResponse(repos)

            @app.get("/api/repo/entries")
            async def api_repo_entries(path: str = ""):
                if not path:
                    return JSONResponse([])
                return JSONResponse(_read_repo_entries(path))

            @app.get("/api/repo/presence")
            async def api_repo_presence(path: str = ""):
                if not path:
                    return JSONResponse({})
                return JSONResponse(_read_repo_presence(path))

            @app.get("/api/repo/search")
            async def api_repo_search(path: str = "", q: str = ""):
                if not path or not q:
                    return JSONResponse([])
                entries = _read_repo_entries(path)
                q_lower = q.lower()
                results = [
                    e for e in entries
                    if q_lower in e.get("summary", "").lower()
                    or q_lower in e.get("title", "").lower()
                    or q_lower in " ".join(e.get("files_touched", [])).lower()
                    or q_lower in e.get("blockers", "").lower()
                    or q_lower in e.get("next_up", "").lower()
                    or q_lower in e.get("notes", "").lower()
                ]
                return JSONResponse(results)

            @app.get("/api/setup-status")
            async def api_setup_status():
                for cp in [
                    pathlib.Path.home() / ".codeium" / "windsurf" / "mcp_settings.json",
                    pathlib.Path.home() / "Library" / "Application Support" / "Code" / "User" / "mcp.json",
                    pathlib.Path.home() / ".config" / "Code" / "User" / "mcp.json",
                    pathlib.Path.home() / ".config" / "opencode" / "config.json",
                ]:
                    if cp.exists():
                        try:
                            data = json.loads(cp.read_text())
                            if "pod-sync" in data.get("mcpServers", {}):
                                return JSONResponse({"setup_complete": True})
                        except (json.JSONDecodeError, KeyError):
                            continue
                return JSONResponse({"setup_complete": False})

            uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="error")
        except OSError:
            pass
        except Exception as e:
            print(f"[http-bg] warning: {e}", file=sys.stderr)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# MCP server (--stdio mode)
# ---------------------------------------------------------------------------

def run_stdio():
    """Start the MCP server in stdio mode."""
    from mcp.server.fastmcp import FastMCP

    _start_http_background()

    mcp = FastMCP("pod-sync")

    @mcp.tool()
    def log_status(
        summary: str,
        repo_path: str,
        files_touched: list[str] = [],
        blockers: str = "None",
        next_up: str = "",
    ) -> str:
        """
        Log your end-of-day status entry to the project repo's logging branch.
        Author is auto-detected from git config — never ask the user for their name.
        repo_path: absolute path to the project repo the user is working in.
        summary: 2-4 sentences, past tense, specific. Synthesize from conversation context.
        files_touched: list of file paths modified today. Can be empty list.
        blockers: anything blocking progress, or 'None'.
        next_up: what you are picking up tomorrow. Specific enough a teammate could continue it.
        """
        return tool_log_status(summary, repo_path, files_touched, blockers, next_up)

    @mcp.tool()
    def read_status(
        repo_path: str,
        who: str = "all",
        when: str = "latest",
        query: str = "",
    ) -> str:
        """
        Read status and OpenSpec entries from a project repo's logging branch.
        repo_path: absolute path to the project repo to read from.
        who: 'all' or a partial name match (case-insensitive).
        when: 'latest', 'today', 'yesterday', 'this week', 'last week',
              or an ISO date 'YYYY-MM-DD', or a range 'YYYY-MM-DD:YYYY-MM-DD'.
        query: optional keyword filter applied after date/author filtering.
        Always git pulls before reading to ensure fresh data.
        """
        return tool_read_status(repo_path, who, when, query)

    @mcp.tool()
    def log_openspec_event(
        title: str,
        repo: str,
        repo_path: str,
        event_type: str = "proposal_created",
        openspec_path: str = "openspec/changes/",
        notes: str = "",
    ) -> str:
        """
        Called after an OpenSpec proposal is created or updated in a project repo.
        Commits the proposal and logs the event to entries.json on the logging branch.

        title: human-readable proposal title e.g. "Alert Engine Rate Limiting"
        repo: repo name e.g. "dashboard-repo"
        repo_path: absolute path to the project repo on this machine
        event_type: "proposal_created", "proposal_updated", "proposal_archived"
        openspec_path: path within repo to the OpenSpec change folder
        notes: optional one-line note about the proposal
        """
        return tool_log_openspec_event(title, repo, repo_path, event_type, openspec_path, notes)

    @mcp.tool()
    def read_presence(repo_path: str) -> str:
        """
        Show who on the team is currently active in a specific project repo.
        Reads heartbeat/presence.json from the repo's logging branch.
        Active = last_seen within 30 minutes.
        repo_path: absolute path to the project repo to check.
        Always git pulls before reading.
        """
        return tool_read_presence(repo_path)

    mcp.run(transport="stdio")

# ---------------------------------------------------------------------------
# HTTP server (--http mode)
# ---------------------------------------------------------------------------

def run_http():
    """Start the FastAPI HTTP server for the web UI."""
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn

    app = FastAPI(title="Pod-Sync")

    @app.get("/", response_class=HTMLResponse)
    async def serve_ui():
        if WEB_UI_PATH.exists():
            return HTMLResponse(WEB_UI_PATH.read_text())
        return HTMLResponse("<h1>Pod-Sync</h1><p>web-ui/index.html not found.</p>")

    # --- Repo-scoped data endpoints ---

    @app.get("/api/repos")
    async def api_repos():
        config = load_config()
        repos = []
        for rp in config.get("registered_repos", []):
            repos.append(_build_repo_summary(rp))
        return JSONResponse(repos)

    @app.get("/api/repo/entries")
    async def api_repo_entries(path: str = ""):
        if not path:
            return JSONResponse([])
        return JSONResponse(_read_repo_entries(path))

    @app.get("/api/repo/presence")
    async def api_repo_presence(path: str = ""):
        if not path:
            return JSONResponse({})
        return JSONResponse(_read_repo_presence(path))

    @app.get("/api/repo/search")
    async def api_repo_search(path: str = "", q: str = ""):
        if not path or not q:
            return JSONResponse([])
        entries = _read_repo_entries(path)
        q_lower = q.lower()
        results = [
            e for e in entries
            if q_lower in e.get("summary", "").lower()
            or q_lower in e.get("title", "").lower()
            or q_lower in " ".join(e.get("files_touched", [])).lower()
            or q_lower in e.get("blockers", "").lower()
            or q_lower in e.get("next_up", "").lower()
            or q_lower in e.get("notes", "").lower()
        ]
        return JSONResponse(results)

    # --- Setup endpoints (unchanged) ---

    @app.get("/api/detect-ides")
    async def api_detect_ides():
        detected = {}
        for ide, cmd in [("windsurf", "windsurf"), ("vscode", "code"), ("opencode", "opencode")]:
            detected[ide] = shutil.which(cmd) is not None
        return JSONResponse(detected)

    @app.post("/api/test-ssh")
    async def api_test_ssh():
        host = _get_git_host()
        result = subprocess.run(
            ["ssh", "-T", f"git@{host}"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stderr + result.stdout
        success = "successfully authenticated" in output.lower()
        return JSONResponse({"success": success, "message": output.strip()})

    @app.post("/api/store-pat")
    async def api_store_pat(request: Request):
        body = await request.json()
        pat = body.get("pat", "")
        if not pat:
            return JSONResponse({"success": False, "message": "No PAT provided."}, status_code=400)

        host = _get_git_host()
        credential_input = (
            "protocol=https\n"
            f"host={host}\n"
            f"username=git\n"
            f"password={pat}\n\n"
        )
        result = subprocess.run(
            ["git", "credential", "approve"],
            input=credential_input, capture_output=True, text=True
        )
        if result.returncode != 0:
            return JSONResponse({"success": False, "message": result.stderr.strip()})

        verify = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--quiet", "origin"],
            capture_output=True, text=True, cwd=REPO_ROOT
        )
        success = verify.returncode == 0
        return JSONResponse({
            "success": success,
            "message": "PAT stored and verified." if success else "PAT stored but verification failed."
        })

    @app.post("/api/complete-setup")
    async def api_complete_setup(request: Request):
        body = await request.json()
        ide = body.get("ide", "windsurf")

        venv_python = REPO_ROOT / ".venv" / "bin" / "python"
        server_path = REPO_ROOT / "server.py"

        mcp_entry = {
            "pod-sync": {
                "command": str(venv_python),
                "args": [str(server_path), "--stdio"]
            }
        }

        errors = []

        try:
            if ide == "windsurf":
                config_path = pathlib.Path.home() / ".codeium" / "windsurf" / "mcp_settings.json"
            elif ide == "vscode":
                if sys.platform == "darwin":
                    config_path = pathlib.Path.home() / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
                elif sys.platform == "win32":
                    config_path = pathlib.Path(os.environ.get("APPDATA", "")) / "Code" / "User" / "mcp.json"
                else:
                    config_path = pathlib.Path.home() / ".config" / "Code" / "User" / "mcp.json"
            elif ide == "opencode":
                config_path = pathlib.Path.home() / ".config" / "opencode" / "config.json"
            else:
                config_path = None

            if config_path:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                existing = {}
                if config_path.exists():
                    try:
                        existing = json.loads(config_path.read_text())
                    except json.JSONDecodeError:
                        existing = {}

                if "mcpServers" not in existing:
                    existing["mcpServers"] = {}
                existing["mcpServers"].update(mcp_entry)
                atomic_write(config_path, existing)
        except Exception as e:
            errors.append(f"MCP config: {e}")

        try:
            if ide == "windsurf":
                skills_dest = pathlib.Path.home() / ".codeium" / "windsurf" / "memories"
            elif ide == "vscode":
                skills_dest = pathlib.Path.home() / ".vscode" / "skills"
            elif ide == "opencode":
                skills_dest = pathlib.Path.home() / ".config" / "opencode" / "skills"
            else:
                skills_dest = None

            if skills_dest:
                skills_dest.mkdir(parents=True, exist_ok=True)
                for skill_file in SKILLS_DIR.glob("*.md"):
                    shutil.copy2(skill_file, skills_dest / skill_file.name)
        except Exception as e:
            errors.append(f"Skills copy: {e}")

        signal_path = REPO_ROOT / ".setup-complete"
        signal_path.write_text("done")

        if errors:
            return JSONResponse({"success": False, "errors": errors})
        return JSONResponse({"success": True, "ide": ide, "message": "Setup complete."})

    @app.get("/api/setup-status")
    async def api_setup_status():
        for cp in [
            pathlib.Path.home() / ".codeium" / "windsurf" / "mcp_settings.json",
            pathlib.Path.home() / "Library" / "Application Support" / "Code" / "User" / "mcp.json",
            pathlib.Path.home() / ".config" / "Code" / "User" / "mcp.json",
            pathlib.Path.home() / ".config" / "opencode" / "config.json",
        ]:
            if cp.exists():
                try:
                    data = json.loads(cp.read_text())
                    if "pod-sync" in data.get("mcpServers", {}):
                        return JSONResponse({"setup_complete": True})
                except (json.JSONDecodeError, KeyError):
                    continue
        return JSONResponse({"setup_complete": False})

    print(f"Pod-Sync server starting on http://localhost:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Pod-Sync — team coordination for AI-native dev pods",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  install-skills [.]    Install Pod-Sync skills to your IDE
                        Omit '.' for global install (all repos)
                        Add '.' for local install (current repo only)

Modes:
  --stdio               MCP stdio mode (spawned automatically by your IDE)
  --http                HTTP mode (web UI on localhost:7823)

Examples:
  pod-sync install-skills          # global install, works from any repo
  pod-sync install-skills .        # local install for current repo only
  python server.py --stdio         # started automatically by IDE via MCP config
  python server.py --http          # started by install.sh during setup
        """
    )
    parser.add_argument("--stdio", action="store_true", help="Run as MCP stdio server")
    parser.add_argument("--http", action="store_true", help="Run as HTTP server for web UI")

    subparsers = parser.add_subparsers(dest="command")
    skills_parser = subparsers.add_parser(
        "install-skills",
        help="Install Pod-Sync skill files to your IDE"
    )
    skills_parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="'.' to install locally in current repo, omit for global IDE install"
    )

    args = parser.parse_args()

    # CLI commands — run without server startup
    if args.command == "install-skills":
        cmd_install_skills(getattr(args, "target", None))
        sys.exit(0)

    # Server modes — require full startup
    startup()

    if args.stdio:
        run_stdio()
    elif args.http:
        run_http()
    else:
        parser.print_help()
        sys.exit(1)
