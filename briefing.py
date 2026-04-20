#!/usr/bin/env python3
"""
Analyst Buddy — Morning Briefing
Runs via GitHub Actions cron at 07:00 CET.
Reads PROMPT.md, calls Claude with web_search, sends to Telegram.
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

MODEL       = "claude-sonnet-4-5"
MAX_TOKENS  = 4096
MAX_LOOPS   = 10          # agentic tool-use guard
TELEGRAM_LIMIT = 4000     # Telegram message character cap

CET = ZoneInfo("Europe/Copenhagen")

# ── Load prompt ───────────────────────────────────────────────────────────────

def load_system_prompt() -> str:
    prompt_path = Path(__file__).parent / "PROMPT.md"
    if not prompt_path.exists():
        print(f"[ERROR] PROMPT.md not found at {prompt_path}", file=sys.stderr)
        sys.exit(1)
    return prompt_path.read_text(encoding="utf-8")

# ── Claude agentic loop ───────────────────────────────────────────────────────

def run_briefing(system_prompt: str) -> str:
    """
    Calls Claude with web_search enabled.
    Runs the tool-use agentic loop until stop_reason == 'end_turn'.
    Returns the final text content of the briefing.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    now_cet = datetime.now(CET)
    date_str = now_cet.strftime("%A, %d %B %Y")
    time_str = now_cet.strftime("%H:%M CET")

    user_message = (
        f"Today is {date_str}, {time_str}. "
        "Generate my morning briefing. Search aggressively — "
        "run at least 8 web searches before writing the final briefing. "
        "Cover all sections in the system prompt."
    )

    messages = [{"role": "user", "content": user_message}]

    tools = [{"type": "web_search_20250305", "name": "web_search"}]

    print(f"[{time_str}] Starting briefing generation...", flush=True)

    for loop_i in range(MAX_LOOPS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )

        print(f"  Loop {loop_i + 1}: stop_reason={response.stop_reason}, "
              f"blocks={[b.type for b in response.content]}", flush=True)

        # ── Finished: extract final text ──────────────────────────────────────
        if response.stop_reason == "end_turn":
            text_blocks = [b.text for b in response.content if b.type == "text"]
            if not text_blocks:
                raise ValueError("Claude returned end_turn with no text block")
            return "\n\n".join(text_blocks)

        # ── Tool use: append assistant turn + dummy tool results ──────────────
        if response.stop_reason == "tool_use":
            # Append the full assistant content (including tool_use blocks)
            messages.append({"role": "assistant", "content": response.content})

            # Append tool_result for every tool_use block
            tool_results = [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Search executed successfully.",
                }
                for block in response.content
                if block.type == "tool_use"
            ]
            messages.append({"role": "user", "content": tool_results})
            continue

        # ── Unexpected stop reason ────────────────────────────────────────────
        raise ValueError(f"Unexpected stop_reason: {response.stop_reason}")

    raise RuntimeError(f"Agentic loop exceeded {MAX_LOOPS} iterations without end_turn")

# ── Telegram delivery ─────────────────────────────────────────────────────────

def send_telegram(text: str) -> None:
    """
    Sends the briefing to Telegram, splitting into chunks if > TELEGRAM_LIMIT chars.
    Uses parse_mode=Markdown for bold/italic formatting.
    """
    bot_token = os.environ["BOT_TOKEN"]
    chat_id   = os.environ["CHAT_ID"]
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    # Split on paragraph boundaries to avoid cutting mid-sentence
    chunks = _split_smart(text, TELEGRAM_LIMIT)

    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id":    chat_id,
            "text":       chunk,
            "parse_mode": "Markdown",
        }
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            # Fallback: retry without Markdown in case of parse errors
            payload["parse_mode"] = ""
            resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        print(f"  Telegram chunk {i+1}/{len(chunks)} sent ({len(chunk)} chars)", flush=True)

        if i < len(chunks) - 1:
            time.sleep(0.5)   # Avoid hitting Telegram rate limits

def _split_smart(text: str, limit: int) -> list[str]:
    """
    Splits text into chunks at paragraph boundaries, respecting `limit`.
    Falls back to hard split if a single paragraph exceeds limit.
    """
    paragraphs = text.split("\n\n")
    chunks, current = [], ""

    for para in paragraphs:
        candidate = (current + "\n\n" + para).strip() if current else para
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # If a single paragraph is too long, hard-split it
            if len(para) > limit:
                for j in range(0, len(para), limit):
                    chunks.append(para[j:j+limit])
                current = ""
            else:
                current = para

    if current:
        chunks.append(current)

    return chunks

# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("  Analyst Buddy — Morning Briefing")
    print(f"  {datetime.now(CET).strftime('%Y-%m-%d %H:%M CET')}")
    print("=" * 60)

    # Validate required env vars
    for var in ("ANTHROPIC_API_KEY", "BOT_TOKEN", "CHAT_ID"):
        if not os.environ.get(var):
            print(f"[ERROR] Missing environment variable: {var}", file=sys.stderr)
            sys.exit(1)

    system_prompt = load_system_prompt()
    print(f"[OK] Loaded PROMPT.md ({len(system_prompt)} chars)")

    briefing = run_briefing(system_prompt)
    print(f"[OK] Briefing generated ({len(briefing)} chars)")

    send_telegram(briefing)
    print("[OK] Briefing delivered to Telegram")
    print("=" * 60)

if __name__ == "__main__":
    main()
