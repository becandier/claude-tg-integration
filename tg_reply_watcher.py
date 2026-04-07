#!/usr/bin/env python3
"""
Telegram Reply Watcher for Claude Code (multi-session)

Поллит Telegram на reply-сообщения. По message_id определяет,
в какой tmux pane отправить текст.

Использование:
  python3 -u ~/.claude/tg-integration/tg_reply_watcher.py
"""

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from logging.handlers import RotatingFileHandler

# Загружаем конфиг
def _load_config():
    config = {}
    config_path = os.path.expanduser("~/.claude/tg-integration/tg_config.sh")
    with open(config_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            config[key.strip()] = val.strip().strip('"')
    return config

_cfg = _load_config()
BOT_TOKEN = _cfg["TG_BOT_TOKEN"]
CHAT_ID = int(_cfg["TG_CHAT_ID"])
USER_ID = int(_cfg.get("TG_USER_ID", _cfg["TG_CHAT_ID"]))
OFFSET_FILE = "/tmp/claude_tg_watcher_offset"
PID_FILE = "/tmp/claude_tg_watcher.pid"
LOG_FILE = "/tmp/claude_tg_watcher.log"
WHISPER_MODEL = os.path.expanduser("~/.claude/tg-integration/whisper-models/ggml-medium.bin")
PROJECTS_ROOT = os.path.expanduser("~/projects")
TERMINAL_MODE = _cfg.get("TERMINAL_MODE", "window")
TERMINAL_APP = _cfg.get("TERMINAL_APP", "iterm")
MAX_PROJECT_DEPTH = 4
PROJECT_MARKERS = {
    ".git", "pubspec.yaml", "package.json", "Cargo.toml",
    "go.mod", "pyproject.toml", "Makefile", "CMakeLists.txt", "pom.xml",
}

# Импортируем shared-модуль
sys.path.insert(0, os.path.expanduser("~/.claude/tg-integration"))
import tg_sessions

logger = logging.getLogger("watcher")


def setup_logging():
    handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(message)s", datefmt="%H:%M:%S"
    ))
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)


