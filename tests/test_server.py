"""
Integration tests for Pod-Sync's git-backed entry store.

Each test runs against real git repos in a tmp directory: a bare `origin`
and per-user clones, exactly like a pod sharing a project repo.
"""

import json
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import server  # noqa: E402


def run(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def git_show(repo, ref_path):
    """Read a file from a ref without checking it out."""
    res = subprocess.run(
        ["git", "show", ref_path], cwd=repo, capture_output=True, text=True
    )
    return res.stdout if res.returncode == 0 else None


@pytest.fixture()
def pod(tmp_path, monkeypatch):
    """A bare origin plus two user clones (Alice, Bob), isolated git config."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = Fallback\n\temail = f@example.com\n"
        "[commit]\n\tgpgsign = false\n"
        "[init]\n\tdefaultBranch = main\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "config" / "config.json")
    monkeypatch.setattr(server, "WORKTREES_DIR", tmp_path / "worktrees")

    origin = tmp_path / "origin.git"
    run(["git", "init", "--bare", str(origin)], cwd=tmp_path)

    def clone(name, username):
        path = tmp_path / name
        run(["git", "clone", str(origin), str(path)], cwd=tmp_path)
        run(["git", "config", "user.name", username], cwd=path)
        run(["git", "config", "user.email", f"{name}@example.com"], cwd=path)
        return path

    alice = clone("alice_repo", "Alice")
    (alice / "app.py").write_text("print('hello')\n")
    run(["git", "add", "."], cwd=alice)
    run(["git", "commit", "-m", "init"], cwd=alice)
    run(["git", "push", "origin", "main"], cwd=alice)

    bob = clone("bob_repo", "Bob")

    return SimpleNamespace(origin=origin, alice=alice, bob=bob, tmp=tmp_path)


# ---------------------------------------------------------------------------
# log_status
# ---------------------------------------------------------------------------

def test_log_status_pushes_without_touching_working_tree(pod):
    # Dirty working tree on a feature branch — exactly the state Pod-Sync
    # must never disturb.
    run(["git", "checkout", "-b", "feat/x"], cwd=pod.alice)
    (pod.alice / "wip.txt").write_text("uncommitted work")

    result = server.tool_log_status(
        "Implemented the rate limiter.", str(pod.alice),
        files_touched=["app.py"], blockers="None", next_up="Tests"
    )
    assert "Logged and pushed" in result

    # User's tree untouched: same branch, dirty file intact, no data files.
    branch = run(["git", "branch", "--show-current"], cwd=pod.alice).stdout.strip()
    assert branch == "feat/x"
    assert (pod.alice / "wip.txt").read_text() == "uncommitted work"
    assert not (pod.alice / "entries").exists()

    # Entry landed on origin's logging branch.
    raw = git_show(pod.origin, "logging:entries/alice.jsonl")
    assert raw is not None
    entry = json.loads(raw.strip())
    assert entry["author"] == "Alice"
    assert entry["summary"] == "Implemented the rate limiter."
    assert entry["type"] == "status"


def test_logging_branch_is_orphan_with_no_project_code(pod):
    server.tool_log_status("Day one.", str(pod.alice))

    files = run(["git", "ls-tree", "-r", "--name-only", "logging"], cwd=pod.origin).stdout
    assert "app.py" not in files
    assert "entries/alice.jsonl" in files

    # No shared history with main.
    merge_base = subprocess.run(
        ["git", "merge-base", "logging", "main"],
        cwd=pod.origin, capture_output=True, text=True
    )
    assert merge_base.returncode != 0


def test_second_user_builds_on_origin_logging(pod):
    assert "Logged and pushed" in server.tool_log_status("Alice's work.", str(pod.alice))
    assert "Logged and pushed" in server.tool_log_status("Bob's work.", str(pod.bob))

    files = run(["git", "ls-tree", "-r", "--name-only", "logging"], cwd=pod.origin).stdout
    assert "entries/alice.jsonl" in files
    assert "entries/bob.jsonl" in files

    out = server.tool_read_status(str(pod.alice), when="today")
    assert "Alice's work." in out
    assert "Bob's work." in out


def test_concurrent_authors_do_not_conflict(pod):
    # Both users' worktrees exist before either pushes — the second push is
    # rejected and must rebase cleanly because they touch different files.
    server.ensure_logging_worktree(pod.alice)
    server.ensure_logging_worktree(pod.bob)

    assert "Logged and pushed" in server.tool_log_status("Alice first.", str(pod.alice))
    assert "Logged and pushed" in server.tool_log_status("Bob second.", str(pod.bob))

    out = server.tool_read_status(str(pod.bob), when="today")
    assert "Alice first." in out
    assert "Bob second." in out


def test_duplicate_update_and_replace_modes(pod):
    assert "Logged and pushed" in server.tool_log_status("Morning entry.", str(pod.alice))

    dup = server.tool_log_status("Second try.", str(pod.alice))
    assert "already have a status entry" in dup
    assert 'mode="update"' in dup and 'mode="replace"' in dup

    assert "Logged and pushed" in server.tool_log_status(
        "Mid-day addition.", str(pod.alice), mode="update")
    raw = git_show(pod.origin, "logging:entries/alice.jsonl")
    assert len(raw.strip().splitlines()) == 2

    assert "Logged and pushed" in server.tool_log_status(
        "Replaced entry.", str(pod.alice), mode="replace")
    raw = git_show(pod.origin, "logging:entries/alice.jsonl")
    lines = [json.loads(l) for l in raw.strip().splitlines()]
    assert len(lines) == 1
    assert lines[0]["summary"] == "Replaced entry."


def test_legacy_prefix_protocol_still_works(pod):
    server.tool_log_status("First.", str(pod.alice))
    assert "Logged and pushed" in server.tool_log_status("[replace] Via prefix.", str(pod.alice))
    raw = git_show(pod.origin, "logging:entries/alice.jsonl")
    lines = [json.loads(l) for l in raw.strip().splitlines()]
    assert len(lines) == 1
    assert lines[0]["summary"] == "Via prefix."


# ---------------------------------------------------------------------------
# read_status
# ---------------------------------------------------------------------------

def test_read_status_filters_by_author_and_query(pod):
    server.tool_log_status("Worked on the parser.", str(pod.alice))
    server.tool_log_status("Worked on the dashboard.", str(pod.bob))

    out = server.tool_read_status(str(pod.alice), who="ali")
    assert "parser" in out and "dashboard" not in out

    out = server.tool_read_status(str(pod.alice), query="dashboard")
    assert "dashboard" in out and "parser" not in out

    out = server.tool_read_status(str(pod.alice), who="nobody-here")
    assert out == "No entries found for that query."


def test_read_status_legacy_entries_json_compat(pod):
    # Pods that used the old single-file store can still read their history.
    wt = server.ensure_logging_worktree(pod.alice)
    legacy = [{
        "id": "1", "type": "status", "author": "Carol",
        "date": server.today_iso(), "timestamp": server.now_iso(),
        "summary": "Legacy entry.", "blockers": "None", "next_up": "",
    }]
    (wt / "entries.json").write_text(json.dumps(legacy))
    run(["git", "add", "entries.json"], cwd=wt)
    run(["git", "commit", "-m", "legacy"], cwd=wt)
    run(["git", "push", "origin", "logging"], cwd=wt)

    out = server.tool_read_status(str(pod.bob), when="today")
    assert "Legacy entry." in out


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def test_old_entries_are_archived_and_still_readable(pod):
    server.tool_log_status("Recent work.", str(pod.alice))

    # Inject an old entry directly into Alice's file.
    old_date = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    old_week = (datetime.now() - timedelta(days=200)).strftime("%G-W%V")
    wt = server.ensure_logging_worktree(pod.alice)
    author_file = wt / "entries" / "alice.jsonl"
    entries = server.read_jsonl(author_file)
    entries.insert(0, {
        "id": "old", "type": "status", "author": "Alice", "date": old_date,
        "timestamp": f"{old_date}T10:00:00+00:00",
        "summary": "Ancient work.", "blockers": "None", "next_up": "",
    })
    server.write_jsonl(author_file, entries)

    # Next write triggers archival.
    server.tool_log_status("Trigger archive.", str(pod.alice), mode="update")

    files = run(["git", "ls-tree", "-r", "--name-only", "logging"], cwd=pod.origin).stdout
    assert f"archive/alice-{old_week}.jsonl" in files
    current = git_show(pod.origin, "logging:entries/alice.jsonl")
    assert "Ancient work." not in current

    # Archived entries are found via a date query.
    out = server.tool_read_status(str(pod.alice), when=old_date)
    assert "Ancient work." in out


# ---------------------------------------------------------------------------
# OpenSpec
# ---------------------------------------------------------------------------

def test_openspec_writes_proposal_files_and_event(pod):
    result = server.tool_log_openspec_event(
        title="Rate Limiting",
        repo="alice_repo",
        repo_path=str(pod.alice),
        event_type="proposal_created",
        openspec_path="openspec/changes/rate-limit/",
        notes="Throttle the alert engine",
        proposal_files={"openspec/changes/rate-limit/proposal.md": "# Rate Limiting\n\nDetails.\n"},
    )
    assert "OpenSpec event logged" in result

    doc = git_show(pod.origin, "logging:openspec/changes/rate-limit/proposal.md")
    assert doc is not None and "# Rate Limiting" in doc

    raw = git_show(pod.origin, "logging:entries/alice.jsonl")
    event = json.loads(raw.strip())
    assert event["type"] == "openspec_proposal"
    assert event["title"] == "Rate Limiting"

    out = server.tool_read_status(str(pod.bob), when="today")
    assert "[openspec]" in out and "Rate Limiting" in out


def test_openspec_rejects_path_escape(pod):
    result = server.tool_log_openspec_event(
        title="Evil", repo="alice_repo", repo_path=str(pod.alice),
        proposal_files={"../../evil.md": "nope"},
    )
    assert "escapes the repo" in result
    assert not (pod.tmp / "evil.md").exists()


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------

def test_presence_derived_from_commit_activity(pod):
    # Alice pushed `main` in the fixture; Bob sees her as active.
    presence = server.derive_presence(pod.bob)
    assert "Alice" in presence
    assert presence["Alice"]["branch"] == "main"

    out = server.tool_read_presence(str(pod.bob))
    assert "Active now:" in out
    assert "Alice" in out


def test_presence_empty_when_no_recent_activity(pod, tmp_path):
    quiet = tmp_path / "quiet_repo"
    run(["git", "init", str(quiet)], cwd=tmp_path)
    run(["git", "remote", "add", "origin", str(pod.origin)], cwd=quiet)
    # No fetch yet, no remote refs — and even after fetch, the activity is
    # Alice's, so this just exercises the no-crash path.
    out = server.tool_read_presence(str(quiet))
    assert "Error" not in out


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def test_invalid_inputs(pod):
    assert "does not exist" in server.tool_log_status("x", "/nope/missing")
    assert "invalid mode" in server.tool_log_status("x", str(pod.alice), mode="banana")
    assert "Invalid date format" in server.tool_read_status(str(pod.alice), when="not-a-date")


def test_repo_registered_after_log(pod):
    server.tool_log_status("Register me.", str(pod.alice))
    config = server.load_config()
    assert str(pod.alice.resolve()) in config["registered_repos"]
