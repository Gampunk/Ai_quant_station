#!/usr/bin/env python3
"""
Quick Telegram Bot Test
=======================
Run this to verify your BOT_TOKEN and CHAT_ID are configured correctly.

Usage:
    cd backend/
    python -m scripts.test_telegram
"""
import os
import sys
import json
import urllib.request
from pathlib import Path

# Add parent to path for config import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_telegram():
    try:
        from app.core.config import settings
        token = settings.TELEGRAM_BOT_TOKEN
        chat_id = settings.TELEGRAM_CHAT_ID
    except ImportError:
        print("[FAIL] Cannot import settings — check your .env file")
        return False

    if not token:
        print("[FAIL] TELEGRAM_BOT_TOKEN is empty — add it to backend/.env")
        print("       Create bot via @BotFather on Telegram → /newbot")
        return False

    if not chat_id:
        print("[FAIL] TELEGRAM_CHAT_ID is empty — add it to backend/.env")
        print("       1) Send any message to your bot")
        print("       2) Visit: https://api.telegram.org/bot{TOKEN}/getUpdates")
        print("       3) Find chat.id in the response")
        return False

    api_base = f"https://api.telegram.org/bot{token}"

    # Test 1: Get bot info
    print(f"Testing bot token... ", end="")
    try:
        req = urllib.request.Request(f"{api_base}/getMe")
        resp = urllib.request.urlopen(req, timeout=10)
        bot_info = json.loads(resp.read())
        print(f"OK — @{bot_info['result']['username']}")
    except Exception as e:
        print(f"FAIL — {e}")
        print("       Check your TELEGRAM_BOT_TOKEN")
        return False

    # Test 2: Send test message
    print(f"Sending test message to chat {chat_id}... ", end="")
    try:
        text = "✅ *Impulse Analyst*\n_Telegram bot connected successfully!_\n\nReports will be delivered here."
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }).encode()
        req = urllib.request.Request(
            f"{api_base}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15)
        print("OK")
    except Exception as e:
        print(f"FAIL — {e}")
        print("       Check your TELEGRAM_CHAT_ID")
        return False

    print("\n[DONE] Telegram is configured correctly! You should see the test message in your chat.")
    return True


if __name__ == "__main__":
    success = test_telegram()
    sys.exit(0 if success else 1)
