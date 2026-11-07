#!/usr/bin/env python3
"""
Постоянный снимок Telegram-сессий для восстановления после перезагрузки Mac.

/tmp/claude_tg_sessions.json и tmux живут до перезагрузки, pane id после неё
другие. Снимок лежит вне /tmp и адресуется topic_id — единственным
идентификатором, который переживает перезагрузку:

{
  "<topic_id>": {"agent": "claude", "project": "/abs/path", "session_id": "...",
                 "model": "opus", "effort": "xhigh", "title": "...",
                 "topic_name": "...", "updated_at": 1700000000.0}
}

Запись обновляет watcher для живых pane'ов, удаляет явное закрытие (/close).
Естественная смерть pane запись не трогает — иначе восстанавливать нечего.
"""

import fcntl
import glob
import json
import os
import re
import tempfile
import time

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
RESUME_FILE = os.path.join(STATE_DIR, "resume.json")
CLAUDE_SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")

_SESSION_ID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,63}$")

# Решения resume_decision(); всё, кроме RESTORE, сообщается пользователю.
RESTORE = "restore"
NO_SNAPSHOT = "no_snapshot"
NO_SESSION_ID = "no_session_id"
NO_PROJECT = "no_project"
NO_HISTORY = "no_history"


def _load():
    try:
        with open(RESUME_FILE) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_atomic(data):
    directory = os.path.dirname(RESUME_FILE)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="resume_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.rename(tmp, RESUME_FILE)
    except Exception:
        os.unlink(tmp)
        raise


def _with_lock(fn):
    """fn(data) под exclusive lock; запись только если fn вернула True."""
    os.makedirs(os.path.dirname(RESUME_FILE), mode=0o700, exist_ok=True)
    with open(RESUME_FILE + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load()
        if fn(data):
            _write_atomic(data)


def get(topic_id):
    return _load().get(str(topic_id))


def forget(topic_id):
    """Явное закрытие сессии пользователем: восстанавливать её не нужно."""
    def _do(data):
        return data.pop(str(topic_id), None) is not None
    _with_lock(_do)


def claude_session_id(pane_id, session_name, sessions_dir=CLAUDE_SESSIONS_DIR):
    """sessionId живой Claude-сессии этого pane из ~/.claude/sessions/<pid>.json.

    Поле tmux имеет вид "<session>:@<win>.%<pane>". Сверяем и имя tmux-сессии:
    после перезапуска tmux pane id повторяются, а имена claude-<ns> — нет.
    После --resume или /clear sessionId меняется, поэтому берём свежую запись.
    """
    if not pane_id or not session_name:
        return ""
    best = None
    for path in glob.glob(os.path.join(sessions_dir, "*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        tmux = record.get("tmux")
        if not isinstance(tmux, str):
            continue
        if not tmux.startswith(f"{session_name}:") or not tmux.endswith(f".{pane_id}"):
            continue
        if not _SESSION_ID_RE.match(str(record.get("sessionId") or "")):
            continue
        if best is None or record.get("updatedAt", 0) > best.get("updatedAt", 0):
            best = record
    return best["sessionId"] if best else ""


def build_records(sessions, live_panes, previous, *, sessions_dir=CLAUDE_SESSIONS_DIR, now=None):
    """Новые записи снимка для живых pane'ов.

    sessions — содержимое /tmp/claude_tg_sessions.json, live_panes —
    {pane_id: tmux session name}, previous — текущий снимок. Неизвестный
    session_id не стирает известный: CLI мог ещё не записать свой файл.
    """
    now = time.time() if now is None else now
    records = {}
    for pane_id, topic_id in sessions.get("pane_to_topic", {}).items():
        if not topic_id or pane_id not in live_panes:
            continue
        key = str(topic_id)
        old = previous.get(key) or {}
        agent = sessions.get("pane_to_agent", {}).get(pane_id, "claude")
        session_id = ""
        if agent == "claude":
            session_id = claude_session_id(pane_id, live_panes[pane_id], sessions_dir)
        session_id = (
            session_id
            or sessions.get("pane_to_session_id", {}).get(pane_id, "")
            or (old.get("session_id", "") if old.get("agent") == agent else "")
        )
        record = {
            "agent": agent,
            "project": sessions.get("pane_to_project", {}).get(pane_id) or old.get("project", ""),
            "session_id": session_id,
            "model": sessions.get("pane_to_model", {}).get(pane_id, "") or old.get("model", ""),
            "effort": sessions.get("pane_to_effort", {}).get(pane_id, "") or old.get("effort", ""),
            "title": sessions.get("pane_to_title", {}).get(pane_id, "") or old.get("title", ""),
            "topic_name": sessions.get("pane_to_topic_name", {}).get(pane_id, "") or old.get("topic_name", ""),
        }
        unchanged = {k: v for k, v in old.items() if k != "updated_at"} == record
        record["updated_at"] = old.get("updated_at", now) if unchanged else now
        records[key] = record
    return records


def refresh(sessions, live_panes, *, sessions_dir=CLAUDE_SESSIONS_DIR, now=None):
    """Обновить снимок по живым pane'ам; записи мёртвых pane'ов остаются."""
    def _do(data):
        records = build_records(sessions, live_panes, data, sessions_dir=sessions_dir, now=now)
        changed = any(data.get(key) != value for key, value in records.items())
        data.update(records)
        return changed
    _with_lock(_do)


def resume_decision(record, *, project_exists=os.path.isdir, history_exists=None):
    """Можно ли поднять сессию по записи снимка: одна из констант RESTORE/NO_*."""
    if not record:
        return NO_SNAPSHOT
    session_id = record.get("session_id") or ""
    if not _SESSION_ID_RE.match(session_id):
        return NO_SESSION_ID
    if not record.get("project") or not project_exists(record["project"]):
        return NO_PROJECT
    if history_exists is not None and not history_exists(record):
        return NO_HISTORY
    return RESTORE
