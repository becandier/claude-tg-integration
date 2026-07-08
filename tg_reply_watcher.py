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
import re
import signal
import subprocess
import sys
import shutil
import tempfile
import threading
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
# Папки, в которые не спускаемся при поиске вложенных проектов: они огромны
# (тысячи файлов) и содержат "ложные" маркеры (package.json в node_modules и т.п.).
# Спуск в них — главная причина медленного формирования списка папок.
IGNORE_DIRS = {
    "node_modules", ".venv", "venv", "env", "__pycache__",
    "dist", "build", "out", "target", ".dart_tool", ".next", ".nuxt",
    "Pods", ".gradle", ".idea", ".vscode", "vendor", ".cache",
    "DerivedData", ".terraform", "coverage", ".pub-cache", ".cocoapods",
}
# TTL-кэш для результатов обхода ФС (структура папок меняется редко).
_FS_CACHE_TTL = 30.0
_is_project_cache = {}      # path -> (expires_at, bool)
_nested_cache = {}          # (path, depth) -> (expires_at, bool)
MEDIA_DIR = "/tmp/claude_tg_media"

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
    # getUpdates — long polling, ретраи не нужны (main loop сам обработает).
    # Остальные методы ретраим до 3 раз при сетевых ошибках.
    max_attempts = 1 if method == "getUpdates" else 3
    for attempt in range(max_attempts):
        try:
            req = urllib.request.Request(url, data=data)
            timeout = 35 if method == "getUpdates" else 10
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception as e:
            logger.error(f"[TG API] {method} (attempt {attempt + 1}/{max_attempts}): {e}")
            if attempt < max_attempts - 1:
                time.sleep(1)
    return {"ok": False}


# Палитра цветов для Forum Topics (ротация)
TOPIC_COLORS = [7322096, 16766590, 13338331, 9367192, 16749490, 16478047]
# Инициализируем индекс из количества существующих топиков, чтобы не повторять цвета после рестарта
_topic_color_idx = len(tg_sessions._load().get("pane_to_topic", {}))


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


def _safe_pane_id(pane_id):
    """Безопасное имя директории из pane_id (убирает спецсимволы)."""
    return pane_id.replace("%", "pane").replace("/", "_")


def media_dir_for_pane(pane_id):
    return os.path.join(MEDIA_DIR, _safe_pane_id(pane_id))


def download_media(file_id, pane_id, filename):
    """Скачивает медиафайл в директорию pane, возвращает путь или None."""
    dest_dir = media_dir_for_pane(pane_id)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, filename)
    if tg_download_file(file_id, dest_path):
        logger.info(f"[Media] Downloaded {dest_path}")
        return dest_path
    return None


def cleanup_media(pane_id=None):
    """Удаляет медиафайлы. Если pane_id — только для этого pane, иначе все."""
    if pane_id:
        path = media_dir_for_pane(pane_id)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            logger.info(f"[Media] Cleaned {path}")
    else:
        if os.path.isdir(MEDIA_DIR):
            shutil.rmtree(MEDIA_DIR, ignore_errors=True)
            logger.info("[Media] Cleaned all media")


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
    # Длинный текст вызывает bracketed paste в Claude Code ([Pasted-text]).
    # Нужна пауза, чтобы Claude Code успел обработать paste перед Enter.
    if len(text) > 120:
        time.sleep(0.5)
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
        ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-15"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    content = result.stdout
    if "Esc to cancel" not in content:
        return False
    # Разные типы permission-диалогов Claude Code:
    #  - Bash/команды: "Enter to confirm/proceed"
    #  - Edit/Write: "Do you want to make this edit/write..." + нумерованный выбор
    if "Enter to confirm" in content or "Enter to proceed" in content:
        return True
    if "Do you want to" in content:
        return True
    if "1. Yes" in content and ("2. " in content or "3. No" in content):
        return True
    return False


def wait_and_trust_folder(pane_id, timeout=15):
    """Ждёт появления trust-промпта Claude Code и автоматически подтверждает."""
    for _ in range(timeout * 5):
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-25"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False
        content = result.stdout
        if "Yes, I trust this folder" in content or "Enter to confirm" in content:
            time.sleep(0.3)
            subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"])
            logger.info(f"[Trust] Auto-confirmed trust prompt for pane {pane_id}")
            return True
        time.sleep(0.2)
    logger.warning(f"[Trust] Timeout waiting for trust prompt on pane {pane_id}")
    return False


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

    # Автоматически подтверждаем trust-промпт в фоне (не блокируя polling)
    threading.Thread(
        target=wait_and_trust_folder, args=(pane_id,), daemon=True
    ).start()

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
    now = time.time()
    cached = _is_project_cache.get(path)
    if cached and cached[0] > now:
        return cached[1]
    try:
        entries = os.listdir(path)
        result = bool(PROJECT_MARKERS & set(entries))
    except (PermissionError, FileNotFoundError, NotADirectoryError):
        result = False
    _is_project_cache[path] = (now + _FS_CACHE_TTL, result)
    return result