def tg_api(method, params=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode() if params else None
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.error(f"[TG API] {method}: {e}")
        return {"ok": False}


# Палитра цветов для Forum Topics (ротация)
TOPIC_COLORS = [7322096, 16766590, 13338331, 9367192, 16749490, 16478047]
_topic_color_idx = 0


def create_forum_topic(name):
    """Создаёт Forum Topic в супергруппе, возвращает message_thread_id или None."""
    global _topic_color_idx
    color = TOPIC_COLORS[_topic_color_idx % len(TOPIC_COLORS)]
    _topic_color_idx += 1
    resp = tg_api("createForumTopic", {
        "chat_id": CHAT_ID,
        "name": name[:128],
        "icon_color": color,
    })
    if resp.get("ok"):
        return resp["result"]["message_thread_id"]
    logger.error(f"[Topic] Failed to create: {resp}")
    return None


def tg_send(text, topic_id=None, reply_to=None, parse_mode=None, reply_markup=None):
    """Отправка сообщения с поддержкой Forum Topics."""
    params = {"chat_id": CHAT_ID, "text": text}
    if topic_id:
        params["message_thread_id"] = topic_id
    if reply_to:
        params["reply_to_message_id"] = reply_to
        params["allow_sending_without_reply"] = "true"
    if parse_mode:
        params["parse_mode"] = parse_mode
    if reply_markup:
        params["reply_markup"] = reply_markup if isinstance(reply_markup, str) else json.dumps(reply_markup)
    return tg_api("sendMessage", params)


def tg_edit(message_id, text, parse_mode=None, reply_markup=None):
    """Редактирование сообщения."""
    params = {"chat_id": CHAT_ID, "message_id": message_id, "text": text}
    if parse_mode:
        params["parse_mode"] = parse_mode
    if reply_markup:
        params["reply_markup"] = reply_markup if isinstance(reply_markup, str) else json.dumps(reply_markup)
    return tg_api("editMessageText", params)


def get_msg_topic(msg):
    """Извлекает message_thread_id из входящего сообщения (если из топика)."""
    return msg.get("message_thread_id")


def tg_react(message_id, emoji="\u26a1"):
    """Ставит реакцию на сообщение как подтверждение доставки."""
    params = {
        "chat_id": CHAT_ID,
        "message_id": message_id,
        "reaction": json.dumps([{"type": "emoji", "emoji": emoji}]),
    }
    return tg_api("setMessageReaction", params)


def tg_download_file(file_id, dest_path):
    """Скачивает файл из Telegram по file_id."""
    info = tg_api("getFile", {"file_id": file_id})
    if not info.get("ok"):
        return False
    file_path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    try:
        urllib.request.urlretrieve(url, dest_path)
        return True
    except Exception as e:
        logger.error(f"[Download] {e}")
        return False


def transcribe_voice(file_id):
    """Скачивает голосовое, конвертирует в WAV, распознаёт через whisper-cpp."""
    if not os.path.exists(WHISPER_MODEL):
        logger.warning("Whisper model not found, skipping voice")
        return None

    tmp_dir = tempfile.mkdtemp(prefix="claude_voice_")
    oga_path = os.path.join(tmp_dir, "voice.oga")
    wav_path = os.path.join(tmp_dir, "voice.wav")

    try:
        if not tg_download_file(file_id, oga_path):
            return None

        # Конвертируем в WAV 16kHz mono (формат whisper-cpp)
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", oga_path, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.error(f"[ffmpeg] {result.stderr[:200]}")
            return None

        # Распознаём через whisper-cli
        result = subprocess.run(
            ["whisper-cli", "-m", WHISPER_MODEL, "-l", "ru", "-np", "-nt", "-f", wav_path],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            logger.error(f"[whisper] {result.stderr[:200]}")
            return None

        text = result.stdout.strip()
        if text:
            logger.info(f"[Voice] Transcribed: {text[:100]}")
        return text if text else None
    except subprocess.TimeoutExpired:
        logger.error("[Voice] Timeout during transcription")
        return None
    except Exception as e:
        logger.error(f"[Voice] {e}")
        return None
    finally:
        for f in (oga_path, wav_path):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


def get_offset():
    try:
        with open(OFFSET_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_offset(offset):
    with open(OFFSET_FILE, "w") as f:
        f.write(str(offset))


def init_offset():
    """При первом запуске пропускаем старые сообщения."""
    if os.path.exists(OFFSET_FILE):
        return
    data = tg_api("getUpdates", {"offset": -1})
    results = data.get("result", [])
    if results:
        save_offset(results[-1]["update_id"] + 1)
    else:
        save_offset(0)


def open_terminal_with_tmux(session_name):
    """Открывает iTerm2 или Terminal.app с tmux attach (window или tab)."""
    if TERMINAL_MODE == "none":
        return
    cmd = f"/opt/homebrew/bin/tmux attach -t {session_name}"

    if TERMINAL_APP == "iterm":
        if TERMINAL_MODE == "tab":
            script = f'''
tell application "iTerm2"
    activate
    if (count of windows) > 0 then
        tell current window
            create tab with default profile command "{cmd}"
        end tell
    else
        create window with default profile command "{cmd}"
    end if
end tell'''
        else:
            script = f'''
tell application "iTerm2"
    activate
    create window with default profile command "{cmd}"
end tell'''
    else:
        if TERMINAL_MODE == "tab":
            script = f'''
tell application "Terminal"
    activate
    if (count of windows) > 0 then
        tell application "System Events"
            keystroke "t" using command down
        end tell
        delay 0.3
        do script "{cmd}" in front window
    else
        do script "{cmd}"
    end if
end tell'''
        else:
            script = f'''
tell application "Terminal"
    activate
    do script "{cmd}"
end tell'''

    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info(f"[Terminal] opened {TERMINAL_APP}/{TERMINAL_MODE} for {session_name}")
    except Exception as e:
        logger.error(f"[Terminal] {e}")


def pane_exists(pane_id):
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
        capture_output=True, text=True,
    )
    return pane_id in result.stdout.split()


def tmux_send(pane_id, text):
    """Отправляет текст + Enter в tmux pane."""
    if not pane_exists(pane_id):
        return False
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", text])
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"])
    return True


def tmux_send_keys(pane_id, *keys):
    """Отправляет raw-клавиши (не литеральный текст) в tmux pane."""
    if not pane_exists(pane_id):
        return False
    for key in keys:
        subprocess.run(["tmux", "send-keys", "-t", pane_id, key])
    return True


def is_permission_prompt(pane_id):
    """Проверяет, показан ли в pane интерактивный permission-промпт Claude Code."""
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-10"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    return "Esc to cancel" in result.stdout


def write_pid():
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def cleanup_pid(*_):
    try:
        os.unlink(PID_FILE)
    except FileNotFoundError:
        pass
    sys.exit(0)


# ── Команды бота ──────────────────────────────────────────────

def handle_newstart(msg, text):
    """Создаёт новую tmux-сессию с Claude Code."""
    parts = text.split(maxsplit=1)
    project = parts[1].strip() if len(parts) > 1 else None
    user_msg_id = msg.get("message_id")
    src_topic = get_msg_topic(msg)

    if not project:
        handle_projects(msg)
        return

    if project == "__home__":
        project_dir = os.path.expanduser("~")
    elif project == "__projects__":
        project_dir = PROJECTS_ROOT
    else:
        project_dir = os.path.join(PROJECTS_ROOT, project)
    if not os.path.isdir(project_dir):
        tg_send(f"❌ Проект не найден: {project}", topic_id=src_topic, reply_to=user_msg_id)
        return

    session_name = f"claude-{int(time.time())}"
    project_name = os.path.basename(project_dir)

    tg_react(user_msg_id, "⚡")

    # Создаём Forum Topic для этой сессии
    topic_id = create_forum_topic(f"🚀 {project_name}")
    if not topic_id:
        tg_send("❌ Не удалось создать топик", topic_id=src_topic, reply_to=user_msg_id)
        return

    shell_cmd = (
        f"claude --dangerously-skip-permissions; "
        f"tmux wait-for -S {session_name}-done"
    )
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", project_dir,
         f"zsh -l -c '{shell_cmd}'"],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        tg_send(
            f"❌ Не удалось создать сессию:\n{result.stderr[:200]}",
            topic_id=topic_id, reply_to=user_msg_id,
        )
        return

    result = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_id}"],
        capture_output=True, text=True,
    )
    pane_id = result.stdout.strip().split("\n")[0] if result.stdout.strip() else None

    if not pane_id:
        tg_send("❌ Сессия создана, но не удалось получить pane_id", topic_id=topic_id)
        return

    # Сохраняем маппинг pane → topic
    tg_sessions.save_topic(pane_id, topic_id)

    response = tg_send(
        f"🚀 Claude Code запущен\nПроект: {project_name}\nСессия: {session_name}",
        topic_id=topic_id,
    )

    if response.get("ok"):
        bot_msg_id = str(response["result"]["message_id"])
        tg_sessions.save_route(bot_msg_id, pane_id)

    tg_sessions.save_reply_to(pane_id, user_msg_id)
    open_terminal_with_tmux(session_name)
    logger.info(f"[NewStart] session={session_name} pane={pane_id} topic={topic_id} project={project_name}")


