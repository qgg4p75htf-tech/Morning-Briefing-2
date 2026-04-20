#!/usr/bin/env python3
"""
Analyst Buddy — Chat Bot
Always-on Telegram bot. Listens for messages, replies via Claude.
Runs on Railway as a persistent worker process.
"""

import os
import sys
import time
import requests
import anthropic
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

# ── Config ────────────────────────────────────────────────────────────────────

MODEL          = "claude-sonnet-4-5"
MAX_TOKENS     = 1024
POLL_TIMEOUT   = 30       # long-polling timeout in seconds
TELEGRAM_LIMIT = 4000
MAX_HISTORY    = 20       # messages to keep in memory per user

CET = ZoneInfo("Europe/Copenhagen")

# In-memory conversation history per chat_id
histories: dict[int, list[dict]] = {}

# ── Load system prompt ────────────────────────────────────────────────────────

def load_analyst_prompt() -> str:
    path = Path(__file__).parent / "ANALYST.md"
    if not path.exists():
        print(f"[ERROR] ANALYST.md not found", file=sys.stderr)
        sys.exit(1)
    return path.read_text(encoding="utf-8")

# ── Claude ────────────────────────────────────────────────────────────────────

def ask_claude(system: str, history: list[dict], user_message: str) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    messages = history + [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=messages,
    )

    # Agentic loop for web search
    for _ in range(8):
        if response.stop_reason == "end_turn":
            break
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = [
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": "Search executed.",
                }
                for b in response.content if b.type == "tool_use"
            ]
            messages.append({"role": "user", "content": tool_results})
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=messages,
            )

    text_blocks = [b.text for b in response.content if b.type == "text"]
    return "\n\n".join(text_blocks) if text_blocks else "Sorry, I couldn't generate a response."

# ── Telegram helpers ──────────────────────────────────────────────────────────

def tg(method: str, **kwargs) -> dict:
    token = os.environ["BOT_TOKEN"]
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/{method}",
        json=kwargs,
        timeout=35,
    )
    return resp.json()

def send_message(chat_id: int, text: str) -> None:
    chunks = [text[i:i+TELEGRAM_LIMIT] for i in range(0, len(text), TELEGRAM_LIMIT)]
    for chunk in chunks:
        result = tg("sendMessage", chat_id=chat_id, text=chunk, parse_mode="Markdown")
        # Fallback: retry without Markdown if parse error
        if not result.get("ok"):
            tg("sendMessage", chat_id=chat_id, text=chunk)
        time.sleep(0.3)

def send_typing(chat_id: int) -> None:
    tg("sendChatAction", chat_id=chat_id, action="typing")

# ── Main polling loop ─────────────────────────────────────────────────────────

def main() -> None:
    system_prompt = load_analyst_prompt()
    print(f"[OK] Loaded ANALYST.md ({len(system_prompt)} chars)")
    print("[OK] Bot started — polling for messages...")

    offset = 0

    while True:
        try:
            result = tg("getUpdates", offset=offset, timeout=POLL_TIMEOUT)

            if not result.get("ok"):
                print(f"[WARN] getUpdates error: {result}", flush=True)
                time.sleep(5)
                continue

            for update in result.get("result", []):
                offset = update["update_id"] + 1

                message = update.get("message")
                if not message:
                    continue

                chat_id = message["chat"]["id"]
                text    = message.get("text", "").strip()

                if not text:
                    continue

                # Handle /start command
                if text == "/start":
                    send_message(
                        chat_id,
                        "*Analyst Buddy* is ready.\n\nAsk me anything — markets, macro, stocks, economics. I have web search so I can look up current data."
                    )
                    continue

                # Handle /clear command (reset conversation history)
                if text == "/clear":
                    histories[chat_id] = []
                    send_message(chat_id, "Conversation cleared.")
                    continue

                now = datetime.now(CET).strftime("%H:%M")
                print(f"[{now}] [{chat_id}] {text[:80]}", flush=True)

                # Show typing indicator
                send_typing(chat_id)

                # Get or init history for this chat
                history = histories.get(chat_id, [])

                try:
                    reply = ask_claude(system_prompt, history, text)
                except Exception as e:
                    print(f"[ERROR] Claude: {e}", flush=True)
                    send_message(chat_id, "Sorry, something went wrong. Try again.")
                    continue

                # Update history (keep last MAX_HISTORY messages)
                history.append({"role": "user",      "content": text})
                history.append({"role": "assistant",  "content": reply})
                histories[chat_id] = history[-MAX_HISTORY:]

                send_message(chat_id, reply)
                print(f"  → replied ({len(reply)} chars)", flush=True)

        except requests.exceptions.Timeout:
            # Normal long-poll timeout — just continue
            continue
        except requests.exceptions.RequestException as e:
            print(f"[WARN] Network error: {e} — retrying in 10s", flush=True)
            time.sleep(10)
        except KeyboardInterrupt:
            print("\n[OK] Bot stopped.")
            break
        except Exception as e:
            print(f"[ERROR] Unexpected: {e}", flush=True)
            time.sleep(5)

if __name__ == "__main__":
    for var in ("ANTHROPIC_API_KEY", "BOT_TOKEN"):
        if not os.environ.get(var):
            print(f"[ERROR] Missing env var: {var}", file=sys.stderr)
            sys.exit(1)
    main()