def _has_nested_projects(path, depth=0):
    """Проверяет, есть ли вложенные проекты (рекурсивно до MAX_PROJECT_DEPTH).

    Кэшируется по (path, depth): для конкретной папки глубина в дереве
    детерминирована, поэтому кэш не меняет результат.
    """
    if depth >= MAX_PROJECT_DEPTH:
        return False
    now = time.time()
    key = (path, depth)
    cached = _nested_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]

    result = False
    try:
        entries = os.listdir(path)
    except (PermissionError, FileNotFoundError, NotADirectoryError):
        entries = []
    for entry in entries:
        if entry.startswith(".") or entry in IGNORE_DIRS:
            continue
        full = os.path.join(path, entry)
        if not os.path.isdir(full):
            continue
        if _is_project(full) or _has_nested_projects(full, depth + 1):
            result = True
            break

    _nested_cache[key] = (now + _FS_CACHE_TTL, result)
    return result


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
        if entry.startswith(".") or entry in IGNORE_DIRS:
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
    """Gracefully close a Claude Code tmux session + закрыть Forum Topic."""
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_id}"],
        capture_output=True, text=True,
    )
    pane_id = result.stdout.strip().split("\n")[0] if result.stdout.strip() else None

    # Закрываем Forum Topic до cleanup (после cleanup маппинг пропадёт)
    if pane_id:
        topic_id = tg_sessions.get_topic(pane_id)
        if topic_id:
            close_topic(topic_id)

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
        cleanup_media(pane_id)
        tg_sessions.cleanup_pane(pane_id)
    logger.info(f"[Close] session {session_name} closed")


def close_topic(topic_id):
    """Удаляет Forum Topic."""
    resp = tg_api("deleteForumTopic", {
        "chat_id": CHAT_ID,
        "message_thread_id": topic_id,
    })
    if resp.get("ok"):
        logger.info(f"[Topic] Deleted topic {topic_id}")
    else:
        logger.error(f"[Topic] Failed to delete {topic_id}: {resp}")


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
            # close_session уже закроет топик
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


def handle_cleanup(msg):
    """Очищает все скачанные медиафайлы."""
    user_msg_id = msg.get("message_id")
    topic_id = get_msg_topic(msg)
    cleanup_media()
    tg_send("🧹 Все медиафайлы удалены", topic_id=topic_id, reply_to=user_msg_id)


# Нативные уровни /effort в Claude Code. Кнопка в TG дёргает реальную команду
# `/effort <level>` в pane сессии — поэтому уровни должны совпадать с CLI.
EFFORT_LEVELS = {
    "low": "быстро, минимум накладных",
    "medium": "сбалансированно",
    "high": "глубже рассуждает",
    "xhigh": "чуть ниже максимума (Opus 4.8/4.7)",
    "ultracode": "xhigh + оркестрация workflow",
}

# Порядок и подписи кнопок инлайн-клавиатуры (раскладка по строкам).
EFFORT_ROWS = [
    [("low", "Low"), ("medium", "Medium"), ("high", "High")],
    [("xhigh", "Xhigh"), ("ultracode", "🧠 Ultracode")],
]


def _effort_keyboard(current):
    """Инлайн-клавиатура выбора эффорта; текущий уровень помечен галочкой."""
    return [
        [
            {
                "text": f"{'✅ ' if level == current else ''}{label}",
                "callback_data": f"effort:{level}",
            }
            for level, label in row
        ]
        for row in EFFORT_ROWS
    ]


def _effort_text(current):
    if current and current in EFFORT_LEVELS:
        return f"⚙️ Эффорт reasoning: *{current}* — {EFFORT_LEVELS[current]}"
    return "⚙️ Выбери уровень reasoning для этой сессии:"


def set_session_effort(pane_id, level):
    """Отправляет нативную команду `/effort <level>` в pane Claude Code."""
    if not pane_exists(pane_id):
        return False
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", f"/effort {level}"])
    time.sleep(0.4)  # дать слэш-палитре закрыться перед Enter
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"])
    return True


