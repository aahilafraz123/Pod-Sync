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


def test_branch_creation_is_reported_once(pod):
    first = server.tool_log_status("First ever entry.", str(pod.alice))
    assert "created" in first and "logging" in first

    second = server.tool_log_status("Second session.", str(pod.alice))
    assert "Note: the 'logging' branch did not exist" not in second

    # A teammate whose repo gets the branch from origin is not told it was
    # "created" — it already existed for the team.
    bob_log = server.tool_log_status("Bob's entry.", str(pod.bob))
    assert "Note: the 'logging' branch did not exist" not in bob_log


def test_sessions_append_without_prompting(pod):
    assert "Logged and pushed" in server.tool_log_status("Morning session.", str(pod.alice))
    second = server.tool_log_status("Afternoon session.", str(pod.alice))
    assert "Logged and pushed" in second
    assert "session 2 today" in second

    raw = git_show(pod.origin, "logging:entries/alice.jsonl")
    lines = [json.loads(l) for l in raw.strip().splitlines()]
    assert [e["summary"] for e in lines] == ["Morning session.", "Afternoon session."]

    # "today" returns every session; "latest" returns only the newest per author.
    out = server.tool_read_status(str(pod.alice), when="today")
    assert "Morning session." in out and "Afternoon session." in out
    out = server.tool_read_status(str(pod.alice), when="latest")
    assert "Afternoon session." in out and "Morning session." not in out


def test_replace_swaps_only_the_most_recent_entry(pod):
    server.tool_log_status("Morning session.", str(pod.alice))
    server.tool_log_status("Afternoon session.", str(pod.alice))

    result = server.tool_log_status("Afternoon, corrected.", str(pod.alice), mode="replace")
    assert "Logged and pushed" in result

    raw = git_show(pod.origin, "logging:entries/alice.jsonl")
    lines = [json.loads(l) for l in raw.strip().splitlines()]
    assert [e["summary"] for e in lines] == ["Morning session.", "Afternoon, corrected."]


def test_replace_with_nothing_to_replace(pod):
    result = server.tool_log_status("Oops.", str(pod.alice), mode="replace")
    assert "Nothing to replace" in result


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


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def test_old_entries_are_archived_and_still_readable(pod):
    server.tool_log_status("Recent work.", str(pod.alice))

    # Inject an old entry directly into Alice's file.
    old_date = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    old_week = (datetime.now() - timedelta(days=200)).strftime("%G-W%V")
    wt, _ = server.ensure_logging_worktree(pod.alice)
    author_file = wt / "entries" / "alice.jsonl"
    entries = server.read_jsonl(author_file)
    entries.insert(0, {
        "id": "old", "type": "status", "author": "Alice", "date": old_date,
        "timestamp": f"{old_date}T10:00:00+00:00",
        "summary": "Ancient work.", "blockers": "None", "next_up": "",
    })
    server.write_jsonl(author_file, entries)

    # Next write triggers archival.
    server.tool_log_status("Trigger archive.", str(pod.alice))

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

def test_openspec_mirrors_working_tree_docs_to_logging(pod):
    # The OpenSpec library creates documents on the user's working branch;
    # Pod-Sync only mirrors them to the logging branch.
    change_dir = pod.alice / "openspec" / "changes" / "rate-limit"
    change_dir.mkdir(parents=True)
    (change_dir / "proposal.md").write_text("# Rate Limiting\n\nDetails.\n")
    (change_dir / "tasks.md").write_text("- [ ] implement throttle\n")

    result = server.tool_log_openspec_event(
        title="Rate Limiting",
        repo="alice_repo",
        repo_path=str(pod.alice),
        event_type="proposal_created",
        openspec_path="openspec/changes/rate-limit/",
        notes="Throttle the alert engine",
    )
    assert "OpenSpec event logged" in result

    # Mirrored to logging on origin.
    doc = git_show(pod.origin, "logging:openspec/changes/rate-limit/proposal.md")
    assert doc is not None and "# Rate Limiting" in doc
    tasks = git_show(pod.origin, "logging:openspec/changes/rate-limit/tasks.md")
    assert tasks is not None and "throttle" in tasks

    # Working tree copy untouched and still on the user's branch.
    assert (change_dir / "proposal.md").read_text().startswith("# Rate Limiting")
    branch = run(["git", "branch", "--show-current"], cwd=pod.alice).stdout.strip()
    assert branch == "main"

    raw = git_show(pod.origin, "logging:entries/alice.jsonl")
    event = json.loads(raw.strip())
    assert event["type"] == "openspec_proposal"
    assert event["title"] == "Rate Limiting"

    out = server.tool_read_status(str(pod.bob), when="today")
    assert "[openspec]" in out and "Rate Limiting" in out


def test_openspec_requires_existing_docs(pod):
    result = server.tool_log_openspec_event(
        title="Ghost", repo="alice_repo", repo_path=str(pod.alice),
        openspec_path="openspec/changes/does-not-exist/",
    )
    assert "not found in the working tree" in result

    # Archiving is allowed even after OpenSpec moved the folder away.
    result = server.tool_log_openspec_event(
        title="Ghost", repo="alice_repo", repo_path=str(pod.alice),
        event_type="proposal_archived",
        openspec_path="openspec/changes/does-not-exist/",
    )
    assert "OpenSpec event logged" in result


def test_openspec_rejects_path_escape(pod):
    result = server.tool_log_openspec_event(
        title="Evil", repo="alice_repo", repo_path=str(pod.alice),
        openspec_path="../escape/",
    )
    assert "escapes the repo" in result

    result = server.tool_log_openspec_event(
        title="Evil", repo="alice_repo", repo_path=str(pod.alice),
        openspec_path="/etc",
    )
    assert "must be relative" in result


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
# Skill installation
# ---------------------------------------------------------------------------

def test_skill_sources_follow_agent_skills_convention():
    dirs = server._skill_dirs()
    assert [d.name for d in dirs] == ["pod-sync-read", "pod-sync-update"]
    for d in dirs:
        text = (d / "SKILL.md").read_text()
        assert text.startswith("---")
        assert f"name: {d.name}" in text


def test_install_skills_local_windsurf_layout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        server.shutil, "which", lambda cmd: "/usr/bin/windsurf" if cmd == "windsurf" else None
    )
    server.cmd_install_skills(".")
    assert (tmp_path / ".windsurf" / "skills" / "pod-sync-update" / "SKILL.md").exists()
    assert (tmp_path / ".windsurf" / "skills" / "pod-sync-read" / "SKILL.md").exists()


def test_install_skills_global_windsurf_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        server.shutil, "which", lambda cmd: "/usr/bin/windsurf" if cmd == "windsurf" else None
    )
    with pytest.raises(SystemExit):
        server.cmd_install_skills(None)
    assert "per project" in capsys.readouterr().out
    assert not (tmp_path / ".windsurf").exists()


def test_install_skills_local_vscode_layout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        server.shutil, "which", lambda cmd: "/usr/bin/code" if cmd == "code" else None
    )
    server.cmd_install_skills(".")
    assert (tmp_path / ".github" / "skills" / "pod-sync-update" / "SKILL.md").exists()


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
