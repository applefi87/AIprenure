"""
truth.py -- deterministic fact-checking
========================================
All "run container / check exit code / verify git" logic lives here.
The orchestrator trusts only what this module returns -- never agent self-reports.

M0/M1: stubs
M2:    run_in_container real, tests_fail / tests_pass via container
M3:    pr_exists / ci_green via GitHub API
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger("truth")

REPO_ROOT = Path(__file__).parent
WORKTREES_DIR = REPO_ROOT / ".worktrees"
_HOME = Path(os.path.expanduser("~"))
CLAUDE_AUTH_DIR = _HOME / ".claude"


# ------------------------------------------------------------------ #
#  ContainerResult                                                     #
# ------------------------------------------------------------------ #

class ContainerResult:
    def __init__(self, exit_code: int, stdout: str = "", stderr: str = ""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    def __repr__(self) -> str:
        return (
            f"ContainerResult(exit_code={self.exit_code}, "
            f"stdout={self.stdout[:80]!r})"
        )


# ------------------------------------------------------------------ #
#  Git worktree management                                             #
# ------------------------------------------------------------------ #

def worktree_path(card: dict) -> Path:
    """Return host path for this card's git worktree."""
    return WORKTREES_DIR / card["id"]


def setup_worktree(card: dict) -> Path:
    """
    Ensure a git worktree exists for this card's branch.
    Creates the branch and worktree if they don't exist.
    Returns the worktree path (used as Docker volume mount).
    """
    wt = worktree_path(card)
    card_id = card["id"]
    branch = card.get("branch") or f"card/{card_id}"
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)

    if wt.exists():
        log.debug("Worktree already exists: %s", wt)
        return wt

    # Create branch if it does not exist yet
    r = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if not r.stdout.strip():
        subprocess.run(
            ["git", "branch", branch],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )
        log.info("Created branch %s", branch)

    # Add worktree
    subprocess.run(
        ["git", "worktree", "add", str(wt), branch],
        cwd=REPO_ROOT, check=True,
    )
    log.info("Worktree created: %s  (branch=%s)", wt, branch)
    return wt


def teardown_worktree(card: dict) -> None:
    """Remove worktree after card is done."""
    wt = worktree_path(card)
    subprocess.run(
        ["git", "worktree", "remove", str(wt), "--force"],
        cwd=REPO_ROOT, capture_output=True,
    )
    log.info("Worktree removed: %s", wt)


# ------------------------------------------------------------------ #
#  Container execution                                                  #
# ------------------------------------------------------------------ #

def run_in_container(
    image: str,
    cmd: list,
    work_dir: Optional[Path] = None,
    timeout_sec: int = 300,
    env: Optional[dict] = None,
) -> ContainerResult:
    """
    Run a command inside a Docker container.

    Args:
        image:       Docker image name (e.g. "ai-company-worker")
        cmd:         Command + args list
        work_dir:    Host path mounted as /work (read-write)
        timeout_sec: Wall-clock timeout; returns exit_code=-1 on expiry
        env:         Extra env vars injected into container

    Auth:
        ~/.claude is mounted read-only at /root/.claude so that
        claude -p can use the Claude Pro subscription inside the container.
    """
    docker_cmd = ["docker", "run", "--rm", "--network", "bridge"]

    # Claude Pro auth (read-only)
    if CLAUDE_AUTH_DIR.exists():
        docker_cmd += ["-v", f"{CLAUDE_AUTH_DIR}:/root/.claude:ro"]

    # Work directory
    if work_dir is not None:
        docker_cmd += ["-v", f"{work_dir}:/work", "-w", "/work"]

    # Extra environment
    for k, v in (env or {}).items():
        docker_cmd += ["-e", f"{k}={v}"]

    docker_cmd += [image] + list(cmd)

    log.info("run_in_container: %s", " ".join(str(x) for x in docker_cmd))

    try:
        proc = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        return ContainerResult(proc.returncode, proc.stdout, proc.stderr)

    except subprocess.TimeoutExpired:
        log.error("Container timed out after %ds  work_dir=%s", timeout_sec, work_dir)
        return ContainerResult(-1, "", f"Timed out after {timeout_sec}s")

    except FileNotFoundError:
        log.error("'docker' not found -- is Docker Desktop running?")
        return ContainerResult(-2, "", "docker not found; ensure Docker Desktop is running")


# ------------------------------------------------------------------ #
#  Test verification                                                    #
# ------------------------------------------------------------------ #

_TEST_CMD = [
    "bash", "-lc",
    # Detect Python tests or Node tests
    ("if ls tests/*.py 2>/dev/null | head -1 | grep -q . || [ -f conftest.py ]; then "
     "  pytest -q; "
     "elif [ -f package.json ]; then "
     "  npm test -- --watchAll=false; "
     "else "
     "  echo 'No test files found' && exit 1; "
     "fi"),
]


def _run_tests_in_container(card: dict, work_dir: Path) -> ContainerResult:
    return run_in_container(
        image="ai-company-worker",
        cmd=_TEST_CMD,
        work_dir=work_dir,
        timeout_sec=300,
    )


def tests_fail(card: dict, work_dir: Optional[Path] = None) -> bool:
    """
    Run tests expecting them to FAIL (TDD red state).
    Returns True if tests actually fail (correct pre-implementation state).
    """
    wd = work_dir or worktree_path(card)
    result = _run_tests_in_container(card, wd)
    is_red = result.exit_code != 0
    log.info("tests_fail  card=%s  exit_code=%d  is_red=%s", card["id"], result.exit_code, is_red)
    if not is_red:
        log.warning(
            "Tests PASSED before implementation for %s -- agent may have written bad tests",
            card["id"],
        )
    return is_red


def tests_pass(card: dict, work_dir: Optional[Path] = None) -> bool:
    """
    Run tests expecting them to PASS (TDD green state).
    Returns True if all tests pass.
    """
    wd = work_dir or worktree_path(card)
    result = _run_tests_in_container(card, wd)
    is_green = result.exit_code == 0
    log.info("tests_pass  card=%s  exit_code=%d  is_green=%s", card["id"], result.exit_code, is_green)
    return is_green


# ------------------------------------------------------------------ #
#  Git checks                                                           #
# ------------------------------------------------------------------ #

def branch_exists(branch: str) -> bool:
    """Check whether a local git branch exists."""
    try:
        r = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        return bool(r.stdout.strip())
    except Exception as e:
        log.warning("branch_exists failed: %s", e)
        return False


# ------------------------------------------------------------------ #
#  GitHub checks (M3)                                                  #
# ------------------------------------------------------------------ #

def pr_exists(card: dict) -> bool:
    """Return True if an open PR exists for this card's branch."""
    import github_api
    return github_api.get_pr_for_branch(card) is not None


def ci_green(card: dict) -> bool:
    """Return True if CI is passing for this card's PR."""
    import github_api
    pr_number = card.get("pr_number")
    if not pr_number:
        return False
    return github_api.get_ci_status(pr_number) == "success"
