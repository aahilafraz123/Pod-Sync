#!/usr/bin/env python3
"""
Pod-Sync MCP Server + Local HTTP API

Two modes:
  python server.py --stdio   → MCP stdio mode (spawned by IDE)
  python server.py --http    → HTTP mode (web UI + API on localhost:7823)

Both modes share identical tool logic.

Data architecture: all data lives on the `logging` branch of each project
repo — NOT in the pod-sync repo. The pod-sync repo is purely a tool:
installer, server, skills.

  <project repo> logging branch:
    entries/<author>.jsonl            rolling 90-day entry store, one file
                                      per author so concurrent teammates
                                      never produce git merge conflicts
    archive/<author>-YYYY-W##.jsonl   entries older than 90 days
    openspec/changes/...              OpenSpec proposal documents

Pod-Sync never checks out the logging branch in the user's working tree.
All reads and writes go through a hidden git worktree under
~/.local/share/pod-sync/worktrees/, so the user's (and their agent's)
working state is never stashed, switched, or otherwise disturbed.

Presence is derived from recent commit activity on origin — there is no
heartbeat file and no background commit traffic.
"""

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).parent.resolve()
WEB_UI_PATH = REPO_ROOT / "web-ui" / "index.html"
SKILLS_DIR = REPO_ROOT / "skills"
CONFIG_PATH = pathlib.Path.home() / ".config" / "pod-sync" / "config.json"
WORKTREES_DIR = pathlib.Path.home() / ".local" / "share" / "pod-sync" / "worktrees"
PORT = 7823
ACTIVE_WINDOW_MINUTES = 30
PRESENCE_LOOKBACK = "7.days"

# ---------------------------------------------------------------------------
# Helpers — file I/O
# ---------------------------------------------------------------------------

def atomic_write(path: pathlib.Path, data):
    """Write JSON atomically — tmp file then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def read_jsonl(path: pathlib.Path) -> list:
    """Read a JSONL file into a list of dicts. Skips malformed lines."""
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def write_jsonl(path: pathlib.Path, entries: list):
    """Write a list of dicts as JSONL atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(e) + "\n" for e in entries)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)

# ---------------------------------------------------------------------------
# Helpers — date/time
# ---------------------------------------------------------------------------

def _parse_date(date_str):
    """Parse YYYY-MM-DD to datetime. Returns datetime.min on failure."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return datetime.min


def _parse_ts(ts: str) -> Optional[datetime]:
    """Parse an ISO timestamp to an aware datetime. Naive values are assumed UTC."""
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def today_iso():
    # Local date on purpose: a status entry belongs to the author's working day.
    return datetime.now().strftime("%Y-%m-%d")


def iso_week():
    return datetime.now().strftime("%G-W%V")


def now_iso():
    # Timestamps are UTC with an explicit offset so presence/sorting math is
    # correct across timezones.
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

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
        atomic_write(CONFIG_PATH, config)

# ---------------------------------------------------------------------------
# Helpers — git
# ---------------------------------------------------------------------------

def git_config_username(cwd=None):
    """Read git config user.name from a repo. Returns 'Unknown' if not set."""
    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True, text=True, cwd=cwd or REPO_ROOT
        )
        name = result.stdout.strip()
        return name if name else "Unknown"
    except Exception:
        return "Unknown"


def author_slug(name: str) -> str:
    """Filesystem-safe identifier for an author's entry file."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "unknown"


def _git_run(args, cwd=None, check=False, input_text=None):
    """Run a git command, return CompletedProcess."""
    return subprocess.run(
        ["git"] + args,
        cwd=cwd or REPO_ROOT,
        capture_output=True, text=True,
        check=check, input=input_text
    )


def _git_pull_branch(branch="logging", cwd=None):
    """Pull latest from origin for a specific branch. Returns (success, error_msg)."""
    result = _git_run(["pull", "--rebase", "origin", branch], cwd=cwd)
    if result.returncode != 0:
        return False, _classify_git_error(result.stderr, cwd=cwd)
    return True, ""


def _git_push(branch="logging", cwd=None):
    """Push to origin. Returns (success, error_msg)."""
    result = _git_run(["push", "origin", branch], cwd=cwd)
    if result.returncode != 0:
        return False, _classify_git_error(result.stderr, cwd=cwd)
    return True, ""