def _is_project(path):
    """Проверяет, является ли директория проектом (содержит маркерный файл)."""
    try:
        entries = os.listdir(path)
    except PermissionError:
        return False
    return bool(PROJECT_MARKERS & set(entries))


def _has_nested_projects(path, depth=0):
    """Проверяет, есть ли вложенные проекты (рекурсивно до MAX_PROJECT_DEPTH)."""
    if depth >= MAX_PROJECT_DEPTH:
        return False
    try:
        entries = os.listdir(path)
    except PermissionError:
        return False
    for entry in entries:
        if entry.startswith("."):
            continue
        full = os.path.join(path, entry)
        if not os.path.isdir(full):
            continue
        if _is_project(full):
            return True
        if _has_nested_projects(full, depth + 1):
            return True
    return False


def _folder_keyboard(rel_path=""):
    """Инлайн-клавиатура для папки (drill-down навигация).

    rel_path — путь относительно PROJECTS_ROOT ("" = корень).
    """
    abs_path = os.path.join(PROJECTS_ROOT, rel_path) if rel_path else PROJECTS_ROOT
    if not os.path.isdir(abs_path):
        return []

    # Глубина текущей папки относительно PROJECTS_ROOT
    depth = len(rel_path.split("/")) if rel_path else 0

    keyboard = []

    # Кнопка "Назад" и "Выбрать эту папку" если не в корне
    if rel_path:
        parent = "/".join(rel_path.split("/")[:-1])
        keyboard.append([{"text": "← Назад", "callback_data": f"nav:{parent}"}])
        keyboard.append([{"text": "📌 Выбрать эту папку", "callback_data": f"start:{rel_path}"}])

    # Специальные кнопки только в корне
    if not rel_path:
        keyboard.append([
            {"text": "🏠 Домашняя директория", "callback_data": "start:__home__"},
            {"text": "🆕 ~/projects", "callback_data": "start:__projects__"},
        ])

    try:
        entries = sorted(os.listdir(abs_path))
    except PermissionError:
        return keyboard

    row = []
    for entry in entries:
        if entry.startswith("."):
            continue
        full = os.path.join(abs_path, entry)
        if not os.path.isdir(full):
            continue

        entry_rel = f"{rel_path}/{entry}" if rel_path else entry
        is_proj = _is_project(full)
        has_nested = depth < MAX_PROJECT_DEPTH and _has_nested_projects(full, depth)

        if is_proj and has_nested:
            # Проект с вложенными — показываем обе кнопки на отдельной строке
            if row:
                keyboard.append(row)
                row = []
            keyboard.append([
                {"text": f"📂 {entry}", "callback_data": f"start:{entry_rel}"},
                {"text": f"📁 {entry} →", "callback_data": f"nav:{entry_rel}"},
            ])
            continue
        elif is_proj:
            row.append({"text": f"📂 {entry}", "callback_data": f"start:{entry_rel}"})
        elif has_nested:
            row.append({"text": f"📁 {entry} →", "callback_data": f"nav:{entry_rel}"})
        else:
            continue

        if len(row) == 2:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)
    return keyboard


