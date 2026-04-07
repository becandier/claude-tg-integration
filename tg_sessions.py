#!/usr/bin/env python3
"""
Shared helper для управления маппингом сессий Claude Code <-> Telegram.
Атомарная запись + file locking. Используется хуками и watcher-ом.

Структура JSON:
{
  "msg_to_pane":    {"<tg_msg_id>": "<tmux_pane_id>", ...},
  "pane_to_reply":  {"<tmux_pane_id>": <user_tg_msg_id>, ...}
}

CLI:
  python3 tg_sessions.py save_route <msg_id> <pane_id>
  python3 tg_sessions.py save_reply_to <pane_id> <user_msg_id>
  python3 tg_sessions.py get_pane <msg_id>
  python3 tg_sessions.py get_reply_to <pane_id>
  python3 tg_sessions.py cleanup_pane <pane_id>
"""

import fcntl
import json
import os
import sys
import tempfile

SESSIONS_FILE = "/tmp/claude_tg_sessions.json"
MAX_ROUTES = 100


def _write_atomic(data):
    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix="claude_tg_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.rename(tmp, SESSIONS_FILE)
    except Exception:
        os.unlink(tmp)
        raise


def _load():
    try:
        with open(SESSIONS_FILE) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def _save(data):
    mtp = data.get("msg_to_pane", {})
    if len(mtp) > MAX_ROUTES:
        keys = sorted(mtp.keys(), key=lambda x: int(x) if x.isdigit() else 0)
        for k in keys[:-MAX_ROUTES]:
            del mtp[k]
    _write_atomic(data)


def _with_lock(fn):
    """Выполнить fn(data) с exclusive lock, записать результат."""
    lock_path = SESSIONS_FILE + ".lock"
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load()
        data.setdefault("msg_to_pane", {})
        data.setdefault("pane_to_reply", {})
        fn(data)
        _save(data)


def save_route(msg_id, pane_id):
    def _do(data):
        data["msg_to_pane"][str(msg_id)] = pane_id
    _with_lock(_do)


def save_reply_to(pane_id, user_msg_id):
    def _do(data):
        data["pane_to_reply"][pane_id] = int(user_msg_id)
    _with_lock(_do)


def get_pane(msg_id):
    data = _load()
    return data.get("msg_to_pane", {}).get(str(msg_id), "")


def get_reply_to(pane_id):
    data = _load()
    return data.get("pane_to_reply", {}).get(pane_id, "")


def cleanup_pane(pane_id):
    def _do(data):
        data["msg_to_pane"] = {
            k: v for k, v in data["msg_to_pane"].items() if v != pane_id
        }
        data["pane_to_reply"].pop(pane_id, None)
    _with_lock(_do)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: tg_sessions.py <command> [args...]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "save_route" and len(sys.argv) == 4:
        save_route(sys.argv[2], sys.argv[3])
    elif cmd == "save_reply_to" and len(sys.argv) == 4:
        save_reply_to(sys.argv[2], sys.argv[3])
    elif cmd == "get_pane" and len(sys.argv) == 3:
        result = get_pane(sys.argv[2])
        if result:
            print(result)
    elif cmd == "get_reply_to" and len(sys.argv) == 3:
        result = get_reply_to(sys.argv[2])
        if result:
            print(result)
    elif cmd == "cleanup_pane" and len(sys.argv) == 3:
        cleanup_pane(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
