"""Telegram 通知工具，后台线程发送，不阻塞主循环。"""
import json
import logging
import threading
import urllib.request
import urllib.parse

import config

logger = logging.getLogger(__name__)


def send_tg(text: str) -> None:
    """异步发送 Telegram 消息，失败只记日志不抛异常。"""
    token = getattr(config, "TG_BOT_TOKEN", "")
    chat_id = getattr(config, "TG_CHAT_ID", "")
    if not token or not chat_id:
        return
    threading.Thread(target=_send, args=(token, chat_id, text), daemon=True).start()


def _send(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10):
            pass
    except Exception as e:
        logger.warning(f"[TG通知] 发送失败: {e}")
