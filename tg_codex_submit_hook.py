#!/usr/bin/env python3
"""Register Telegram prompts when Codex begins submitting them."""

import json
import os
import re
import sys

import tg_sessions
import tg_stats


PROMPT_MARKER = "[TG | ~/.claude/tg-integration]"
_MESSAGE_ID = re.compile(r"^\[TG \| ~/\.claude/tg-integration\] \[tg-message-id:(\d+)\](?:\s|$)")
REACTION_READING = "👀"


def message_id_from_prompt(prompt):
    match = _MESSAGE_ID.match(prompt)
    return int(match.group(1)) if match else None


def mark_prompt_accepted(payload, pane_id=None):
    pane_id = pane_id or os.environ.get("TMUX_PANE", "")
    if payload.get("hook_event_name") != "UserPromptSubmit" or not pane_id:
        return False
    message_id = message_id_from_prompt(payload.get("prompt", ""))
    if message_id is None or tg_sessions.get_agent(pane_id) != "codex":
        return False
    if not tg_sessions.get_topic(pane_id):
        return False
    session_id = payload.get("session_id", "")
    path = tg_stats.find_codex_rollout(session_id)
    if not path:
        return False
    offset = os.path.getsize(path)
    tg_sessions.start_codex_progress(pane_id, session_id, payload.get("turn_id", ""), offset)
    return True


def main():
    try:
        payload = json.load(sys.stdin)
        mark_prompt_accepted(payload)
    except (OSError, ValueError, TypeError):
        # Hooks must never block the user's turn because Telegram is unavailable.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
