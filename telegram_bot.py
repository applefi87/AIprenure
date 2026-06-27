"""
telegram_bot.py — Telegram 人類核准入口（M5）
==============================================
純 stdlib urllib — 不需要 python-telegram-bot 套件。

需在 .env 設定：
  TELEGRAM_BOT_TOKEN=123456:ABCdef...
  TELEGRAM_CHAT_ID=-100123456789  (群組 ID 或個人 chat_id)

公開 API（由編排器主迴圈呼叫）：
  send_message(text)          -> 傳送通知給人類
  get_pending_approvals()     -> 輪詢，回傳 /approve <story_id> 清單
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("telegram_bot")

_OFFSET_FILE = Path(__file__).parent / "data" / ".telegram_offset"


def _token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not t:
        raise EnvironmentError("TELEGRAM_BOT_TOKEN 未設定")
    return t


def _chat_id() -> str:
    c = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not c:
        raise EnvironmentError("TELEGRAM_CHAT_ID 未設定")
    return c


def is_configured() -> bool:
    return bool(
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and
        os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    )


def _api_get(method: str, params: dict = None) -> object:
    url = "https://api.telegram.org/bot" + _token() + "/" + method
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "ai-company/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")
        raise RuntimeError("Telegram GET " + method + " error " + str(e.code) + ": " + err) from e
    if not body.get("ok"):
        raise RuntimeError("Telegram not ok: " + str(body))
    return body.get("result")


def _api_post(method: str, data: dict) -> object:
    url = "https://api.telegram.org/bot" + _token() + "/" + method
    payload = json.dumps(data).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "ai-company/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")
        raise RuntimeError("Telegram POST " + method + " error " + str(e.code) + ": " + err) from e
    if not body.get("ok"):
        raise RuntimeError("Telegram not ok: " + str(body))
    return body.get("result")


def send_message(text: str) -> None:
    """傳送文字訊息到設定的 chat。失敗只記 log，不拋例外。"""
    if not is_configured():
        log.debug("Telegram 未設定，跳過：%s", text[:60])
        return
    try:
        _api_post("sendMessage", {
            "chat_id": _chat_id(),
            "text": text,
            "parse_mode": "HTML",
        })
        log.info("Telegram 已傳送：%s", text[:80])
    except Exception as e:
        log.error("Telegram 傳送失敗：%s", e)


def _load_offset() -> int:
    try:
        return int(_OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    try:
        _OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _OFFSET_FILE.write_text(str(offset))
    except Exception as e:
        log.warning("無法儲存 telegram offset：%s", e)


def get_pending_approvals() -> list:
    """
    非阻塞輪詢 Telegram（timeout=0）。
    解析 /approve <story_id> 指令，回傳核准的 story_id 清單。
    自動推進 offset，不重播已處理的訊息。
    """
    if not is_configured():
        return []
    offset = _load_offset()
    try:
        updates = _api_get("getUpdates", {"offset": offset, "timeout": 0})
    except Exception as e:
        log.error("Telegram getUpdates 失敗：%s", e)
        return []
    if not isinstance(updates, list):
        return []
    approved = []
    max_uid = offset - 1
    for upd in updates:
        uid = upd.get("update_id", 0)
        max_uid = max(max_uid, uid)
        msg = upd.get("message") or upd.get("channel_post") or {}
        text = (msg.get("text") or "").strip()
        if text.lower().startswith("/approve"):
            parts = text.split(None, 1)
            if len(parts) >= 2:
                story_id = parts[1].strip()
                approved.append(story_id)
                log.info("Telegram 核准：story=%s", story_id)
    if max_uid >= offset:
        _save_offset(max_uid + 1)
    return approved


def run_bot() -> None:
    """M5 起改為輪詢模式，此函式保留以避免呼叫點報錯。"""
    log.info("telegram_bot.run_bot() 已廢棄（M5 改為 get_pending_approvals() 輪詢）")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    send_message("AI Company 機器人連線測試 — 設定正常！")
    print("已傳送測試訊息")