def handle_effort(msg, text):
    """`/effort [low|medium|high|xhigh|ultracode]` — эффорт сессии текущего топика.

    Без аргумента показывает инлайн-кнопки выбора.
    """
    user_msg_id = msg.get("message_id")
    topic_id = get_msg_topic(msg)

    if not topic_id:
        tg_send("⚙️ /effort работает только внутри топика сессии", reply_to=user_msg_id)
        return

    pane_id = tg_sessions.get_pane_by_topic(topic_id)
    if not pane_id:
        tg_send("⚠️ Нет активной сессии в этом топике", topic_id=topic_id)
        return

    parts = text.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""

    if not arg:
        cur = tg_sessions.get_effort(pane_id)
        tg_send(
            _effort_text(cur),
            topic_id=topic_id, parse_mode="Markdown",
            reply_markup={"inline_keyboard": _effort_keyboard(cur)},
        )
        return

    if arg not in EFFORT_LEVELS:
        tg_send(
            "⚠️ Неизвестный уровень. Доступно: `low` · `medium` · `high` · `xhigh` · `ultracode`",
            topic_id=topic_id, parse_mode="Markdown",
        )
        return

    if not set_session_effort(pane_id, arg):
        tg_send("⚠️ Сессия не найдена", topic_id=topic_id)
        return
    tg_sessions.save_effort(pane_id, arg)
    tg_send(
        _effort_text(arg),
        topic_id=topic_id, parse_mode="Markdown",
        reply_markup={"inline_keyboard": _effort_keyboard(arg)},
    )


# Список моделей парсится из нативного пикера /model в pane сессии — он всегда
# соответствует установленной версии Claude Code (модели меняются часто).
# Фолбэк — если пикер не открылся/не распарсился (стабильные алиасы CLI).
MODEL_FALLBACK = [
    {"alias": "default", "label": "Default", "desc": "", "current": False},
    {"alias": "opus", "label": "Opus", "desc": "", "current": False},
    {"alias": "sonnet", "label": "Sonnet", "desc": "", "current": False},
    {"alias": "haiku", "label": "Haiku", "desc": "", "current": False},
    {"alias": "fable", "label": "Fable", "desc": "", "current": False},
]

# Строка пикера: "  ❯ 3. Fable ✔                Fable 5 · Most capable..."
_MODEL_LINE_RE = re.compile(r"^\s*(?:❯\s*)?\d+\.\s+(.+?)\s{2,}(.+?)\s*$")


def _capture_pane(pane_id):
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", pane_id, "-p"],
        capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def _input_line(pane_id):
    """Нижняя строка ввода TUI (последняя строка с '❯')."""
    lines = [l for l in _capture_pane(pane_id).split("\n") if l.lstrip().startswith("❯")]
    return lines[-1] if lines else ""


def _type_command(pane_id, command):
    """Печатает слэш-команду и жмёт Enter, убедившись что текст дошёл целиком.

    Сразу после перерисовки TUI (закрытие пикера и т.п.) часть символов может
    теряться — тогда чистим строку (C-u) и пробуем ещё раз.
    """
    for attempt in range(2):
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", command])
        time.sleep(0.5)
        if command in _input_line(pane_id):
            subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"])
            return True
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "C-u"])
        time.sleep(0.3)
    return False


def probe_models(pane_id):
    """Открывает пикер /model в pane, парсит модели и активную (✔), закрывает Esc.

    Возвращает [{alias, label, desc, current}] или None, если пикер не открылся.
    """
    if not pane_exists(pane_id):
        return None
    if not _type_command(pane_id, "/model"):
        return None
    time.sleep(2)
    out = _capture_pane(pane_id)
    if "Select model" not in out:
        # Пикер не открылся — Escape не шлём (мог бы прервать текущий ход),
        # только чистим возможный остаток ввода.
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "C-u"])
        return None
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "Escape"])
    models = []
    for line in out.split("Select model", 1)[1].split("\n"):
        m = _MODEL_LINE_RE.match(line)
        if not m:
            continue
        label, desc = m.group(1).strip(), m.group(2).strip()
        current = "✔" in label or "✔" in desc
        label = label.replace("✔", "").strip()
        alias = label.split()[0].lower().strip(".")
        if not alias.isalpha():
            continue
        models.append({
            "alias": alias, "label": label,
            "desc": desc.replace("✔", "").strip(), "current": current,
        })
    return models or None


