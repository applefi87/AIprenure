"""
github_api.py -- GitHub REST API client
=========================================
All GitHub API calls live here.
Uses only stdlib urllib -- no third-party dependencies.

Required env vars:
  GITHUB_TOKEN   Personal access token (repo + workflow scopes)
  GITHUB_REPO    Owner/repo string, e.g. "applefi87/AIprenure"
"""

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("github_api")

REPO_ROOT = Path(__file__).parent


# ------------------------------------------------------------------ #
#  Auth / config helpers                                               #
# ------------------------------------------------------------------ #

def _token() -> str:
    t = os.environ.get("GITHUB_TOKEN", "").strip()
    if not t:
        raise EnvironmentError("GITHUB_TOKEN not set")
    return t


def _repo() -> str:
    r = os.environ.get("GITHUB_REPO", "").strip()
    if not r:
        raise EnvironmentError("GITHUB_REPO not set  (format: owner/repo)")
    return r


def _owner() -> str:
    return _repo().split("/")[0]


# ------------------------------------------------------------------ #
#  Raw API call                                                        #
# ------------------------------------------------------------------ #

def _api(method: str, path: str, body: Optional[dict] = None) -> dict:
    """
    Make a GitHub REST API call.
    path must start with / (e.g. "/repos/owner/repo/pulls").
    Returns parsed JSON response.
    """
    url = "https://api.github.com" + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": "Bearer " + _token(),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "ai-company-orchestrator/1.0",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode(errors="replace")
        raise RuntimeError(
            "GitHub API " + method + " " + url + " -> HTTP " + str(e.code) + ": " + error_body
        ) from e


# ------------------------------------------------------------------ #
#  Branch push                                                         #
# ------------------------------------------------------------------ #

def push_branch(card: dict, worktree: Path) -> None:
    """
    Push the card branch from the worktree to origin.
    Uses token-authenticated HTTPS remote (no stored credentials needed).
    """
    token = _token()
    repo = _repo()
    branch = card["branch"]
    remote_url = "https://x-access-token:" + token + "@github.com/" + repo + ".git"

    result = subprocess.run(
        ["git", "push", remote_url, "HEAD:" + branch, "--force-with-lease"],
        cwd=worktree, capture_output=True, text=True,
    )
    if result.returncode != 0:
        # --force-with-lease fails when no upstream yet; retry with --force
        result2 = subprocess.run(
            ["git", "push", remote_url, "HEAD:" + branch, "--force"],
            cwd=worktree, capture_output=True, text=True,
        )
        if result2.returncode != 0:
            raise RuntimeError(
                "git push failed for " + branch + ": " + result2.stderr.strip()
            )
    log.info("Pushed branch %s  (card=%s)", branch, card["id"])


# ------------------------------------------------------------------ #
#  Pull Request                                                        #
# ------------------------------------------------------------------ #

def create_pr(card: dict, base: str = "dev") -> int:
    """
    Open a PR for this card. Returns the PR number.
    """
    repo = _repo()
    card_id = card["id"]
    card_title = card["title"]
    branch = card["branch"]
    title = "feat(" + card_id + "): " + card_title
    body = "**Card**: " + card_id + "\n**Title**: " + card_title + "\n\n_Automatic PR by AI Company orchestrator._"

    resp = _api("POST", "/repos/" + repo + "/pulls", {
        "title": title,
        "body": body,
        "head": branch,
        "base": base,
        "draft": False,
    })
    pr_number = resp["number"]
    log.info("Created PR #%d  (card=%s  branch=%s)", pr_number, card_id, branch)
    return pr_number


def get_pr_for_branch(card: dict) -> Optional[dict]:
    """
    Return the open PR dict for this card branch, or None if none exists.
    """
    repo = _repo()
    owner = _owner()
    branch = card["branch"]
    prs = _api("GET", "/repos/" + repo + "/pulls?state=open&head=" + owner + ":" + branch + "&per_page=1")
    return prs[0] if prs else None


# ------------------------------------------------------------------ #
#  CI / Checks                                                         #
# ------------------------------------------------------------------ #

def get_ci_status(pr_number: int) -> str:
    """
    Return CI status for a PR: "pending" | "success" | "failure".

    Checks GitHub Checks API on the PR head SHA.
    pending  -- checks still running or not yet started
    success  -- all checks completed successfully
    failure  -- at least one check failed / cancelled / timed out
    """
    repo = _repo()
    pr = _api("GET", "/repos/" + repo + "/pulls/" + str(pr_number))
    sha = pr["head"]["sha"]

    checks = _api("GET", "/repos/" + repo + "/commits/" + sha + "/check-runs?per_page=100")
    runs = checks.get("check_runs", [])

    if not runs:
        log.debug("CI: no check runs yet for PR #%d (sha=%s)", pr_number, sha[:8])
        return "pending"

    statuses = {r["status"] for r in runs}
    if statuses != {"completed"}:
        log.debug(
            "CI: %d/%d checks completed for PR #%d",
            sum(1 for r in runs if r["status"] == "completed"),
            len(runs), pr_number,
        )
        return "pending"

    failure_set = {"failure", "cancelled", "timed_out", "action_required"}
    conclusions = {r["conclusion"] for r in runs}
    if conclusions & failure_set:
        log.warning("CI failure for PR #%d: conclusions=%s", pr_number, conclusions)
        return "failure"

    log.info("CI success for PR #%d", pr_number)
    return "success"


# ------------------------------------------------------------------ #
#  Merge trigger                                                       #
# ------------------------------------------------------------------ #

def trigger_merge(pr_number: int, card_id: str) -> None:
    """
    Trigger the privileged merge workflow (merge.yml) via workflow_dispatch.
    The workflow runs on main and does the actual squash merge to dev.
    """
    repo = _repo()
    _api("POST", "/repos/" + repo + "/actions/workflows/merge.yml/dispatches", {
        "ref": "main",
        "inputs": {
            "pr_number": str(pr_number),
            "card_id": card_id,
        }
    })
    log.info("Triggered merge.yml for PR #%d  (card=%s)", pr_number, card_id)


def wait_for_merge(pr_number: int, max_polls: int = 20, interval_sec: int = 15) -> bool:
    """
    Poll until the PR is merged (state == closed + merged) or max_polls exceeded.
    Returns True if merged, False if timed out or closed without merge.
    """
    repo = _repo()
    for i in range(max_polls):
        pr = _api("GET", "/repos/" + repo + "/pulls/" + str(pr_number))
        if pr.get("merged"):
            log.info("PR #%d confirmed merged", pr_number)
            return True
        if pr["state"] == "closed" and not pr.get("merged"):
            log.warning("PR #%d was closed without merging", pr_number)
            return False
        log.debug("Waiting for PR #%d merge (%d/%d)", pr_number, i + 1, max_polls)
        time.sleep(interval_sec)
    log.warning("PR #%d merge wait timed out after %d polls", pr_number, max_polls)
    return False
