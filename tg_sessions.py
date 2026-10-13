#!/usr/bin/env python3
"""
Shared helper для управления маппингом сессий Claude/Codex <-> Telegram.
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
        data.setdefault("pane_to_topic", {})
        data.setdefault("pane_to_effort", {})
        data.setdefault("pane_to_agent", {})
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


def save_topic(pane_id, topic_id):
    def _do(data):
        data.setdefault("pane_to_topic", {})
        data["pane_to_topic"][pane_id] = int(topic_id)
    _with_lock(_do)


def get_topic(pane_id):
    data = _load()
    return data.get("pane_to_topic", {}).get(pane_id, "")


def get_pane_by_topic(topic_id):
    """Обратный поиск: topic_id → pane_id (для прямых сообщений в топике без reply)."""
    data = _load()
    topic_id = int(topic_id)
    for pane, tid in data.get("pane_to_topic", {}).items():
        if tid == topic_id:
            return pane
    return ""


def save_effort(pane_id, level):
    def _do(data):
        data.setdefault("pane_to_effort", {})
        if level:
            data["pane_to_effort"][pane_id] = level
        else:
            data["pane_to_effort"].pop(pane_id, None)
    _with_lock(_do)


def get_effort(pane_id):
    data = _load()
    return data.get("pane_to_effort", {}).get(pane_id, "")


def save_model(pane_id, alias):
    def _do(data):
        data.setdefault("pane_to_model", {})
        if alias:
            data["pane_to_model"][pane_id] = alias
        else:
            data["pane_to_model"].pop(pane_id, None)
    _with_lock(_do)


def get_model(pane_id):
    data = _load()
    return data.get("pane_to_model", {}).get(pane_id, "")


def save_session_id(pane_id, session_id):
    """Id сессии CLI (Codex thread / Claude session) — для чтения журналов."""
    def _do(data):
        mapping = data.setdefault("pane_to_session_id", {})
        if session_id:
            mapping[pane_id] = session_id
        else:
            mapping.pop(pane_id, None)
    _with_lock(_do)


def get_session_id(pane_id):
    data = _load()
    return data.get("pane_to_session_id", {}).get(pane_id, "")


def start_codex_progress(pane_id, session_id, turn_id, offset):
    """Track only Telegram-initiated Codex turns and survive watcher restarts."""
    def _do(data):
        if data.get("pane_to_agent", {}).get(pane_id) != "codex":
            return
        current = data.setdefault("pane_to_codex_progress", {}).get(pane_id, {})
        if current.get("session_id") == session_id and current.get("active"):
            offset_to_use = min(current["offset"], offset)
            turn_to_use = turn_id or current.get("turn_id", "")
        else:
            offset_to_use = offset
            turn_to_use = turn_id
        data["pane_to_codex_progress"][pane_id] = {
            "session_id": session_id,
            "turn_id": turn_to_use,
            "offset": offset_to_use,
            "active": True,
        }
        data.setdefault("pane_to_session_id", {})[pane_id] = session_id
    _with_lock(_do)


def update_codex_progress(pane_id, session_id, turn_id, offset, *, active=None):
    def _do(data):
        current = data.get("pane_to_codex_progress", {}).get(pane_id)
        if not current or current.get("session_id") != session_id or current.get("turn_id") != turn_id:
            return
        current["offset"] = offset
        if active is not None:
            current["active"] = active
    _with_lock(_do)


def save_agent(pane_id, agent):
    def _do(data):
        data.setdefault("pane_to_agent", {})[pane_id] = agent
    _with_lock(_do)


def get_agent(pane_id):
    data = _load()
    return data.get("pane_to_agent", {}).get(pane_id, "claude")


def save_project(pane_id, project):
    def _do(data):
        data.setdefault("pane_to_project", {})[pane_id] = project
    _with_lock(_do)


def save_topic_name(pane_id, name):
    def _do(data):
        if pane_id in data.get("pane_to_topic", {}):
            data.setdefault("pane_to_topic_name", {})[pane_id] = name
    _with_lock(_do)


def save_startup(pane_id, state):
    def _do(data):
        mapping = data.setdefault("pane_to_startup", {})
        if state is None:
            mapping.pop(pane_id, None)
        else:
            mapping[pane_id] = state
    _with_lock(_do)


def queue_message(pane_id, text, message_id):
    def _do(data):
        pending = data.setdefault("pane_to_pending", {}).setdefault(pane_id, [])
        if not any(item["message_id"] == message_id for item in pending):
            pending.append({"text": text, "message_id": message_id})
    _with_lock(_do)


def pending_messages(pane_id):
    return _load().get("pane_to_pending", {}).get(pane_id, [])


def ack_message(pane_id, message_id):
    def _do(data):
        mapping = data.setdefault("pane_to_pending", {})
        remaining = [item for item in mapping.get(pane_id, []) if item["message_id"] != message_id]
        if remaining:
            mapping[pane_id] = remaining
        else:
            mapping.pop(pane_id, None)
    _with_lock(_do)


def cleanup_pane(pane_id):
    def _do(data):
        data["msg_to_pane"] = {
            k: v for k, v in data["msg_to_pane"].items() if v != pane_id
        }
        data["pane_to_reply"].pop(pane_id, None)
        data.get("pane_to_topic", {}).pop(pane_id, None)
        data.get("pane_to_effort", {}).pop(pane_id, None)
        data.get("pane_to_model", {}).pop(pane_id, None)
        data.get("pane_to_agent", {}).pop(pane_id, None)
        data.get("pane_to_session_id", {}).pop(pane_id, None)
        data.get("pane_to_codex_progress", {}).pop(pane_id, None)
        data.get("pane_to_startup", {}).pop(pane_id, None)
        data.get("pane_to_pending", {}).pop(pane_id, None)
        data.get("pane_to_title", {}).pop(pane_id, None)
        data.get("pane_to_project", {}).pop(pane_id, None)
        data.get("pane_to_topic_name", {}).pop(pane_id, None)
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
    elif cmd == "save_topic" and len(sys.argv) == 4:
        save_topic(sys.argv[2], sys.argv[3])
    elif cmd == "save_effort" and len(sys.argv) == 4:
        save_effort(sys.argv[2], sys.argv[3])
    elif cmd == "get_effort" and len(sys.argv) == 3:
        result = get_effort(sys.argv[2])
        if result:
            print(result)
    elif cmd == "save_model" and len(sys.argv) == 4:
        save_model(sys.argv[2], sys.argv[3])
    elif cmd == "get_model" and len(sys.argv) == 3:
        result = get_model(sys.argv[2])
        if result:
            print(result)
    elif cmd == "get_pane" and len(sys.argv) == 3:
        result = get_pane(sys.argv[2])
        if result:
            print(result)
    elif cmd == "get_reply_to" and len(sys.argv) == 3:
        result = get_reply_to(sys.argv[2])
        if result:
            print(result)
    elif cmd == "get_topic" and len(sys.argv) == 3:
        result = get_topic(sys.argv[2])
        if result:
            print(result)
    elif cmd == "cleanup_pane" and len(sys.argv) == 3:
        cleanup_pane(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