def _project_keyboard():
    """Инлайн-клавиатура с проектами (гибрид: проекты + drill-down)."""
    return _folder_keyboard("")


def handle_projects(msg):
    """Список проектов с инлайн-кнопками."""
    user_msg_id = msg.get("message_id")
    topic_id = get_msg_topic(msg)

    if not os.path.isdir(PROJECTS_ROOT):
        tg_send("📁 ~/projects не найдена", topic_id=topic_id, reply_to=user_msg_id)
        return

    keyboard = _project_keyboard()
    if not keyboard:
        tg_send("📁 Нет проектов в ~/projects", topic_id=topic_id, reply_to=user_msg_id)
        return

    tg_send(
        "📁 Выбери проект:",
        topic_id=topic_id,
        reply_to=user_msg_id,
        reply_markup={"inline_keyboard": keyboard},
    )


def handle_sessions(msg):
    """Показывает активные tmux-сессии с Claude Code."""
    user_msg_id = msg.get("message_id")
    topic_id = get_msg_topic(msg)
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name} #{session_path}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        tg_send("📋 Нет активных сессий", topic_id=topic_id, reply_to=user_msg_id)
        return

    lines = ["📋 <b>Активные сессии:</b>\n"]
    for line in result.stdout.strip().split("\n"):
        parts = line.split(maxsplit=1)
        name = parts[0]
        path = os.path.basename(parts[1]) if len(parts) > 1 else ""
        if name.startswith("claude-"):
            lines.append(f"▸ <code>{name}</code> — {path}")

    if len(lines) == 1:
        lines.append("Нет сессий Claude Code")

    tg_send(
        "\n".join(lines),
        topic_id=topic_id,
        reply_to=user_msg_id,
        parse_mode="HTML",
    )


