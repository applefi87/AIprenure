"""
telegram_bot.py — 人類命令入口（M5 填入實作）
===============================================
只做一件事：把人類的 Telegram 指令轉成一筆事件寫進 DB。
不做任何業務邏輯；編排器輪詢 DB 才是動作的觸發點。

支援指令：
  /new <需求文字>     → 建立 story 卡（status=backlog）並寫事件
  /approve <card_id>  → 人類核准，寫 human_approve 事件
  /status [card_id]   → 回報目前狀態（直接查 DB 回覆）
  /pause <card_id>    → 強制把卡轉 paused

M0-M4：stub（不啟動，避免缺 token 報錯）。
M5：替換為真實 python-telegram-bot 實作。
"""

import logging
import os
from pathlib import Path

log = logging.getLogger("telegram_bot")


def run_bot() -> None:
    """
    啟動 Telegram bot。
    M5 填入真實實作：
      from telegram.ext import Application, CommandHandler
      app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
      app.add_handler(CommandHandler("new", cmd_new))
      app.add_handler(CommandHandler("approve", cmd_approve))
      app.add_handler(CommandHandler("status", cmd_status))
      app.add_handler(CommandHandler("pause", cmd_pause))
      app.run_polling()
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN 未設定，telegram_bot 不啟動（M5 前正常）")
        return
    raise NotImplementedError("telegram_bot：M5 尚未實作")


# ─── 指令處理器（M5 填入）────────────────────────────────────────────────

async def cmd_new(update, context) -> None:
    """
    /new <需求文字>
    建立 story 卡（status=backlog），寫 human_new 事件。
    """
    raise NotImplementedError


async def cmd_approve(update, context) -> None:
    """
    /approve <card_id>
    寫 human_approve 事件；編排器看到後執行合併。
    """
    raise NotImplementedError


async def cmd_status(update, context) -> None:
    """
    /status [card_id]
    查 DB 回傳目前狀態，不改任何東西。
    """
    raise NotImplementedError


async def cmd_pause(update, context) -> None:
    """
    /pause <card_id>
    強制把卡轉 paused（人類介入）。
    """
    raise NotImplementedError


if __name__ == "__main__":
    run_bot()
