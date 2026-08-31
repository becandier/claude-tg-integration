#!/usr/bin/env python3
"""Forward Codex's agent-turn-complete payload to the shared Telegram hook."""

import os
import json
import subprocess
import sys


def main(argv=None, runner=subprocess.run):
    args = sys.argv if argv is None else argv
    if len(args) < 2 or not args[-1]:
        return 2

    try:
        payload = json.loads(args[-1])
    except (json.JSONDecodeError, TypeError):
        return 2
    if "last-assistant-message" in payload:
        payload["last_assistant_message"] = payload.pop("last-assistant-message")
    normalized_payload = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    stop_hook = os.path.join(os.path.dirname(__file__), "tg_stop_hook.sh")
    runner(
        ["bash", stop_hook],
        input=normalized_payload,
        text=True,
        check=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