def close_session(session_name):
    """Gracefully close a Claude Code tmux session."""
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_id}"],
        capture_output=True, text=True,
    )
    pane_id = result.stdout.strip().split("\n")[0] if result.stdout.strip() else None

    try:
        subprocess.run(["tmux", "send-keys", "-t", session_name, "C-c"], timeout=2)
        time.sleep(0.3)
        subprocess.run(["tmux", "send-keys", "-t", session_name, "-l", "/exit"], timeout=2)
        subprocess.run(["tmux", "send-keys", "-t", session_name, "Enter"], timeout=2)
        for _ in range(6):
            time.sleep(0.5)
            check = subprocess.run(
                ["tmux", "has-session", "-t", session_name],
                capture_output=True, text=True,
            )
            if check.returncode != 0:
                break
    except Exception as e:
        logger.warning(f"[Close] graceful exit failed for {session_name}: {e}")

    subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        capture_output=True, text=True,
    )

    if pane_id:
        tg_sessions.cleanup_pane(pane_id)
    logger.info(f"[Close] session {session_name} closed")


def close_topic(topic_id):
    """Закрывает Forum Topic."""
    resp = tg_api("closeForumTopic", {
        "chat_id": CHAT_ID,
        "message_thread_id": topic_id,
    })
    if resp.get("ok"):
        logger.info(f"[Topic] Closed topic {topic_id}")
    else:
        logger.error(f"[Topic] Failed to close {topic_id}: {resp}")


def close_session_by_pane(pane_id):
    """Закрывает tmux-сессию по pane_id."""
    # Находим имя сессии по pane
    result = subprocess.run(
        ["tmux", "display-message", "-t", pane_id, "-p", "#{session_name}"],
        capture_output=True, text=True,
    )
    session_name = result.stdout.strip()
    if session_name:
        close_session(session_name)
    else:
        tg_sessions.cleanup_pane(pane_id)


def handle_close(msg):
    """Закрывает сессию. В топике — закрывает эту сессию + топик. В General — список."""
    user_msg_id = msg.get("message_id")
    topic_id = get_msg_topic(msg)

    # Если /close из топика — закрыть сессию этого топика
    if topic_id:
        pane_id = tg_sessions.get_pane_by_topic(topic_id)
        if pane_id:
            tg_send("🔴 Закрываю сессию...", topic_id=topic_id)
            close_session_by_pane(pane_id)
            tg_send("🔴 Сессия закрыта", topic_id=topic_id)
            close_topic(topic_id)
        else:
            tg_send("⚠️ Нет активной сессии в этом топике", topic_id=topic_id)
            close_topic(topic_id)
        return
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name} #{session_path}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        tg_send("📋 Нет активных сессий", topic_id=topic_id, reply_to=user_msg_id)
        return

    keyboard = []
    for line in result.stdout.strip().split("\n"):
        parts = line.split(maxsplit=1)
        name = parts[0]
        path = os.path.basename(parts[1]) if len(parts) > 1 else ""
        if name.startswith("claude-"):
            keyboard.append([{
                "text": f"❌ {name} — {path}",
                "callback_data": f"close:{name}",
            }])

    if not keyboard:
        tg_send("📋 Нет активных сессий Claude Code", topic_id=topic_id, reply_to=user_msg_id)
        return

    if len(keyboard) > 1:
        keyboard.append([{
            "text": "🔴 Закрыть все",
            "callback_data": "close:__all__",
        }])

    tg_send(
        "🔴 Выбери сессию для закрытия:",
        topic_id=topic_id,
        reply_to=user_msg_id,
        reply_markup={"inline_keyboard": keyboard},
    )


