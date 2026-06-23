"""
truth.py — 真相查核（M2+ 填入實作）
=====================================
封裝所有「查 git / 跑測試 / 看 exit code」的確定性檢查。
編排器只信這裡的回傳值，不信 agent 的自述。

M0：所有函式為 stub，回傳固定值以利骨架跑通。
M2+：替換為真實容器呼叫。
"""

import logging
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger("truth")


# ─── 容器執行（M2 填入）────────────────────────────────────────────────

class ContainerResult:
    def __init__(self, exit_code: int, stdout: str = "", stderr: str = ""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def run_in_container(
    image: str,
    cmd: list[str],
    mount: Optional[str] = None,
    timeout_sec: int = 300,
    no_secrets: bool = True,
    env: Optional[dict] = None,
) -> ContainerResult:
    """
    在 Docker 容器內執行指令。
    M2 填入真實 docker run 呼叫。
    """
    log.info("[M0-STUB] run_in_container image=%s cmd=%s", image, cmd)
    # M2+ 真實實作：
    # docker_cmd = [
    #     "docker", "run", "--rm",
    #     "--network", "bridge",
    #     f"--timeout={timeout_sec}",
    # ]
    # if mount:
    #     docker_cmd += ["-v", f"{mount}:/work"]
    # if env:
    #     for k, v in env.items():
    #         docker_cmd += ["-e", f"{k}={v}"]
    # docker_cmd += [image] + cmd
    # proc = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=timeout_sec)
    # return ContainerResult(proc.returncode, proc.stdout, proc.stderr)
    raise NotImplementedError("run_in_container：M2 尚未實作")


# ─── 測試查核 ────────────────────────────────────────────────────────────

def tests_fail(card: dict) -> bool:
    """
    在容器內跑測試，預期 exit code != 0（紅）。
    回傳 True 代表測試確實失敗（正確的 TDD 前置狀態）。
    """
    result = run_in_container(
        image="ai-company-worker",
        cmd=["bash", "-lc", "pytest -q 2>/dev/null || npm test 2>/dev/null"],
        mount=card_checkout_path(card),
    )
    is_red = result.exit_code != 0
    log.info("tests_fail card=%s exit_code=%d → is_red=%s", card["id"], result.exit_code, is_red)
    return is_red


def tests_pass(card: dict) -> bool:
    """
    在容器內跑測試，預期 exit code == 0（綠）。
    回傳 True 代表測試全部通過。
    """
    result = run_in_container(
        image="ai-company-worker",
        cmd=["bash", "-lc", "pytest -q 2>/dev/null || npm test 2>/dev/null"],
        mount=card_checkout_path(card),
    )
    is_green = result.exit_code == 0
    log.info("tests_pass card=%s exit_code=%d → is_green=%s", card["id"], result.exit_code, is_green)
    return is_green


# ─── Git 查核 ────────────────────────────────────────────────────────────

def pr_exists(card: dict) -> bool:
    """
    確認 GitHub 上這張卡的 PR 是否真實存在。
    M3 填入 GitHub API 呼叫。
    """
    log.info("[M0-STUB] pr_exists card=%s", card["id"])
    raise NotImplementedError("pr_exists：M3 尚未實作")


def ci_green(card: dict) -> bool:
    """
    確認 PR 的 CI checks 全部通過。
    M3 填入 GitHub API 呼叫。
    """
    log.info("[M0-STUB] ci_green card=%s", card["id"])
    raise NotImplementedError("ci_green：M3 尚未實作")


def branch_exists(branch: str, repo_path: Optional[str] = None) -> bool:
    """確認 git branch 是否存在（本地或遠端）。"""
    try:
        cmd = ["git", "branch", "--list", branch]
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=repo_path
        )
        return branch in result.stdout
    except Exception as e:
        log.warning("branch_exists 失敗：%s", e)
        return False


# ─── 工具函式 ────────────────────────────────────────────────────────────

def card_checkout_path(card: dict) -> str:
    """回傳這張卡的分支 checkout 路徑（供掛進容器用）。"""
    branch = card.get("branch") or f"card/{card['id']}"
    # M2+ 替換為實際 checkout 目錄
    return f"/work/{branch}"