def _git_commit_and_push(message, paths=None, branch="logging", cwd=None, retries=3):
    """Stage paths, commit, push. Retries with pull-rebase on push failure."""
    for p in (paths or ["-A"]):
        _git_run(["add", str(p)], cwd=cwd)

    result = _git_run(["commit", "-m", message], cwd=cwd)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "nothing to commit" in stderr or "nothing to commit" in result.stdout:
            return True, ""
        return False, f"Commit failed: {stderr}"

    err = ""
    for _ in range(retries):
        ok, err = _git_push(branch, cwd=cwd)
        if ok:
            return True, ""
        # Push rejected — pull rebase and retry. Per-author entry files mean
        # the rebase never has to merge another teammate's changes into ours.
        pull_ok, pull_err = _git_pull_branch(branch, cwd=cwd)
        if not pull_ok:
            return False, f"Push failed and pull-rebase also failed: {pull_err}"

    return False, f"Push failed after {retries} attempts: {err}"


def _get_git_host(cwd=None):
    """Parse the git host from the origin remote URL.
    Handles both SSH (git@host:...) and HTTPS (https://host/...) formats."""
    try:
        url = _git_run(["remote", "get-url", "origin"], cwd=cwd).stdout.strip()
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
        name = url.split("/")[-1].split(":")[-1]
        return name or repo_path.name
    except Exception:
        return repo_path.name


def _classify_git_error(stderr, cwd=None):
    """Inspect stderr for auth/SSO signals and return actionable message."""
    lower = stderr.lower()
    for signal in ["saml", "sso", "authentication", "403", "permission denied"]:
        if signal in lower:
            host = _get_git_host(cwd=cwd)
            return (
                f"Git authentication failed. Re-authorize your credentials:\n"
                f"  SSH: https://{host}/settings/ssh\n"
                f"  PAT: https://{host}/settings/tokens\n"
                f"Raw error: {stderr.strip()}"
            )
    return stderr.strip()

# ---------------------------------------------------------------------------
# Helpers — logging worktree
#
# Pod-Sync does all logging-branch work in a hidden worktree, never in the
# user's checkout. No stashing, no branch switching, no races with the
# user's editor or agent.
# ---------------------------------------------------------------------------

_REPO_LOCKS = {}
_REPO_LOCKS_GUARD = threading.Lock()


def _repo_lock(repo_path) -> threading.Lock:
    """One lock per project repo, shared by tool calls and the dashboard."""
    key = str(pathlib.Path(repo_path).resolve())
    with _REPO_LOCKS_GUARD:
        return _REPO_LOCKS.setdefault(key, threading.Lock())


def _worktree_path(repo: pathlib.Path) -> pathlib.Path:
    digest = hashlib.sha1(str(repo).encode()).hexdigest()[:12]
    return WORKTREES_DIR / f"{repo.name}-{digest}"


def _ref_exists(ref: str, cwd) -> bool:
    return _git_run(["rev-parse", "--verify", "--quiet", ref], cwd=cwd).returncode == 0


BRANCH_CREATED_NOTE = (
    "Note: the 'logging' branch did not exist in this repo, so Pod-Sync "
    "created it — an orphan branch holding only team updates and OpenSpec "
    "documents, never code. Tell the user this happened."
)


def _ensure_logging_branch_exists(repo: pathlib.Path) -> bool:
    """Create the local logging branch if missing. Returns True if created
    brand-new (so callers can notify the user).

    Bases it on origin/logging when the remote branch exists (so teammates
    share one history), otherwise creates an orphan branch with an empty
    tree so no project code ever leaks onto the logging branch."""
    if _ref_exists("refs/heads/logging", repo):
        return False

    _git_run(["fetch", "origin", "logging"], cwd=repo)
    if _ref_exists("refs/remotes/origin/logging", repo):
        result = _git_run(["branch", "--track", "logging", "origin/logging"], cwd=repo)
        if result.returncode != 0:
            raise RuntimeError(f"Could not create logging branch: {result.stderr.strip()}")
        return False  # branch existed on origin — nothing new for the team

    tree = _git_run(["mktree"], cwd=repo, input_text="")
    if tree.returncode != 0:
        raise RuntimeError(f"Could not create logging branch: {tree.stderr.strip()}")
    commit = _git_run(
        ["commit-tree", tree.stdout.strip(), "-m", "pod-sync: initialize logging branch"],
        cwd=repo
    )
    if commit.returncode != 0:
        raise RuntimeError(f"Could not create logging branch: {commit.stderr.strip()}")
    result = _git_run(["branch", "logging", commit.stdout.strip()], cwd=repo)
    if result.returncode != 0:
        raise RuntimeError(f"Could not create logging branch: {result.stderr.strip()}")
    return True