def _model_keyboard(models, current):
    """Одна модель — одна кнопка-строка; активная помечена галочкой."""
    rows = []
    for m in models:
        text = m["label"]
        if m["desc"]:
            text += " — " + m["desc"].split("·")[0].strip()
        if m["alias"] == current:
            text = "✅ " + text
        rows.append([{"text": text[:60], "callback_data": f"model:{m['alias']}"}])
    return rows


def _model_text(current):
    if current:
        return f"🤖 Модель сессии: *{current}*"
    return "🤖 Выбери модель для этой сессии:"


def set_session_model(pane_id, alias):
    """Отправляет нативную команду `/model <alias>` в pane Claude Code."""
    if not pane_exists(pane_id):
        return False
    return _type_command(pane_id, f"/model {alias}")


def handle_model(msg, text):
    """`/model [alias|полное-имя]` — модель сессии текущего топика.

    Без аргумента парсит живой пикер /model (актуальный список + активная
    модель) и показывает инлайн-кнопки.
    """
    user_msg_id = msg.get("message_id")
    topic_id = get_msg_topic(msg)

    if not topic_id:
        tg_send("🤖 /model работает только внутри топика сессии", reply_to=user_msg_id)
        return

    pane_id = tg_sessions.get_pane_by_topic(topic_id)
    if not pane_id:
        tg_send("⚠️ Нет активной сессии в этом топике", topic_id=topic_id)
        return

    parts = text.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""

    if not arg:
        models = probe_models(pane_id)
        note = ""
        if models is None:
            models = MODEL_FALLBACK
            note = "\n⚠️ Не смог прочитать пикер — список может быть неполным"
        current = next(
            (m["alias"] for m in models if m["current"]),
            tg_sessions.get_model(pane_id),
        )
        if current:
            tg_sessions.save_model(pane_id, current)
        tg_send(
            _model_text(current) + note,
            topic_id=topic_id, parse_mode="Markdown",
            reply_markup={"inline_keyboard": _model_keyboard(models, current)},
        )
        return

    if not re.fullmatch(r"[a-z0-9.\-]+", arg):
        tg_send("⚠️ Некорректное имя модели", topic_id=topic_id)
        return
    if not set_session_model(pane_id, arg):
        tg_send("⚠️ Сессия не найдена", topic_id=topic_id)
        return
    tg_sessions.save_model(pane_id, arg)
    tg_send(_model_text(arg), topic_id=topic_id, parse_mode="Markdown")


_last_pane_check = 0.0

# Мониторинг активности pane'ов
# pane_id -> {"sent_at": float, "last_snapshot": str, "last_change": float,
#             "notified_working": bool, "notified_stuck": bool}
_pane_activity = {}

ACTIVITY_WORKING_TIMEOUT = 120   # 2 мин без изменений → "всё ещё работает"
ACTIVITY_STUCK_TIMEOUT = 300     # 5 мин без изменений → "возможно завис"


def _idle_flag_path(pane_id):
    safe_id = re.sub(r"[^a-zA-Z0-9]", "_", pane_id)
    return f"/tmp/claude_code_idle_{safe_id}"


def mark_pane_waiting(pane_id):
    """Отмечает pane как ожидающий ответа от Claude."""
    # Убираем idle-флаг — Claude начал работать
    try:
        os.unlink(_idle_flag_path(pane_id))
    except FileNotFoundError:
        pass
    now = time.time()
    _pane_activity[pane_id] = {
        "sent_at": now,
        "last_snapshot": "",
        "last_change": now,
        "notified_working": False,
        "notified_stuck": False,
    }


def clear_pane_waiting(pane_id):
    """Сбрасывает ожидание (stop_hook отработал)."""
    _pane_activity.pop(pane_id, None)


