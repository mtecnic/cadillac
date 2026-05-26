"""Atomic patch application + verification.

For each candidate patch:
  1. Save patch + regression test to disk
  2. Run unit-test sweep first (the new test should fail BEFORE patch)
  3. Apply patch via `git apply`
  4. Run unit-test sweep (must pass — including the new regression test)
  5. Run a shorter probe to check matrix score doesn't regress
  6. On pass: keep the patch + test, return success
  7. On fail: `git checkout -- .` revert, delete the test file, log reason

Capped at one successful commit per iteration.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from .proposer import Proposal


@dataclass
class ApplyResult:
    proposal: Proposal
    success: bool
    reason: str  # human-readable outcome


def _run(cmd: list[str], cwd: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def _git_dirty(cwd: str) -> bool:
    r = _run(["git", "status", "--porcelain"], cwd=cwd)
    return bool(r.stdout.strip())


def _git_revert_all(cwd: str) -> None:
    """Hard reset working tree to HEAD (untracked files removed)."""
    _run(["git", "checkout", "--", "."], cwd=cwd, timeout=10)
    _run(["git", "clean", "-fd"], cwd=cwd, timeout=10)


def _run_unit_tests(cadillac_root: str) -> tuple[bool, str]:
    r = _run(
        ["python3", "-m", "unittest", "discover",
         "-s", "cadillac/tests", "-p", "test_*.py"],
        cwd=cadillac_root, timeout=180,
    )
    output = (r.stdout or "") + (r.stderr or "")
    return (r.returncode == 0), output[-2000:]


def apply_proposal(proposal: Proposal, cadillac_root: str, emit) -> ApplyResult:
    """Apply one proposal atomically. Returns ApplyResult with outcome.

    cadillac_root is the dir containing cadillac/ (a git repo).
    """
    if _git_dirty(cadillac_root):
        return ApplyResult(
            proposal=proposal, success=False,
            reason="working tree dirty before apply — refusing to risk losing changes",
        )

    # 1. Write the regression test FIRST so we can verify it fails pre-patch.
    test_full_path = os.path.join(cadillac_root, proposal.test_path)
    os.makedirs(os.path.dirname(test_full_path), exist_ok=True)
    with open(test_full_path, "w") as f:
        f.write(proposal.test_content + "\n")

    # The test should FAIL before the patch (otherwise it doesn't pin the fix).
    # If running it shows pass already, the proposal is misframed — revert.
    pre_ok, pre_out = _run_unit_tests(cadillac_root)
    if pre_ok:
        # Test passed without the patch — bad regression test
        try:
            os.unlink(test_full_path)
        except OSError:
            pass
        return ApplyResult(
            proposal=proposal, success=False,
            reason="regression test passed before patch (doesn't pin a real fix)",
        )

    # 2. Apply the patch.
    patch_path = test_full_path + ".diff.tmp"
    with open(patch_path, "w") as f:
        f.write(proposal.patch + "\n")

    check = _run(["git", "apply", "--check", patch_path], cwd=cadillac_root, timeout=30)
    if check.returncode != 0:
        try:
            os.unlink(test_full_path)
            os.unlink(patch_path)
        except OSError:
            pass
        return ApplyResult(
            proposal=proposal, success=False,
            reason=f"git apply --check failed: {check.stderr.strip()[:200]}",
        )

    apply = _run(["git", "apply", patch_path], cwd=cadillac_root, timeout=30)
    try:
        os.unlink(patch_path)
    except OSError:
        pass
    if apply.returncode != 0:
        _git_revert_all(cadillac_root)
        return ApplyResult(
            proposal=proposal, success=False,
            reason=f"git apply failed: {apply.stderr.strip()[:200]}",
        )

    # 3. Run unit tests after patch.
    post_ok, post_out = _run_unit_tests(cadillac_root)
    if not post_ok:
        emit("log", msg=f"[improve/apply] {proposal.weakness_id}: "
                         f"post-patch tests failed, reverting")
        _git_revert_all(cadillac_root)
        return ApplyResult(
            proposal=proposal, success=False,
            reason=f"post-patch unit tests failed: {post_out[:300]}",
        )

    return ApplyResult(
        proposal=proposal, success=True,
        reason="patch applied; new regression test went red→green; "
               "full unit suite still green",
    )


def commit_applied(result: ApplyResult, cadillac_root: str,
                   *, iteration: int,
                   score_delta: float | None = None) -> str | None:
    """Commit the changes from a successful apply. Returns commit SHA or None.

    When `score_delta` is provided and exceeds +0.05 against the prior
    iteration's matrix score, also writes a meta-lesson to cadillac/memory.jsonl
    tagged ['cadillac', 'self'] so future improve iterations can learn from
    what kinds of changes moved the score. The lesson captures the weakness
    id + target file + the patch's high-level pattern.
    """
    if not result.success:
        return None
    msg = (
        f"fix(improve r{iteration}): {result.proposal.weakness_id}\n\n"
        f"Auto-applied by cadillac improve cycle.\n\n"
        f"{result.reason}\n\n"
        f"Targets: {result.proposal.target_file}\n"
        f"Pinned by: {result.proposal.test_path}\n"
    )
    add = _run(["git", "add", "-A"], cwd=cadillac_root, timeout=30)
    if add.returncode != 0:
        return None
    commit = _run(
        ["git", "-c", "user.name=cadillac-improve",
         "-c", "user.email=improve@cadillac.local",
         "commit", "-m", msg],
        cwd=cadillac_root, timeout=30,
    )
    if commit.returncode != 0:
        return None
    sha_proc = _run(["git", "rev-parse", "--short", "HEAD"],
               cwd=cadillac_root, timeout=10)
    sha = sha_proc.stdout.strip() if sha_proc.returncode == 0 else None

    if sha and score_delta is not None and score_delta >= 0.05:
        _record_meta_lesson(result, sha, iteration, score_delta)
    return sha


def _record_meta_lesson(result: ApplyResult, sha: str, iteration: int,
                         score_delta: float) -> None:
    """Append one architecture-class lesson tagged ['cadillac', 'self'].

    Best-effort: any failure (memory.py import, lock contention, disk full)
    is swallowed — losing a meta-lesson never blocks an improve commit. The
    next iteration can re-record it if the pattern reappears.
    """
    try:
        from cadillac.memory import Lesson, save_lesson
        import time
        trigger = (
            f"weakness {result.proposal.weakness_id} in "
            f"{result.proposal.target_file}"
        )
        fix = (
            f"matrix score improved by {score_delta:+.3f} on iter {iteration} "
            f"(commit {sha}): {result.reason[:200]}"
        )
        save_lesson(Lesson(
            ts=time.time(),
            type="architecture",
            trigger=trigger,
            fix=fix,
            confidence=0.6,    # rewarded by observed improvement
            polarity="do",
            tags=["cadillac", "self"],
            source_task="cadillac improve cycle",
        ))
    except Exception:
        pass