def ensure_logging_worktree(repo_path):
    """Return (directory, branch_created) where the logging branch is checked out.

    Normally a hidden worktree under WORKTREES_DIR. If the user happens to
    have the logging branch checked out in their own working tree, that
    checkout is used directly (a branch can only be checked out once).
    branch_created is True when the logging branch was newly created for
    this repo, so tools can notify the user."""
    repo = pathlib.Path(repo_path).resolve()
    if _git_run(["rev-parse", "--git-dir"], cwd=repo).returncode != 0:
        raise RuntimeError(f"Not a git repository: {repo}")

    if _git_run(["branch", "--show-current"], cwd=repo).stdout.strip() == "logging":
        return repo, False

    wt = _worktree_path(repo)
    if (wt / ".git").exists() and _git_run(["rev-parse", "--git-dir"], cwd=wt).returncode == 0:
        return wt, False

    # Stale or missing — rebuild it.
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    _git_run(["worktree", "prune"], cwd=repo)
    created = _ensure_logging_branch_exists(repo)
    wt.parent.mkdir(parents=True, exist_ok=True)
    result = _git_run(["worktree", "add", str(wt), "logging"], cwd=repo)
    if result.returncode != 0:
        raise RuntimeError(f"Could not create pod-sync worktree: {result.stderr.strip()}")
    return wt, created


def _sync_worktree(wt: pathlib.Path):
    """Pull the latest logging branch into the worktree. Returns (ok, err).
    A missing remote branch is fine — the first push will create it."""
    ok, err = _git_pull_branch("logging", cwd=wt)
    if not ok and "couldn't find remote ref" in err.lower():
        return True, ""
    return ok, err

# ---------------------------------------------------------------------------
# Helpers — entry store (per-author JSONL files on the logging branch)
# ---------------------------------------------------------------------------

def _author_file(wt: pathlib.Path, slug: str) -> pathlib.Path:
    return wt / "entries" / f"{slug}.jsonl"


def _load_worktree_entries(wt: pathlib.Path, include_archive=False, weeks=None) -> list:
    """Load all authors' entries from the logging worktree."""
    entries = []

    entries_dir = wt / "entries"
    if entries_dir.is_dir():
        for f in sorted(entries_dir.glob("*.jsonl")):
            entries += read_jsonl(f)

    if include_archive and weeks:
        archive_dir = wt / "archive"
        for week in weeks:
            for f in sorted(archive_dir.glob(f"*-{week}.jsonl")):
                entries += read_jsonl(f)

    return entries


def archive_old_entries(wt: pathlib.Path, slug: str):
    """Move this author's entries older than 90 days into weekly archive files.
    Only touches the author's own files so teammates never conflict.
    No-op if nothing qualifies."""
    try:
        author_file = _author_file(wt, slug)
        entries = read_jsonl(author_file)
        cutoff = datetime.now() - timedelta(days=90)

        to_keep, to_archive = [], []
        for e in entries:
            d = _parse_date(e.get("date", ""))
            if d != datetime.min and d < cutoff:
                to_archive.append(e)
            else:
                to_keep.append(e)

        if not to_archive:
            return

        by_week = {}
        for entry in to_archive:
            week = datetime.strptime(entry["date"], "%Y-%m-%d").strftime("%G-W%V")
            by_week.setdefault(week, []).append(entry)

        for week, week_entries in by_week.items():
            archive_path = wt / "archive" / f"{slug}-{week}.jsonl"
            write_jsonl(archive_path, read_jsonl(archive_path) + week_entries)

        write_jsonl(author_file, to_keep)
    except Exception as e:
        print(f"[archive] warning: {e}", file=sys.stderr)

