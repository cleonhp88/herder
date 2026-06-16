"""Tests for herder.runspace module."""
import hashlib
import os
import stat
import subprocess


from herder.runspace import create_run_dir, snapshot_prompt, write_result_md, make_worktree, capture_worktree_diff


def test_run_dir_layout(herder_home):
    """create_run_dir creates a directory with artifacts subdirectory."""
    rd = create_run_dir("job_X")
    assert rd.exists()
    assert rd.name == "job_X"
    assert (rd / "artifacts").exists()


def test_snapshot(herder_home):
    """snapshot_prompt writes prompt to file and returns (path, SHA256 digest)."""
    rd = create_run_dir("job_Y")
    path, digest = snapshot_prompt(rd, "analyze TCB")
    assert path.read_text() == "analyze TCB"
    assert digest == hashlib.sha256("analyze TCB".encode()).hexdigest()


def test_snapshot_unicode_prompt(herder_home):
    """snapshot_prompt handles unicode (Vietnamese) text correctly with UTF-8 encoding."""
    rd = create_run_dir("job_U")
    vn = "Phân tích cổ phiếu — nhịp tim ❤"
    path, digest = snapshot_prompt(rd, vn)
    assert path.read_text(encoding="utf-8") == vn
    assert digest == hashlib.sha256(vn.encode("utf-8")).hexdigest()


def test_write_result_md(herder_home):
    """write_result_md creates result.md with YAML frontmatter + body."""
    rd = create_run_dir("job_R")
    p = write_result_md(rd, {"job_id": "job_R", "status": "done"}, "the answer")
    text = p.read_text(encoding="utf-8")
    assert p.name == "result.md"
    assert text.startswith("---\n")
    assert "job_id: job_R" in text and "status: done" in text
    assert text.rstrip().endswith("the answer")


def _git_repo(tmp_path):
    """Helper: create a minimal git repo with one commit."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*args):
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    g("init", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (repo / "f.txt").write_text("original\n")
    g("add", "-A")
    g("commit", "-m", "init")
    return repo


def test_make_worktree_creates_isolated_copy(herder_home, tmp_path):
    """make_worktree creates an isolated git worktree with job-scoped branch."""
    repo = _git_repo(tmp_path)
    wt = make_worktree(repo, "job_W")
    assert wt.exists() and (wt / "f.txt").read_text() == "original\n"
    assert wt != repo
    # branch name is job-scoped
    r = subprocess.run(
        ["git", "-C", str(wt), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert r.stdout.strip() == "herder/job_W"


def test_capture_worktree_diff_detects_changes(herder_home, tmp_path):
    """capture_worktree_diff detects both file modifications and new files."""
    repo = _git_repo(tmp_path)
    wt = make_worktree(repo, "job_W2")
    (wt / "f.txt").write_text("modified\n")
    (wt / "new.txt").write_text("brand new\n")
    out = tmp_path / "changes.diff"
    p = capture_worktree_diff(wt, out)
    assert p == out and out.exists()
    diff = out.read_text()
    assert "modified" in diff and "brand new" in diff
    # original repo untouched
    assert (repo / "f.txt").read_text() == "original\n"
    assert not (repo / "new.txt").exists()


def test_capture_worktree_diff_none_when_clean(herder_home, tmp_path):
    """capture_worktree_diff returns None when the worktree is clean."""
    repo = _git_repo(tmp_path)
    wt = make_worktree(repo, "job_W3")
    assert capture_worktree_diff(wt, tmp_path / "c.diff") is None


def test_make_worktree_idempotent_on_second_call(herder_home, tmp_path):
    """make_worktree is idempotent; second call reuses the existing worktree."""
    repo = _git_repo(tmp_path)
    wt1 = make_worktree(repo, "job_RE")
    (wt1 / "scratch.txt").write_text("attempt 1 work\n")
    wt2 = make_worktree(repo, "job_RE")  # must NOT raise
    assert wt2 == wt1
    assert (wt2 / "scratch.txt").exists()  # reused, not recreated


def test_git_helper_disables_hooks(herder_home, tmp_path):
    """Verify that git worktree add disables hooks to prevent trojan attacks.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    repo = _git_repo(tmp_path)
    # install a malicious post-checkout hook that would create a marker file
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    marker = tmp_path / "HOOK_FIRED"
    h = hooks / "post-checkout"
    h.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    h.chmod(0o755)
    make_worktree(repo, "job_HK")  # worktree add triggers checkout
    assert not marker.exists(), "git hook should have been disabled (core.hooksPath=/dev/null)"


def test_git_helper_disables_fsmonitor(herder_home, tmp_path):
    """FIX 3: Verify that git disables filesystem monitor (RCE vector).

    A configured core.fsmonitor pointing to arbitrary shell script could
    execute attacker code. _git() must disable it.
    """
    repo = _git_repo(tmp_path)

    # Create a malicious fsmonitor script that would fire if enabled
    marker = tmp_path / "FSMON_FIRED"
    fsmon_script = repo / "fsmon.sh"
    fsmon_script.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    fsmon_script.chmod(0o755)

    # Configure the repo to use this malicious fsmonitor
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.fsmonitor", str(fsmon_script)],
        check=True,
        capture_output=True,
    )

    # Now use make_worktree, which calls _git() internally.
    # If fsmonitor were enabled, the script would fire during the git add.
    wt = make_worktree(repo, "job_FM")
    assert wt.exists()

    # Verify the marker was NOT created (fsmonitor was disabled)
    assert not marker.exists(), "fsmonitor script should have been disabled (core.fsmonitor=)"


def test_run_dir_is_owner_only(herder_home):
    """FIX 4: create_run_dir sets owner-only permissions (0o700)."""
    rd = create_run_dir("job_P")
    mode = stat.S_IMODE(os.stat(rd).st_mode)
    # Verify no group or world bits
    assert mode & 0o077 == 0, f"run dir has group/world bits: {oct(mode)}"