def _capture_pane_content(pane_id):
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-15"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def check_dead_panes():
    """Проверяет живость pane'ов и активность — уведомляет о мёртвых и зависших."""
    global _last_pane_check
    now = time.time()
    if now - _last_pane_check < 30:
        return
    _last_pane_check = now

    data = tg_sessions._load()
    pane_to_topic = data.get("pane_to_topic", {})
    for pane_id, topic_id in list(pane_to_topic.items()):
        if not pane_exists(pane_id):
            tg_send("⚠️ Сессия завершилась", topic_id=topic_id)
            cleanup_media(pane_id)
            clear_pane_waiting(pane_id)
            tg_sessions.cleanup_pane(pane_id)
            logger.info(f"[DeadPane] pane {pane_id} dead, notified topic {topic_id}")
            continue

        # Мониторинг активности
        activity = _pane_activity.get(pane_id)
        if not activity:
            continue

        # idle-флаг ставится notify_hook'ом когда Claude ждёт ввода
        if os.path.exists(_idle_flag_path(pane_id)):
            clear_pane_waiting(pane_id)
            continue

        # stop_hook ставит флаг-файл — если есть, значит Claude ответил
        safe_id = re.sub(r"[^a-zA-Z0-9]", "_", pane_id)
        stop_flag = f"/tmp/claude_code_stopped_{safe_id}"
        if os.path.exists(stop_flag):
            clear_pane_waiting(pane_id)
            continue

        if snapshot != activity["last_snapshot"]:
            activity["last_snapshot"] = snapshot
            activity["last_change"] = now
            activity["notified_working"] = False
            activity["notified_stuck"] = False
            continue

        idle_time = now - activity["last_change"]

        if idle_time >= ACTIVITY_STUCK_TIMEOUT and not activity["notified_stuck"]:
            tg_send("⚠️ Claude не отвечает уже 5 минут — возможно завис", topic_id=topic_id)
            activity["notified_stuck"] = True
            logger.info(f"[Activity] pane {pane_id} stuck ({idle_time:.0f}s)")
        elif idle_time >= ACTIVITY_WORKING_TIMEOUT and not activity["notified_working"]:
            tg_send("⏳ Claude всё ещё работает...", topic_id=topic_id)
            activity["notified_working"] = True
            logger.info(f"[Activity] pane {pane_id} still working ({idle_time:.0f}s)")


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
        # Защита от path traversal
        if ".." in project and project not in ("__home__", "__projects__"):
            tg_api("answerCallbackQuery", {
                "callback_query_id": cb_id,
                "text": "⚠️ Недопустимый путь",
                "show_alert": True,
            })
            return
        label = "домашняя директория" if project == "__home__" else project
        tg_api("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": f"Запускаю {label}...",
        })
        handle_newstart(msg, f"/newstart {project}")
    elif data.startswith("nav:"):
        rel_path = data[4:]  # может быть "" для корня
        if ".." in rel_path:
            tg_api("answerCallbackQuery", {
                "callback_query_id": cb_id,
                "text": "⚠️ Недопустимый путь",
                "show_alert": True,
            })
            return
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
    elif data.startswith("effort:"):
        level = data.split(":", 1)[1]
        chat_msg_id = msg.get("message_id")
        topic_id = get_msg_topic(msg)
        pane_id = tg_sessions.get_pane_by_topic(topic_id) if topic_id else ""

        if not pane_id or not pane_exists(pane_id):
            tg_api("answerCallbackQuery", {
                "callback_query_id": cb_id,
                "text": "⚠️ Нет активной сессии",
                "show_alert": True,
            })
            return
        if level not in EFFORT_LEVELS:
            tg_api("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        set_session_effort(pane_id, level)
        tg_sessions.save_effort(pane_id, level)
        tg_api("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": f"Эффорт: {level}",
        })
        tg_edit(
            chat_msg_id,
            _effort_text(level),
            parse_mode="Markdown",
            reply_markup={"inline_keyboard": _effort_keyboard(level)},
        )
        logger.info(f"[Callback] effort={level} -> pane {pane_id}")
    elif data.startswith("model:"):
        alias = data.split(":", 1)[1]
        chat_msg_id = msg.get("message_id")
        topic_id = get_msg_topic(msg)
        pane_id = tg_sessions.get_pane_by_topic(topic_id) if topic_id else ""

        if not pane_id or not pane_exists(pane_id):
            tg_api("answerCallbackQuery", {
                "callback_query_id": cb_id,
                "text": "⚠️ Нет активной сессии",
                "show_alert": True,
            })
            return

        set_session_model(pane_id, alias)
        tg_sessions.save_model(pane_id, alias)
        tg_api("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": f"Модель: {alias}",
        })
        # Клавиатуру не перестраиваем с нуля — двигаем галочку в существующей
        kb = (msg.get("reply_markup") or {}).get("inline_keyboard") or []
        new_kb = []
        for row in kb:
            new_row = []
            for btn in row:
                base = btn.get("text", "")
                if base.startswith("✅ "):
                    base = base[len("✅ "):]
                cb_data = btn.get("callback_data", "")
                new_row.append({
                    "text": ("✅ " + base) if cb_data == data else base,
                    "callback_data": cb_data,
                })
            new_kb.append(new_row)
        tg_edit(
            chat_msg_id,
            _model_text(alias),
            parse_mode="Markdown",
            reply_markup={"inline_keyboard": new_kb},
        )
        logger.info(f"[Callback] model={alias} -> pane {pane_id}")
    else:
        tg_api("answerCallbackQuery", {"callback_query_id": cb_id})