# ---------------------------------------------------------------------------
# Helpers — presence (derived from commit activity, no heartbeat)
# ---------------------------------------------------------------------------

def derive_presence(project: pathlib.Path) -> dict:
    """Who has pushed commits recently, derived from origin's branches.

    Returns {author: {"last_seen": iso_ts, "branch": name}}. Read-only:
    a fetch updates remote-tracking refs but never touches the working tree.
    """
    _git_run(["fetch", "origin", "--quiet"], cwd=project)  # best effort; offline → stale refs
    result = _git_run(
        ["log", "--remotes=origin", "--source", f"--since={PRESENCE_LOOKBACK}",
         "--date=iso-strict", "--format=%an%x09%aI%x09%S"],
        cwd=project
    )
    # --source may attribute a commit to origin/HEAD; translate that to the
    # default branch's real name.
    head_target = _git_run(
        ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=project
    ).stdout.strip()
    presence = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, ts, ref = parts
        if name in presence:
            continue  # log is newest-first, so the first hit per author wins
        branch = head_target if (ref.endswith("/HEAD") and head_target) else ref
        for prefix in ("refs/remotes/origin/", "origin/"):
            if branch.startswith(prefix):
                branch = branch[len(prefix):]
                break
        presence[name] = {"last_seen": ts, "branch": branch}
    return presence

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
    No data files to create — data lives on project repos' logging branches."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Tool logic — shared by MCP and HTTP modes
# All data operations target the project repo's logging branch via the
# hidden worktree. The user's working tree is never touched.
# ---------------------------------------------------------------------------

def tool_log_status(summary: str, repo_path: str, files_touched: list = None,
                    blockers: str = "None", next_up: str = "", mode: str = "log") -> str:
    """Log a working-session status entry to the project repo's logging branch.

    Each call appends one session entry — multiple sessions per day are
    normal. mode="replace" swaps out today's most recent entry instead
    (for fixing a mistake)."""
    if files_touched is None:
        files_touched = []

    project = pathlib.Path(repo_path)
    if not project.exists():
        return f"Error: repo_path does not exist: {repo_path}"

    author = git_config_username(cwd=project)
    if author == "Unknown":
        return "Error: git config user.name is not set. Run: git config --global user.name \"Your Name\""

    if mode not in ("log", "replace"):
        return (f"Error: invalid mode '{mode}'. Use 'log' (append this session's entry) "
                f"or 'replace' (replace today's most recent entry).")

    repo_name = get_repo_name_from_path(project)
    date = today_iso()
    slug = author_slug(author)

    with _repo_lock(project):
        try:
            wt, branch_created = ensure_logging_worktree(project)
        except RuntimeError as e:
            return f"Error preparing logging worktree: {e}"

        try:
            sync_ok, sync_err = _sync_worktree(wt)

            author_file = _author_file(wt, slug)
            entries = read_jsonl(author_file)

            if mode == "replace":
                todays = [e for e in entries if e.get("date") == date and e.get("type") == "status"]
                if not todays:
                    return (f"Nothing to replace — you have no status entry for today ({date}). "
                            f"Call again without mode=\"replace\" to log normally.")
                # The author file is append-only, so the last of today's
                # entries is the most recent regardless of timestamp ties.
                latest = todays[-1]
                entries = [e for e in entries if e is not latest]

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

            session_number = sum(
                1 for e in entries if e.get("date") == date and e.get("type") == "status"
            ) + 1

            entries.append(entry)
            write_jsonl(author_file, entries)
            archive_old_entries(wt, slug)

            ok, err = _git_commit_and_push(
                f"status: {author} — {date}",
                paths=["entries", "archive"],
                branch="logging",
                cwd=wt
            )
        except Exception as e:
            return f"Error logging status: {e}"

    register_repo(str(project.resolve()))

    if not ok:
        return f"Entry saved locally but push failed: {err}"

    msg = f"Logged and pushed to {repo_name}/logging (entries/{slug}.jsonl). ({author}, {date}"
    if session_number > 1:
        msg += f", session {session_number} today"
    msg += ")"
    if branch_created:
        msg += f"\n{BRANCH_CREATED_NOTE}"
    if not sync_ok:
        msg += f"\nNote: could not sync with origin before writing ({sync_err})."
    return msg