def handle_callback(callback_query):
    """Обработка нажатий инлайн-кнопок."""
    cb_id = callback_query.get("id")
    data = callback_query.get("data", "")
    msg = callback_query.get("message", {})

    if data.startswith("approve:") or data.startswith("reject:"):
        pane_id = data.split(":", 1)[1]
        is_approve = data.startswith("approve:")
        chat_msg_id = msg.get("message_id")
        original_text = msg.get("text", "")

        if not pane_exists(pane_id):
            tg_api("answerCallbackQuery", {
                "callback_query_id": cb_id,
                "text": "⚠️ Сессия не найдена",
                "show_alert": True,
            })
            tg_edit(chat_msg_id, f"{original_text}\n\n⚠️ Сессия не найдена")
            return

        if not is_permission_prompt(pane_id):
            tg_api("answerCallbackQuery", {
                "callback_query_id": cb_id,
                "text": "⚠️ Сейчас нет permission-промпта",
                "show_alert": True,
            })
            return

        if is_approve:
            tmux_send_keys(pane_id, "Enter")
            label = "✅ Принято"
        else:
            tmux_send_keys(pane_id, "Escape")
            label = "❌ Отклонено"

        tg_api("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": label,
        })
        tg_edit(chat_msg_id, f"{original_text}\n\n{label}")
        logger.info(f"[Callback] {label} -> pane {pane_id}")
    elif data.startswith("start:"):
        project = data[6:]
        label = "домашняя директория" if project == "__home__" else project
        tg_api("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": f"Запускаю {label}...",
        })
        handle_newstart(msg, f"/newstart {project}")
    elif data.startswith("nav:"):
        rel_path = data[4:]  # может быть "" для корня
        tg_api("answerCallbackQuery", {"callback_query_id": cb_id})
        chat_msg_id = msg.get("message_id")

        keyboard = _folder_keyboard(rel_path)
        folder_name = os.path.basename(rel_path) if rel_path else "проекты"
        tg_edit(
            chat_msg_id,
            f"📁 {folder_name}:",
            reply_markup={"inline_keyboard": keyboard},
        )
    elif data.startswith("close:"):
        session = data[6:]
        tg_api("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": "Закрываю...",
        })
        chat_msg_id = msg.get("message_id")

        if session == "__all__":
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True,
            )
            closed = []
            if result.returncode == 0:
                for name in result.stdout.strip().split("\n"):
                    if name.startswith("claude-"):
                        close_session(name)
                        closed.append(name)
            text = f"🔴 Закрыто сессий: {len(closed)}" if closed else "Нет сессий для закрытия"
        else:
            close_session(session)
            text = f"🔴 Сессия {session} закрыта"

        tg_edit(chat_msg_id, text)
    else:
        tg_api("answerCallbackQuery", {"callback_query_id": cb_id})