def main():
    setup_logging()
    write_pid()
    signal.signal(signal.SIGTERM, cleanup_pid)
    signal.signal(signal.SIGINT, cleanup_pid)
    init_offset()

    # Регистрируем команды бота (для меню в группе и в приватном чате)
    commands = json.dumps([
        {"command": "newstart", "description": "🚀 Новая сессия Claude Code"},
        {"command": "projects", "description": "📁 Список проектов"},
        {"command": "sessions", "description": "📋 Активные сессии"},
        {"command": "close", "description": "🔴 Закрыть сессию"},
        {"command": "cleanup", "description": "🧹 Очистить все медиафайлы"},
        {"command": "effort", "description": "⚙️ Эффорт reasoning (low…ultracode)"},
        {"command": "model", "description": "🤖 Модель Claude (список из живого пикера)"},
    ])
    tg_api("setMyCommands", {"commands": commands})
    tg_api("setMyCommands", {
        "commands": commands,
        "scope": json.dumps({"type": "all_group_chats"}),
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

            # Команды бота — убираем @bot_username суффикс (в группах: /cmd@botname args)
            raw_text = msg.get("text", "").strip()
            cmd_text = re.sub(r"^(/\w+)@\w+", r"\1", raw_text)
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
            if cmd_text == "/cleanup":
                try:
                    handle_cleanup(msg)
                except Exception as e:
                    logger.error(f"[Cleanup] {e}")
                continue
            if cmd_text.startswith("/effort"):
                try:
                    handle_effort(msg, cmd_text)
                except Exception as e:
                    logger.error(f"[Effort] {e}")
                continue
            if cmd_text.startswith("/model"):
                try:
                    handle_model(msg, cmd_text)
                except Exception as e:
                    logger.error(f"[Model] {e}")
                continue

            # Текст, голосовое, фото или документ
            text = msg.get("text", "").strip()
            voice = msg.get("voice")
            photo = msg.get("photo")
            document = msg.get("document")

            if not text and voice:
                file_id = voice.get("file_id")
                if file_id:
                    tg_react(msg.get("message_id"), "\U0001f3a7")
                    text = transcribe_voice(file_id)
                    if not text:
                        logger.info("Voice transcription failed, skipping")
                        continue

            # Caption от фото/документа
            if not text and (photo or document):
                text = msg.get("caption", "").strip()

            has_media = bool(photo or document)
            if not text and not has_media:
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

            # Скачиваем медиафайл если есть
            media_path = None
            if photo:
                file_id = photo[-1]["file_id"]  # самый большой размер
                media_path = download_media(file_id, pane_id, f"photo_{user_msg_id}.jpg")
            elif document:
                file_name = document.get("file_name", f"doc_{user_msg_id}")
                file_id = document["file_id"]
                media_path = download_media(file_id, pane_id, file_name)

            if media_path:
                label = "Фото" if photo else os.path.basename(media_path)
                text = f"[Файл ({label}): {media_path}] {text}" if text else f"[Файл ({label}): {media_path}]"

            if not text:
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
                text = (
                    f"[TG | ~/.claude/tg-integration] {text}\n\n"
                    "[SYSTEM] Сообщение из Telegram — примени скилл tg-chat "
                    "(правила ответа и отправка файлов там). Отвечай кратко."
                )
                logger.info(f"Reply: {text[:80]} -> pane {pane_id}")
                sent = tmux_send(pane_id, text)

            if sent:
                tg_sessions.save_reply_to(pane_id, user_msg_id)
                tg_react(user_msg_id)
                mark_pane_waiting(pane_id)
                logger.info("  OK")
            else:
                cleanup_media(pane_id)
                tg_sessions.cleanup_pane(pane_id)
                logger.info(f"  Pane {pane_id} not found, cleaned up")

        if new_offset > offset:
            save_offset(new_offset)

        # Периодическая проверка мёртвых pane'ов
        try:
            check_dead_panes()
        except Exception as e:
            logger.error(f"[DeadPane] {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        cleanup_pid()