def tool_read_status(repo_path: str, who: str = "all", when: str = "latest", query: str = "") -> str:
    """Read status and OpenSpec entries from a project repo's logging branch."""
    project = pathlib.Path(repo_path)
    if not project.exists():
        return f"Error: repo_path does not exist: {repo_path}"

    # Determine date range and whether archive is needed (no I/O yet)
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
            return f"Invalid date range format: {when}. Use YYYY-MM-DD:YYYY-MM-DD."
    elif when != "latest":
        try:
            date_start = datetime.strptime(when, "%Y-%m-%d")
            date_end = date_start.replace(hour=23, minute=59, second=59)
        except ValueError:
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

    with _repo_lock(project):
        try:
            wt, branch_created = ensure_logging_worktree(project)
        except RuntimeError as e:
            return f"Error preparing logging worktree: {e}"
        try:
            sync_ok, sync_err = _sync_worktree(wt)
            entries = _load_worktree_entries(wt, include_archive=need_archive, weeks=archive_weeks)
        except Exception as e:
            return f"Error reading status: {e}"

    if not entries:
        msg = "No entries found — this repo has no status log yet."
        if branch_created:
            msg += f"\n{BRANCH_CREATED_NOTE}"
        return msg

    # Filter by author
    if who != "all":
        entries = [e for e in entries if who.lower() in e.get("author", "").lower()]

    # Filter by date
    if when == "latest":
        by_author = {}
        # File position breaks timestamp ties: author files are append-only,
        # so a later index means a newer entry.
        ordered = sorted(
            enumerate(entries),
            key=lambda p: (p[1].get("timestamp", ""), p[0]),
            reverse=True,
        )
        for _, e in ordered:
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
    if not sync_ok:
        lines.append(f"(warning: could not sync with origin — showing last known data: {sync_err})")
        lines.append("")
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


def tool_log_openspec_event(
    title: str,
    repo: str,
    repo_path: str,
    event_type: str = "proposal_created",
    openspec_path: str = "openspec/changes/",
    notes: str = "",
) -> str:
    """Mirror OpenSpec documents from the user's working tree to the logging
    branch and record the event, in one commit.

    OpenSpec (the spec-driven development library) creates its documents on
    the user's working branch as part of its own workflow. Pod-Sync only
    mirrors them: the working tree is read, never modified, and the documents
    keep living on the working branch as normal."""
    project = pathlib.Path(repo_path)
    if not project.exists():
        return f"Error: repo_path does not exist: {repo_path}"

    author = git_config_username(cwd=project)
    if author == "Unknown":
        return "Error: git config user.name is not set. Run: git config --global user.name \"Your Name\""

    if pathlib.Path(openspec_path).is_absolute():
        return f"Error: openspec_path must be relative to the repo root: {openspec_path}"
    src = project / openspec_path
    try:
        src.resolve().relative_to(project.resolve())
    except ValueError:
        return f"Error: openspec_path escapes the repo: {openspec_path}"

    # Archiving may have moved the folder; for created/updated it must exist.
    if not src.exists() and event_type != "proposal_archived":
        return (
            f"Error: {openspec_path} not found in the working tree. "
            f"Run the OpenSpec workflow first — Pod-Sync mirrors existing "
            f"documents, it does not create them."
        )

    slug = author_slug(author)

    with _repo_lock(project):
        try:
            wt, branch_created = ensure_logging_worktree(project)
        except RuntimeError as e:
            return f"Error preparing logging worktree: {e}"

        try:
            _sync_worktree(wt)

            if src.exists():
                dest = wt / openspec_path
                if src.is_dir():
                    shutil.copytree(src, dest, dirs_exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)

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

            author_file = _author_file(wt, slug)
            entries = read_jsonl(author_file)
            entries.append(event)
            write_jsonl(author_file, entries)

            ok, err = _git_commit_and_push(
                f"openspec: {title} ({event_type})",
                paths=[openspec_path, "entries"],
                branch="logging",
                cwd=wt
            )
        except Exception as e:
            return f"Error logging openspec event: {e}"

    register_repo(str(project.resolve()))

    if not ok:
        return f"OpenSpec event saved locally but push failed: {err}"

    msg = (
        f"OpenSpec event logged. "
        f"Documents mirrored to {repo}/logging (working branch copy untouched). "
        f"Event visible in team dashboard."
    )
    if branch_created:
        msg += f"\n{BRANCH_CREATED_NOTE}"
    return msg