def main():
    setup_logging()
    write_pid()
    signal.signal(signal.SIGTERM, cleanup_pid)
    signal.signal(signal.SIGINT, cleanup_pid)
    init_offset()

    # Регистрируем команды бота (кнопка Menu в чате)
    tg_api("setMyCommands", {
        "commands": json.dumps([
            {"command": "newstart", "description": "🚀 Новая сессия Claude Code"},
            {"command": "projects", "description": "📁 Список проектов"},
            {"command": "sessions", "description": "📋 Активные сессии"},
            {"command": "close", "description": "🔴 Закрыть сессию"},
        ])
    })
    logger.info("Telegram Reply Watcher started")
    backoff = 5

    while True:
        offset = get_offset()
        data = tg_api("getUpdates", {
            "offset": offset,
            "timeout": 30,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        })

        if not data.get("ok"):
            logger.warning(f"getUpdates failed, retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        backoff = 5
        results = data.get("result", [])
        new_offset = offset

        for update in results:
            uid = update["update_id"]
            new_offset = max(new_offset, uid + 1)

            # Обработка callback_query (инлайн-кнопки)
            cb = update.get("callback_query")
            if cb:
                if cb.get("from", {}).get("id") == USER_ID:
                    try:
                        handle_callback(cb)
                    except Exception as e:
                        logger.error(f"[Callback] {e}")
                continue

            msg = update.get("message", {})
            if msg.get("chat", {}).get("id") != CHAT_ID:
                continue
            if msg.get("from", {}).get("id") != USER_ID:
                continue

            # Команды бота (прямые сообщения)
            cmd_text = msg.get("text", "").strip()
            if cmd_text.startswith("/newstart"):
                try:
                    handle_newstart(msg, cmd_text)
                except Exception as e:
                    logger.error(f"[NewStart] {e}")
                continue
            if cmd_text in ("/projects", "/start"):
                try:
                    handle_projects(msg)
                except Exception as e:
                    logger.error(f"[Projects] {e}")
                continue
            if cmd_text == "/sessions":
                try:
                    handle_sessions(msg)
                except Exception as e:
                    logger.error(f"[Sessions] {e}")
                continue
            if cmd_text == "/close":
                try:
                    handle_close(msg)
                except Exception as e:
                    logger.error(f"[Close] {e}")
                continue

            # Текст или голосовое сообщение
            text = msg.get("text", "").strip()
            voice = msg.get("voice")

            if not text and voice:
                file_id = voice.get("file_id")
                if file_id:
                    tg_react(msg.get("message_id"), "\U0001f3a7")
                    text = transcribe_voice(file_id)
                    if not text:
                        logger.info("Voice transcription failed, skipping")
                        continue

            if not text:
                continue

            user_msg_id = msg.get("message_id")

            # Маршрутизация: топик → pane (приоритет), fallback на reply
            topic_id = get_msg_topic(msg)
            pane_id = None

            if topic_id:
                pane_id = tg_sessions.get_pane_by_topic(topic_id)

            if not pane_id:
                reply_to = msg.get("reply_to_message")
                if reply_to and reply_to.get("from", {}).get("is_bot", False):
                    reply_msg_id = str(reply_to.get("message_id", ""))
                    pane_id = tg_sessions.get_pane(reply_msg_id)

            if not pane_id:
                if topic_id:
                    logger.info(f"No pane for topic={topic_id}")
                continue

            # Quick replies для permission-промптов (Do you want to make this edit?)
            lower = text.lower()
            if lower in ("y", "yes", "да", "1", "ок", "ok", "+") and is_permission_prompt(pane_id):
                logger.info(f"Permission confirm -> pane {pane_id}")
                sent = tmux_send_keys(pane_id, "Enter")
            elif lower in ("n", "no", "нет", "3", "-") and is_permission_prompt(pane_id):
                logger.info(f"Permission decline -> pane {pane_id}")
                sent = tmux_send_keys(pane_id, "Escape")
            else:
                text = f"[TG | ~/.claude/tg-integration] {text}"
                logger.info(f"Reply: {text[:80]} -> pane {pane_id}")
                sent = tmux_send(pane_id, text)

            if sent:
                tg_sessions.save_reply_to(pane_id, user_msg_id)
                tg_react(user_msg_id)
                logger.info("  OK")
            else:
                tg_sessions.cleanup_pane(pane_id)
                logger.info(f"  Pane {pane_id} not found, cleaned up")

        if new_offset > offset:
            save_offset(new_offset)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        cleanup_pid()