def tool_read_presence(repo_path: str) -> str:
    """Show who on the team is recently active in a specific project repo,
    derived from commit activity on origin."""
    project = pathlib.Path(repo_path)
    if not project.exists():
        return f"Error: repo_path does not exist: {repo_path}"

    try:
        presence = derive_presence(project)
    except Exception as e:
        return f"Error reading presence: {e}"

    if not presence:
        return (
            "No recent activity found — nobody has pushed commits to this "
            "repo's origin in the last 7 days."
        )

    now = datetime.now(timezone.utc)
    active = []
    away = []

    for name, info in presence.items():
        last_seen = _parse_ts(info.get("last_seen", ""))
        minutes = 9999 if last_seen is None else int((now - last_seen).total_seconds() / 60)
        branch = info.get("branch", "unknown")

        if minutes <= ACTIVE_WINDOW_MINUTES:
            time_str = "just now" if minutes < 1 else f"{minutes} min ago"
            active.append(f"  {name}  — last push {time_str}  — {branch}")
        else:
            if minutes < 60:
                time_str = f"{minutes} min ago"
            elif minutes < 1440:
                time_str = f"{minutes // 60}h ago"
            else:
                time_str = f"{minutes // 1440}d ago"
            away.append(f"  {name}  — last push {time_str}")

    lines = ["Presence (derived from commit activity on origin):", "", "Active now:"]
    lines.extend(active if active else ["  (nobody)"])
    lines.append("")
    lines.append("Away:")
    lines.extend(away if away else ["  (nobody)"])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers — repo data for the web UI
# ---------------------------------------------------------------------------

def load_repo_entries(repo_path_str: str) -> list:
    """Read all entries from a project repo's logging branch. Returns list."""
    project = pathlib.Path(repo_path_str)
    if not project.exists():
        return []
    with _repo_lock(project):
        try:
            wt, _ = ensure_logging_worktree(project)
            _sync_worktree(wt)
            return _load_worktree_entries(wt)
        except Exception as e:
            print(f"[read-entries] warning ({repo_path_str}): {e}", file=sys.stderr)
            return []


def load_repo_presence(repo_path_str: str) -> dict:
    """Derived presence for a project repo. Returns dict."""
    project = pathlib.Path(repo_path_str)
    if not project.exists():
        return {}
    try:
        return derive_presence(project)
    except Exception as e:
        print(f"[read-presence] warning ({repo_path_str}): {e}", file=sys.stderr)
        return {}


def _build_repo_summary(repo_path_str: str) -> dict:
    """Build summary data for a single repo (used by /api/repos)."""
    project = pathlib.Path(repo_path_str)
    name = get_repo_name_from_path(project)
    entries = load_repo_entries(repo_path_str)
    presence = load_repo_presence(repo_path_str)

    today = today_iso()
    now = datetime.now(timezone.utc)

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
        last_seen = _parse_ts(info.get("last_seen", ""))
        if last_seen and (now - last_seen).total_seconds() <= ACTIVE_WINDOW_MINUTES * 60:
            active_count += 1

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
# HTTP app — one factory shared by --http mode and the stdio-mode dashboard
# ---------------------------------------------------------------------------

def build_app(include_setup=True):
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="Pod-Sync")

    @app.middleware("http")
    async def _localhost_only(request, call_next):
        # Blocks DNS-rebinding style access; the server itself binds 127.0.0.1.
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("localhost", "127.0.0.1", "[::1]", ""):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return await call_next(request)

    def _registered(path: str):
        """Resolve a client-supplied path against registered repos. The API
        never runs git in a directory Pod-Sync wasn't told about."""
        if not path:
            return None
        try:
            resolved = str(pathlib.Path(path).resolve())
        except OSError:
            return None
        known = {str(pathlib.Path(p).resolve()) for p in load_config().get("registered_repos", [])}
        return resolved if resolved in known else None

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
        rp = _registered(path)
        if rp is None:
            return JSONResponse({"error": "unknown repo"}, status_code=404)
        return JSONResponse(load_repo_entries(rp))

    @app.get("/api/repo/presence")
    async def api_repo_presence(path: str = ""):
        rp = _registered(path)
        if rp is None:
            return JSONResponse({"error": "unknown repo"}, status_code=404)
        return JSONResponse(load_repo_presence(rp))

    @app.get("/api/repo/search")
    async def api_repo_search(path: str = "", q: str = ""):
        rp = _registered(path)
        if rp is None:
            return JSONResponse({"error": "unknown repo"}, status_code=404)
        if not q:
            return JSONResponse([])
        entries = load_repo_entries(rp)
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

    if not include_setup:
        return app

    # --- Setup endpoints (wizard only) ---

    @app.get("/api/detect-ides")
    async def api_detect_ides():
        detected = {}
        for ide, cmd in [("windsurf", "windsurf"), ("vscode", "code"), ("opencode", "opencode")]:
            detected[ide] = shutil.which(cmd) is not None
        return JSONResponse(detected)

    @app.post("/api/test-ssh")
    async def api_test_ssh():
        host = _get_git_host()
        try:
            result = subprocess.run(
                ["ssh", "-T", f"git@{host}"],
                capture_output=True, text=True, timeout=10
            )
        except subprocess.TimeoutExpired:
            return JSONResponse({"success": False, "message": "SSH test timed out after 10s."})
        except FileNotFoundError:
            return JSONResponse({"success": False, "message": "ssh command not found on this machine."})
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

    return app

# ---------------------------------------------------------------------------
# Background HTTP dashboard (spawned as daemon thread in stdio mode)
# ---------------------------------------------------------------------------

def _start_http_background():
    """Spawn the HTTP dashboard in a daemon thread (used during stdio mode)."""
    def _run():
        try:
            import uvicorn
            app = build_app(include_setup=False)
            uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="error")
        except OSError:
            pass  # another Pod-Sync process already serves the dashboard
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
        mode: str = "log",
    ) -> str:
        """
        Log a working-session status entry to the project repo's logging branch.
        Each call appends one session entry — logging multiple sessions per day
        is normal and needs no special handling.
        Author is auto-detected from git config — never ask the user for their name.
        Never touches the user's working tree (writes happen in a hidden worktree).
        repo_path: absolute path to the project repo the user is working in.
        summary: 2-4 sentences, past tense, specific. Synthesize from conversation context.
        files_touched: list of file paths modified this session. Can be empty list.
        blockers: anything blocking progress, or 'None'.
        next_up: what gets picked up next session. Specific enough a teammate could continue it.
        mode: 'log' (default — append this session's entry), or 'replace'
              (replace today's most recent entry, for fixing a mistake).
        """
        return tool_log_status(summary, repo_path, files_touched, blockers, next_up, mode)

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
        Always syncs with origin before reading; never touches the user's working tree.
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
        Called after the OpenSpec workflow creates, updates, or archives a
        change in a project repo. Mirrors the OpenSpec documents from the
        user's working tree to the logging branch and records the event, in
        one commit. The working tree is only read — the documents keep living
        on the user's working branch as normal.

        title: human-readable proposal title e.g. "Alert Engine Rate Limiting"
        repo: repo name e.g. "dashboard-repo"
        repo_path: absolute path to the project repo on this machine
        event_type: "proposal_created", "proposal_updated", "proposal_archived"
        openspec_path: repo-relative path to the OpenSpec change folder,
                       e.g. "openspec/changes/rate-limit/". The documents must
                       already exist there (OpenSpec creates them).
        notes: optional one-line note about the proposal
        """
        return tool_log_openspec_event(title, repo, repo_path, event_type, openspec_path, notes)

    @mcp.tool()
    def read_presence(repo_path: str) -> str:
        """
        Show who on the team is recently active in a specific project repo.
        Presence is derived from commit activity on origin (last push per
        author across all branches). Active = pushed within 30 minutes.
        repo_path: absolute path to the project repo to check.
        Read-only; never touches the user's working tree.
        """
        return tool_read_presence(repo_path)

    mcp.run(transport="stdio")

# ---------------------------------------------------------------------------
# HTTP server (--http mode)
# ---------------------------------------------------------------------------

def run_http():
    """Start the FastAPI HTTP server for the web UI."""
    import uvicorn

    app = build_app(include_setup=True)
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
