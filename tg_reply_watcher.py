#!/usr/bin/env python3
"""
Telegram Reply Watcher for Claude Code and Codex CLI (multi-session)

Поллит Telegram на reply-сообщения. По message_id определяет,
в какой tmux pane отправить текст.

Использование:
  python3 -u ~/.claude/tg-integration/tg_reply_watcher.py
"""

import glob
import json
import hashlib
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
import mimetypes
import urllib.error
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
# До этой длины однострочный текст набирается в Claude Code через send-keys -l,
# длиннее — вставляется bracketed paste (см. tmux_send).
CLAUDE_TYPED_TEXT_LIMIT = 120
# Bot API отдаёт через getFile файлы не больше 20 МБ (лимит на стороне Telegram).
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024
# Виды медиа, которые мост принимает. Порядок важен: document проверяем последним,
# т.к. Telegram кладёт в document только то, что не распознал как video/audio/animation.
MEDIA_KINDS = (
    ("video", "Видео", ".mp4"),
    ("animation", "GIF", ".mp4"),
    ("video_note", "Видеокружок", ".mp4"),
    ("audio", "Аудио", ".mp3"),
    ("document", None, ".bin"),
)

# Импортируем shared-модуль
sys.path.insert(0, os.path.expanduser("~/.claude/tg-integration"))
import tg_sessions
import tg_resume
import tg_stats
from tg_codex_submit_hook import PROMPT_MARKER, REACTION_READING, message_id_from_prompt
from tg_topic_title import format_topic_name
from agent_runtime import (
    AgentKind,
    CLAUDE_MODEL_PICKER_MARKER,
    LaunchSpec,
    build_launch_argv,
    is_managed_session,
    make_session_name,
    parse_claude_model_picker,
    parse_codex_effort_picker,
    parse_codex_model_picker,
    parse_newstart_args,
    picker_navigation_keys,
    project_callback_data,
    resolve_project_dir,
    resume_cli_settings,
    terminal_model,
    terminal_effort,
    terminal_ready,
    parse_codex_update_picker,
    parse_codex_rate_limit_picker,
)

logger = logging.getLogger("watcher")


def setup_logging():
    handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(message)s", datefmt="%H:%M:%S"
    ))
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)


def tg_api(method, params=None, *, max_attempts=None, request_timeout=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode() if params else None
    # getUpdates — long polling, ретраи не нужны (main loop сам обработает).
    # Остальные методы ретраим до 3 раз при сетевых ошибках.
    max_attempts = max_attempts if max_attempts is not None else (1 if method == "getUpdates" else 3)
    for attempt in range(max_attempts):
        try:
            req = urllib.request.Request(url, data=data)
            timeout = request_timeout if request_timeout is not None else (35 if method == "getUpdates" else 10)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                body = ""
            logger.error(f"[TG API] {method} (attempt {attempt + 1}/{max_attempts}): {e} {body[:200]}")
            # 4xx детерминирован, а в теле лежит description, по которому вызов
            # отличает «уже так и называется» от настоящей ошибки — возвращаем его.
            if 400 <= e.code < 500:
                try:
                    payload = json.loads(body)
                except (json.JSONDecodeError, ValueError):
                    payload = None
                if isinstance(payload, dict) and "ok" in payload:
                    return payload
                return {"ok": False, "description": body}
            if attempt < max_attempts - 1:
                time.sleep(1)
            continue
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


def parse_bot_command(raw_text):
    """Нормализует Telegram-команду; media-only updates возвращают пустую команду."""
    command_text = re.sub(r"^(/\w+)@\w+", r"\1", raw_text.strip())
    parts = command_text.split(maxsplit=1)
    return command_text, parts[0] if parts else ""


def agent_prompt(text, agent, message_id):
    prefix = PROMPT_MARKER
    if agent is AgentKind.CODEX and isinstance(message_id, int):
        prefix += f" [tg-message-id:{message_id}]"
    return f"{prefix} {text}"


# Реакции ограничены фиксированным набором Telegram: 🎧 и 📝, например,
# отклоняются с REACTION_INVALID, хотя чат не ограничивает available_reactions.
REACTION_ACK = "\u26a1"           # принято и отправлено в сессию
REACTION_VOICE = "\u270d\ufe0f"   # голосовое распознаётся в текст
REACTION_REJECTED = "\U0001f44e"  # файл не принят


def _set_reaction(message_id, emoji, *, quick=False):
    return tg_api("setMessageReaction", {
        "chat_id": CHAT_ID,
        "message_id": message_id,
        "reaction": json.dumps([{"type": "emoji", "emoji": emoji}]),
    }, **({"max_attempts": 1, "request_timeout": 2} if quick else {}))


def tg_react(message_id, emoji=REACTION_ACK):
    """Ставит реакцию на сообщение как подтверждение доставки.

    Эмодзи вне набора Telegram получает REACTION_INVALID — тогда ставим
    проверенный ⚡, чтобы подтверждение не пропадало молча.
    """
    response = _set_reaction(message_id, emoji)
    if emoji != REACTION_ACK and "REACTION_INVALID" in response.get("description", ""):
        logger.warning("[Reaction] %s не поддерживается Telegram, ставлю %s", emoji, REACTION_ACK)
        response = _set_reaction(message_id, REACTION_ACK)
    return response


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


def safe_media_name(name, fallback):
    """Приводит имя файла из Telegram к безопасному для ФС виду."""
    name = os.path.basename((name or "").strip())
    name = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("._")
    return name or fallback


def extract_media(msg, msg_id):
    """Ищет любое поддерживаемое медиа в сообщении.

    Возвращает (file_id, filename, file_size, label) или (None, None, None, None).
    """
    photo = msg.get("photo")
    if photo:
        best = photo[-1]
        return best["file_id"], f"photo_{msg_id}.jpg", best.get("file_size"), "Фото"

    for kind, label, default_ext in MEDIA_KINDS:
        item = msg.get(kind)
        if not isinstance(item, dict) or "file_id" not in item:
            continue
        ext = mimetypes.guess_extension(item.get("mime_type") or "") or default_ext
        filename = safe_media_name(item.get("file_name"), f"{kind}_{msg_id}{ext}")
        return item["file_id"], filename, item.get("file_size"), label or filename
    return None, None, None, None


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


def live_tmux_panes():
    """{pane_id: имя tmux-сессии} всех живых pane'ов; {} без tmux-сервера."""
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{session_name}"],
        capture_output=True, text=True,
    )
    panes = {}
    for line in result.stdout.splitlines():
        pane_id, _, session_name = line.partition(" ")
        if pane_id:
            panes[pane_id] = session_name
    return panes


def pane_exists(pane_id):
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
        capture_output=True, text=True,
    )
    return pane_id in result.stdout.split()


def tmux_send(pane_id, text):
    """Отправляет текст + Enter в pane Claude Code.

    Длинный или многострочный текст идёт единым bracketed paste: при наборе через
    send-keys -l Claude Code терял начало, из ~3.9 тыс. символов доходили последние ~300.
    Короткая однострочная реплика набирается как раньше, чтобы не получать обёртку
    <pasted_content>.
    """
    if not pane_exists(pane_id):
        return False
    if len(text) <= CLAUDE_TYPED_TEXT_LIMIT and "\n" not in text:
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", text])
    elif _tmux_paste(pane_id, text, "tg-claude"):
        # Пауза, чтобы Claude Code успел свернуть вставку до Enter.
        time.sleep(0.5)
    else:
        return False
    submitted = subprocess.run(
        ["tmux", "send-keys", "-t", pane_id, "Enter"],
        capture_output=True,
        text=True,
    )
    return submitted.returncode == 0


def tmux_send_codex(pane_id, text):
    """Отправляет Codex единый bracketed-paste event, затем отдельный Enter."""
    if not pane_exists(pane_id):
        return False
    if not _tmux_paste(pane_id, text, "tg-codex"):
        return False
    submitted = subprocess.run(
        ["tmux", "send-keys", "-t", pane_id, "Enter"],
        capture_output=True,
        text=True,
    )
    return submitted.returncode == 0


def _codex_delivery_context(pane_id):
    """Get the rollout position before a prompt is delivered, including old CLI sessions."""
    session_id = tg_sessions.get_session_id(pane_id)
    path = tg_stats.find_codex_rollout(session_id) if session_id else ""
    if not path:
        try:
            pane = subprocess.run(
                ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_pid}"],
                capture_output=True, text=True, timeout=3,
            )
            pid = pane.stdout.strip()
            if pane.returncode != 0 or not pid.isdigit():
                return None
            opened = subprocess.run(
                ["lsof", "-p", pid, "-Fn"], capture_output=True, text=True, timeout=5,
            )
            root = os.path.join(tg_stats.CODEX_HOME, "sessions") + os.sep
            for line in opened.stdout.splitlines():
                if not line.startswith("n") or not line[1:].startswith(root):
                    continue
                candidate = line[1:]
                match = re.search(r"-([0-9a-f-]{36})\.jsonl$", candidate)
                if match and tg_stats.find_codex_rollout(match.group(1)) == candidate:
                    session_id, path = match.group(1), candidate
                    break
        except (OSError, subprocess.TimeoutExpired):
            return None
    if not path:
        return None
    try:
        return session_id, os.path.getsize(path)
    except OSError:
        return None


def _tmux_paste(pane_id, text, buffer_prefix):
    """Вставляет текст в pane одним bracketed paste через именованный tmux-буфер."""
    buffer_name = f"{buffer_prefix}-{os.getpid()}-{time.time_ns()}"
    loaded = subprocess.run(
        ["tmux", "load-buffer", "-b", buffer_name, "-"],
        input=text,
        text=True,
        capture_output=True,
    )
    if loaded.returncode != 0:
        return False

    pasted = subprocess.run(
        [
            "tmux", "paste-buffer", "-p", "-r", "-d",
            "-b", buffer_name, "-t", pane_id,
        ],
        capture_output=True,
        text=True,
    )
    if pasted.returncode != 0:
        subprocess.run(
            ["tmux", "delete-buffer", "-b", buffer_name],
            capture_output=True,
            text=True,
        )
        return False
    return True


def tmux_send_keys(pane_id, *keys):
    """Отправляет raw-клавиши (не литеральный текст) в tmux pane."""
    if not pane_exists(pane_id):
        return False
    for key in keys:
        result = subprocess.run(["tmux", "send-keys", "-t", pane_id, key], capture_output=True)
        if result.returncode != 0:
            return False
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


def claude_trust_navigation_keys(content):
    """Возвращает клавиши для выбора Yes только в directory trust dialog."""
    if (
        "Accessing workspace:" not in content
        or "Yes, I trust this folder" not in content
        or "Enter to confirm" not in content
    ):
        return None

    options = [
        line
        for line in content.splitlines()
        if "Yes, I trust this folder" in line
        or "No, exit" in line
        or "No, continue without these permissions" in line
    ]
    selected_index = next(
        (
            index
            for index, line in enumerate(options)
            if line.lstrip().startswith(("❯", "›", ">"))
        ),
        None,
    )
    yes_index = next(
        (
            index
            for index, line in enumerate(options)
            if "Yes, I trust this folder" in line
        ),
        None,
    )
    if selected_index is None or yes_index is None:
        return None
    if selected_index == yes_index:
        return ("Enter",)

    direction = "Down" if yes_index > selected_index else "Up"
    return tuple(
        [direction] * abs(yes_index - selected_index) + ["Enter"]
    )


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
        keys = claude_trust_navigation_keys(content)
        if keys:
            time.sleep(0.3)
            for key in keys:
                subprocess.run(["tmux", "send-keys", "-t", pane_id, key])
            logger.info(f"[Trust] Auto-confirmed trust prompt for pane {pane_id}")
            return True
        time.sleep(0.2)
    logger.warning(f"[Trust] Timeout waiting for trust prompt on pane {pane_id}")
    return False


def wait_and_trust_codex_folder(pane_id, timeout=15):
    """Подтверждает только точный directory-trust prompt Codex CLI."""
    for _ in range(timeout * 5):
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-25"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False
        content = result.stdout
        if (
            "Do you trust the contents of this directory?" in content
            and "1. Yes, continue" in content
            and "2. No, quit" in content
        ):
            subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"])
            logger.info(f"[Trust] Auto-confirmed Codex directory for pane {pane_id}")
            return True
        time.sleep(0.2)
    logger.info(f"[Trust] No Codex directory prompt for pane {pane_id}")
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

def make_launch_spec(agent, session_name, project_dir, *, resume_session_id="", model="", effort=""):
    """Создаёт launch spec; Telegram-доставка Codex идёт через native Stop hook."""
    return LaunchSpec(
        agent=agent,
        session_name=session_name,
        project_dir=project_dir,
        model=model,
        effort=effort,
        resume_session_id=resume_session_id,
    )


def _spec_from_startup(agent, state):
    """Launch spec из pane_to_startup: перезапуск после обновления сохраняет resume."""
    return make_launch_spec(
        agent, state["session"], state["project"],
        resume_session_id=state.get("resume_session_id", ""),
        model=state.get("launch_model", ""),
        effort=state.get("launch_effort", ""),
    )


def spawn_agent_session(launch_spec, topic_id):
    """Запускает CLI в новой tmux-сессии и привязывает pane к топику.

    Возвращает (pane_id, None) или (None, текст ошибки). Общий путь для
    /newstart и восстановления сессии после перезагрузки.
    """
    result = subprocess.run(
        build_launch_argv(launch_spec),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None, f"❌ Не удалось создать сессию:\n{result.stderr[:200]}"

    result = subprocess.run(
        ["tmux", "list-panes", "-t", launch_spec.session_name, "-F", "#{pane_id}"],
        capture_output=True, text=True,
    )
    pane_id = result.stdout.strip().split("\n")[0] if result.stdout.strip() else None
    if not pane_id:
        subprocess.run(["tmux", "kill-session", "-t", launch_spec.session_name], capture_output=True)
        return None, "❌ Сессия создана, но не удалось получить pane_id"

    tg_sessions.save_topic(pane_id, topic_id)
    tg_sessions.save_agent(pane_id, launch_spec.agent.value)
    tg_sessions.save_project(pane_id, launch_spec.project_dir)
    return pane_id, None


def handle_newstart(msg, text):
    """Создаёт новую tmux-сессию с Claude Code или Codex CLI."""
    agent, project = parse_newstart_args(text)
    user_msg_id = msg.get("message_id")
    src_topic = get_msg_topic(msg)

    if not project:
        handle_projects(msg, agent=agent)
        return

    try:
        project_dir = resolve_project_dir(
            project,
            PROJECTS_ROOT,
            os.path.expanduser("~"),
        )
    except ValueError:
        tg_send("❌ Недопустимый путь проекта", topic_id=src_topic, reply_to=user_msg_id)
        return
    if not os.path.isdir(project_dir):
        tg_send(f"❌ Проект не найден: {project}", topic_id=src_topic, reply_to=user_msg_id)
        return

    executable = agent.value
    if shutil.which(executable) is None:
        tg_send(
            f"❌ CLI не найден: {executable}",
            topic_id=src_topic,
            reply_to=user_msg_id,
        )
        return

    session_name = make_session_name(agent, time.time_ns())
    project_name = os.path.basename(project_dir)
    agent_name = "Claude Code" if agent is AgentKind.CLAUDE else "Codex"

    tg_react(user_msg_id)

    # Создаём Forum Topic для этой сессии. Модель ещё неизвестна — пока в
    # названии тип агента, sync_topic_titles заменит его, когда CLI запустится.
    topic_id = create_forum_topic(format_topic_name(agent.value, project_name, ""))
    if not topic_id:
        tg_send("❌ Не удалось создать топик", topic_id=src_topic, reply_to=user_msg_id)
        return

    pane_id, error = spawn_agent_session(
        make_launch_spec(agent, session_name, project_dir), topic_id,
    )
    if not pane_id:
        tg_send(error, topic_id=topic_id, reply_to=user_msg_id)
        close_topic(topic_id)
        return

    response = tg_send(
        f"⏳ {agent_name} запускается\nПроект: {project_name}\nСессия: {session_name}\nМодель: определяется после запуска",
        topic_id=topic_id,
    )
    tg_sessions.save_startup(pane_id, {
        "session": session_name, "project": project_dir,
        "started_at": time.time(),
        "message_id": response.get("result", {}).get("message_id"),
    })

    if response.get("ok"):
        bot_msg_id = str(response["result"]["message_id"])
        tg_sessions.save_route(bot_msg_id, pane_id)

    tg_sessions.save_reply_to(pane_id, user_msg_id)
    open_terminal_with_tmux(session_name)
    logger.info(
        f"[NewStart] agent={agent.value} session={session_name} pane={pane_id} "
        f"topic={topic_id} project={project_name}"
    )


RESUME_FAILURES = {
    tg_resume.NO_SESSION_ID: (
        "⚠️ Сессия этого топика завершилась, но её id не сохранился — "
        "восстановить историю нельзя. Начни новую: /newclaude или /newcodex"
    ),
    tg_resume.NO_PROJECT: "⚠️ Не могу восстановить сессию: каталог проекта не найден ({project})",
    tg_resume.NO_HISTORY: (
        "⚠️ Не могу восстановить сессию: история {session_id} не найдена на диске. "
        "Начни новую: /newclaude или /newcodex"
    ),
}


def _resume_history_exists(record):
    """Журнал сессии на диске: без него `--resume` / `codex resume` завершится ошибкой."""
    session_id = record["session_id"]
    if record.get("agent") == AgentKind.CODEX.value:
        return bool(tg_stats.find_codex_rollout(session_id))
    path = tg_stats.claude_transcript_path({"sessionId": session_id, "cwd": record["project"]})
    if path and os.path.isfile(path):
        return True
    # cwd мог быть записан Claude в другом написании (симлинк, realpath).
    return bool(glob.glob(os.path.join(tg_stats.CLAUDE_PROJECTS_DIR, "*", f"{session_id}.jsonl")))


def _drop_stale_panes(topic_id):
    """Убирает мёртвые pane'ы топика; возвращает их неотправленную очередь."""
    data = tg_sessions._load()
    pending = []
    for pane_id, tid in list(data.get("pane_to_topic", {}).items()):
        if tid != int(topic_id) or pane_exists(pane_id):
            continue
        pending += data.get("pane_to_pending", {}).get(pane_id, [])
        cleanup_media(pane_id)
        clear_pane_waiting(pane_id)
        tg_sessions.cleanup_pane(pane_id)
        logger.info(f"[Resume] dropped stale pane {pane_id} of topic {topic_id}")
    return pending


def restore_topic_session(topic_id, user_msg_id):
    """Поднимает сессию топика по постоянному снимку (после перезагрузки Mac).

    Возвращает pane_id; "" — восстановить нельзя, пользователь уведомлён;
    None — снимка нет, вызывающий оставляет прежнее поведение.
    Повторный вызов при живом pane возвращает его же: несколько сообщений
    подряд не запускают вторую сессию, а встают в очередь первой.
    """
    pane_id = tg_sessions.get_pane_by_topic(topic_id)
    if pane_id and pane_exists(pane_id):
        return pane_id

    record = tg_resume.get(topic_id)
    decision = tg_resume.resume_decision(record, history_exists=_resume_history_exists)
    if decision == tg_resume.NO_SNAPSHOT:
        return None
    if decision != tg_resume.RESTORE:
        logger.info(f"[Resume] topic {topic_id}: {decision}")
        tg_send(RESUME_FAILURES[decision].format(**record), topic_id=topic_id, reply_to=user_msg_id)
        return ""

    try:
        agent = AgentKind(record.get("agent", AgentKind.CLAUDE.value))
    except ValueError:
        tg_send("⚠️ Не могу восстановить сессию: неизвестный тип агента", topic_id=topic_id)
        return ""
    if shutil.which(agent.value) is None:
        tg_send(f"❌ CLI не найден: {agent.value}", topic_id=topic_id, reply_to=user_msg_id)
        return ""

    pending = _drop_stale_panes(topic_id)
    model, effort, late_effort = resume_cli_settings(
        agent, record.get("model", ""), record.get("effort", ""),
    )
    session_name = make_session_name(agent, time.time_ns())
    try:
        pane_id, error = spawn_agent_session(make_launch_spec(
            agent, session_name, record["project"],
            resume_session_id=record["session_id"], model=model, effort=effort,
        ), topic_id)
    except ValueError as e:
        pane_id, error = None, f"⚠️ Не могу восстановить сессию: {e}"
    if not pane_id:
        tg_send(error, topic_id=topic_id, reply_to=user_msg_id)
        return ""

    if record.get("model"):
        tg_sessions.save_model(pane_id, record["model"])
    if record.get("effort"):
        tg_sessions.save_effort(pane_id, record["effort"])
    tg_sessions.save_session_id(pane_id, record["session_id"])
    if record.get("title"):
        def _title(data):
            data.setdefault("pane_to_title", {})[pane_id] = record["title"]
        tg_sessions._with_lock(_title)
    if record.get("topic_name"):
        tg_sessions.save_topic_name(pane_id, record["topic_name"])
    for item in pending:
        tg_sessions.queue_message(pane_id, item["text"], item["message_id"])

    project_name = os.path.basename(record["project"].rstrip("/"))
    response = tg_send(
        f"♻️ Восстанавливаю сессию…\nПроект: {project_name}\nСессия: {session_name}"
        "\nСообщение отправлю, когда CLI будет готов.",
        topic_id=topic_id,
    )
    tg_sessions.save_startup(pane_id, {
        "session": session_name, "project": record["project"],
        "started_at": time.time(),
        "message_id": response.get("result", {}).get("message_id"),
        "resume_session_id": record["session_id"],
        "launch_model": model, "launch_effort": effort, "late_effort": late_effort,
    })
    if response.get("ok"):
        tg_sessions.save_route(str(response["result"]["message_id"]), pane_id)
    tg_sessions.save_reply_to(pane_id, user_msg_id)
    open_terminal_with_tmux(session_name)
    logger.info(
        f"[Resume] agent={agent.value} session={session_name} pane={pane_id} "
        f"topic={topic_id} resume={record['session_id']}"
    )
    return pane_id


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


def _folder_keyboard(rel_path="", agent=AgentKind.CLAUDE):
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
        nav_prefix = "nav:codex:" if agent is AgentKind.CODEX else "nav:"
        keyboard.append([{"text": "← Назад", "callback_data": f"{nav_prefix}{parent}"}])
        keyboard.append([{
            "text": "📌 Выбрать эту папку",
            "callback_data": project_callback_data(agent, rel_path),
        }])

    # Специальные кнопки только в корне
    if not rel_path:
        keyboard.append([
            {
                "text": "🏠 Домашняя директория",
                "callback_data": project_callback_data(agent, "__home__"),
            },
            {
                "text": "🆕 ~/projects",
                "callback_data": project_callback_data(agent, "__projects__"),
            },
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
                {
                    "text": f"📂 {entry}",
                    "callback_data": project_callback_data(agent, entry_rel),
                },
                {
                    "text": f"📁 {entry} →",
                    "callback_data": (
                        f"nav:codex:{entry_rel}"
                        if agent is AgentKind.CODEX else f"nav:{entry_rel}"
                    ),
                },
            ])
            continue
        elif is_proj:
            row.append({
                "text": f"📂 {entry}",
                "callback_data": project_callback_data(agent, entry_rel),
            })
        elif has_nested:
            row.append({
                "text": f"📁 {entry} →",
                "callback_data": (
                    f"nav:codex:{entry_rel}"
                    if agent is AgentKind.CODEX else f"nav:{entry_rel}"
                ),
            })
        else:
            continue

        if len(row) == 2:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)
    return keyboard


def _project_keyboard(agent=AgentKind.CLAUDE):
    """Инлайн-клавиатура с проектами (гибрид: проекты + drill-down)."""
    return _folder_keyboard("", agent=agent)


def handle_projects(msg, agent=AgentKind.CLAUDE):
    """Список проектов с инлайн-кнопками."""
    user_msg_id = msg.get("message_id")
    topic_id = get_msg_topic(msg)

    if not os.path.isdir(PROJECTS_ROOT):
        tg_send("📁 ~/projects не найдена", topic_id=topic_id, reply_to=user_msg_id)
        return

    keyboard = _project_keyboard(agent=agent)
    if not keyboard:
        tg_send("📁 Нет проектов в ~/projects", topic_id=topic_id, reply_to=user_msg_id)
        return

    tg_send(
        f"📁 Выбери проект для {'Codex' if agent is AgentKind.CODEX else 'Claude Code'}:",
        topic_id=topic_id,
        reply_to=user_msg_id,
        reply_markup={"inline_keyboard": keyboard},
    )


def handle_sessions(msg):
    """Показывает активные tmux-сессии Claude Code и Codex."""
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
        if is_managed_session(name):
            label = "Codex" if name.startswith("codex-") else "Claude Code"
            lines.append(f"▸ {label}: <code>{name}</code> — {path}")

    if len(lines) == 1:
        lines.append("Нет сессий Claude Code или Codex")

    tg_send(
        "\n".join(lines),
        topic_id=topic_id,
        reply_to=user_msg_id,
        parse_mode="HTML",
    )


def close_session(session_name):
    """Gracefully close an agent tmux session and its Forum Topic."""
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
    """Удаляет Forum Topic и снимок восстановления: закрытую сессию не поднимаем."""
    tg_resume.forget(topic_id)
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
        tg_resume.forget(topic_id)
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
        if is_managed_session(name):
            keyboard.append([{
                "text": f"❌ {name} — {path}",
                "callback_data": f"close:{name}",
            }])

    if not keyboard:
        tg_send(
            "📋 Нет активных сессий Claude Code или Codex",
            topic_id=topic_id,
            reply_to=user_msg_id,
        )
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
CLAUDE_EFFORT_LEVELS = {
    "low": "быстро, минимум накладных",
    "medium": "сбалансированно",
    "high": "глубже рассуждает",
    "xhigh": "чуть ниже максимума (Opus 4.8/4.7)",
    "ultracode": "xhigh + оркестрация workflow",
}

CODEX_EFFORT_LEVELS = {
    "low": "быстро, лёгкое рассуждение",
    "medium": "баланс скорости и глубины",
    "high": "глубокое рассуждение",
    "xhigh": "максимум основного списка",
    "max": "сложные задачи, повышенный расход",
    "ultra": "оркестрация нескольких агентов",
}

# Порядок и подписи кнопок инлайн-клавиатуры (раскладка по строкам).
CLAUDE_EFFORT_ROWS = [
    [("low", "Low"), ("medium", "Medium"), ("high", "High")],
    [("xhigh", "Xhigh"), ("ultracode", "🧠 Ultracode")],
]

CODEX_EFFORT_ROWS = [
    [("low", "Low"), ("medium", "Medium"), ("high", "High")],
    [("xhigh", "Xhigh"), ("max", "Max"), ("ultra", "Ultra")],
]


def _effort_keyboard(current, agent=AgentKind.CLAUDE):
    """Инлайн-клавиатура выбора эффорта; текущий уровень помечен галочкой."""
    rows = CODEX_EFFORT_ROWS if agent is AgentKind.CODEX else CLAUDE_EFFORT_ROWS
    return [
        [
            {
                "text": f"{'✅ ' if level == current else ''}{label}",
                "callback_data": f"effort:{level}",
            }
            for level, label in row
        ]
        for row in rows
    ]


def _effort_text(current, agent=AgentKind.CLAUDE):
    levels = CODEX_EFFORT_LEVELS if agent is AgentKind.CODEX else CLAUDE_EFFORT_LEVELS
    if current and current in levels:
        return f"⚙️ Эффорт reasoning: *{current}* — {levels[current]}"
    return "⚙️ Выбери уровень reasoning для этой сессии:"


def set_session_effort(pane_id, level, agent=AgentKind.CLAUDE):
    """Отправляет нативную команду `/effort <level>` в pane Claude Code."""
    if not pane_exists(pane_id):
        return False
    if agent is AgentKind.CODEX:
        return _set_codex_effort(pane_id, level)
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

    agent = AgentKind(tg_sessions.get_agent(pane_id))
    levels = CODEX_EFFORT_LEVELS if agent is AgentKind.CODEX else CLAUDE_EFFORT_LEVELS

    parts = text.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""

    if not arg:
        cur = _current_effort(pane_id, agent)
        tg_send(
            _effort_text(cur, agent),
            topic_id=topic_id, parse_mode="Markdown",
            reply_markup={"inline_keyboard": _effort_keyboard(cur, agent)},
        )
        return

    if arg not in levels:
        tg_send(
            "⚠️ Неизвестный уровень. Доступно: "
            + " · ".join(f"`{level}`" for level in levels),
            topic_id=topic_id, parse_mode="Markdown",
        )
        return

    if not set_session_effort(pane_id, arg, agent):
        tg_send("⚠️ Не удалось изменить effort в активной сессии", topic_id=topic_id)
        return
    tg_sessions.save_effort(pane_id, arg)
    tg_send(
        _effort_text(arg, agent),
        topic_id=topic_id, parse_mode="Markdown",
        reply_markup={"inline_keyboard": _effort_keyboard(arg, agent)},
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

# Диалог Claude Code 2.1.29x при смене модели с закэшированной историей:
# "Switch model? … ❯ 1. Yes, switch to Opus 5.5 / 2. No, go back".
CLAUDE_SWITCH_DIALOG_MARKER = "Switch model?"
# Итог /model в транскрипте: "⎿  Set model to Opus 5.5 for this session only",
# "⎿  Set model to Fable 5.1 and saved as your default…", "⎿  Kept model as Fable 5.1".
_CLAUDE_MODEL_RESULT_RE = re.compile(r"(?:Set model to|Kept model as)\s+\S")
CLAUDE_MODEL_SWITCH_TIMEOUT = 4.0
# Пауза перед сверкой через пикер, когда итоговая строка неотличима от прежней.
CLAUDE_MODEL_SWITCH_GRACE = 1.5


def _capture_pane(pane_id):
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", pane_id, "-p"],
        capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else ""


_startup_notices = {}
_resume_screens = {}


def _resume_screen_settled(pane_id, output):
    """True, если экран не изменился с прошлого опроса."""
    previous = _resume_screens.get(pane_id)
    _resume_screens[pane_id] = output
    return previous == output


def sync_topic_titles():
    """Держит названия топиков в актуальном виде: модель, проект, тема.

    Идём по всем топикам, а не только по тем, где агент уже выбрал название:
    модель становится известна при старте сессии и меняется через `/model`,
    и название топика должно это догонять.
    """
    data = tg_sessions._load()
    for pane_id, topic_id in data.get("pane_to_topic", {}).items():
        if not topic_id:
            continue
        title = data.get("pane_to_title", {}).get(pane_id, "")
        project = data.get("pane_to_project", {}).get(pane_id)
        if not project:
            result = subprocess.run(
                ["tmux", "display-message", "-p", "-t", pane_id, "#{session_path}"],
                capture_output=True, text=True,
            )
            if result.returncode != 0 or not result.stdout.strip():
                continue
            project = result.stdout.strip()
            tg_sessions.save_project(pane_id, project)
        name = format_topic_name(
            data.get("pane_to_agent", {}).get(pane_id, "claude"),
            os.path.basename(project.rstrip("/")) or project, title,
            data.get("pane_to_model", {}).get(pane_id, ""),
        )
        # Имя не изменилось — выходим до обращений к tmux и Telegram: цикл
        # watcher-а прокручивается раз в пару секунд, пока сессия стартует.
        if name == data.get("pane_to_topic_name", {}).get(pane_id):
            continue
        if not pane_exists(pane_id):
            continue
        response = tg_api("editForumTopic", {
            "chat_id": CHAT_ID, "message_thread_id": topic_id, "name": name,
        })
        if response.get("ok") or "TOPIC_NOT_MODIFIED" in response.get("description", ""):
            tg_sessions.save_topic_name(pane_id, name)
        else:
            logger.warning("[TopicTitle] Failed to rename topic %s", topic_id)


def _update_fingerprint(options):
    return hashlib.sha256(json.dumps(
        [(o.value, o.label) for o in options], ensure_ascii=False,
    ).encode()).hexdigest()[:12]


def queue_if_blocked(pane_id, text, message_id, topic_id):
    """Return True only when text may be delivered now; persist it otherwise."""
    state = tg_sessions._load()
    agent = AgentKind(tg_sessions.get_agent(pane_id))
    starting = pane_id in state.get("pane_to_startup", {})
    queued = bool(state.get("pane_to_pending", {}).get(pane_id))
    if not starting and not queued:
        if agent is AgentKind.CLAUDE or terminal_ready(
            _capture_pane(pane_id), agent, allow_working=True,
        ):
            return True
    tg_sessions.queue_message(pane_id, text, message_id)
    tg_send("⏳ Сообщение сохранено. Отправлю, когда чат будет готов к вводу.", topic_id=topic_id)
    return False


def check_starting_sessions():
    """Poll startup and drain durable queues in the main thread, without input races."""
    data = tg_sessions._load()
    starting = data.get("pane_to_startup", {})
    pending = data.get("pane_to_pending", {})
    for pane_id in set(starting) | set(pending):
        if not pane_exists(pane_id):
            continue
        state = starting.get(pane_id)
        agent = AgentKind(tg_sessions.get_agent(pane_id))
        topic_id = tg_sessions.get_topic(pane_id)
        output = _capture_pane(pane_id)
        if state and state.get("updating"):
            result = subprocess.run(
                ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_dead} #{pane_dead_status}"],
                capture_output=True, text=True,
            )
            if result.stdout.strip().startswith("1 ") and not state.get("restart_failed"):
                if result.stdout.strip() != "1 0":
                    tg_send("⚠️ Обновление завершилось с ошибкой. Запускаю установленную версию Codex; сообщения сохранены.", topic_id=topic_id)
                argv = build_launch_argv(_spec_from_startup(agent, state))[7:]
                restarted = subprocess.run(
                    ["tmux", "respawn-pane", "-t", pane_id, "-c", state["project"], *argv],
                    capture_output=True, text=True,
                )
                if restarted.returncode == 0:
                    subprocess.run(["tmux", "set-option", "-p", "-t", pane_id, "remain-on-exit", "off"], capture_output=True)
                    state.pop("updating", None)
                    state["started_at"] = time.time()
                    tg_sessions.save_startup(pane_id, state)
                else:
                    state["restart_failed"] = True
                    tg_sessions.save_startup(pane_id, state)
                    tg_send("⚠️ Не удалось перезапустить Codex после обновления. Сообщения сохранены; проверь терминал.\n" + restarted.stderr[-1000:], topic_id=topic_id)
                continue
        if agent is AgentKind.CODEX:
            options = parse_codex_rate_limit_picker(output)
            if options:
                # Keep the user's model; never opt out of reminders globally.
                # Re-capture on the next poll before delivering queued text.
                if _choose_picker_option(pane_id, options, "2"):
                    logger.info("[RateLimit] Kept current model in pane %s; waiting for composer", pane_id)
                continue
        if state:
            keys = claude_trust_navigation_keys(output) if agent is AgentKind.CLAUDE else None
            if agent is AgentKind.CODEX and (
                "Do you trust the contents of this directory?" in output
                and "› 1. Yes, continue" in output and "2. No, quit" in output
            ):
                keys = ("Enter",)
            if keys:
                tmux_send_keys(pane_id, *keys)
                continue
        if terminal_ready(output, agent, allow_working=(agent is AgentKind.CODEX and not state)):
            _startup_notices.pop(pane_id, None)
            if state and state.get("resume_session_id") and not _resume_screen_settled(pane_id, output):
                # Восстановленная сессия перерисовывает историю, и старые строки
                # `❯ …` похожи на строку ввода: ждём два одинаковых снимка подряд.
                continue
            if state:
                model = terminal_model(output, agent)
                if not model and agent is AgentKind.CLAUDE:
                    models = probe_models(pane_id, agent) or []
                    model = next((m["label"] for m in models if m["current"]), "")
                if model:
                    tg_sessions.save_model(pane_id, model)
                effort = terminal_effort(output, agent)
                if effort:
                    tg_sessions.save_effort(pane_id, effort)
                label = "Codex" if agent is AgentKind.CODEX else "Claude Code"
                if state.get("resume_session_id"):
                    label = f"♻️ {label} восстановлен и"
                else:
                    label = f"🚀 {label}"
                text = (f"{label} готов\nПроект: {os.path.basename(state['project'])}"
                        f"\nСессия: {state['session']}\nМодель: {model or 'не удалось определить'}"
                        f"\nEffort: {effort or 'не удалось определить'}")
                response = (tg_edit(state["message_id"], text) if state.get("message_id")
                            else tg_send(text, topic_id=topic_id))
                tg_sessions.save_startup(pane_id, None)
                _resume_screens.pop(pane_id, None)
                if not response.get("ok"):
                    tg_send(text, topic_id=topic_id)
                late_effort = state.get("late_effort")
                if late_effort:
                    # Этот effort флагом запуска не задаётся — применяем командой.
                    if set_session_effort(pane_id, late_effort, agent):
                        tg_sessions.save_effort(pane_id, late_effort)
                    else:
                        tg_send(f"⚠️ Не удалось вернуть effort {late_effort}; проверь /effort", topic_id=topic_id)
                    continue  # очередь — на следующем опросе, после команды /effort
            messages = tg_sessions.pending_messages(pane_id)
            if messages:
                item = messages[0]
                send = tmux_send_codex if agent is AgentKind.CODEX else tmux_send
                delivery = _codex_delivery_context(pane_id) if agent is AgentKind.CODEX else None
                if send(pane_id, item["text"]):
                    tg_sessions.save_reply_to(pane_id, item["message_id"])
                    tg_sessions.ack_message(pane_id, item["message_id"])
                    if delivery:
                        tg_sessions.start_codex_progress(
                            pane_id, delivery[0], "", delivery[1],
                        )
                    tg_react(item["message_id"])
                    mark_pane_waiting(pane_id)
            continue
        options = parse_codex_update_picker(output) if agent is AgentKind.CODEX else []
        if options:
            fingerprint = _update_fingerprint(options)
            if _startup_notices.get(pane_id) != fingerprint:
                response = tg_send(
                    "⬆️ Codex предлагает обновление. Выбери действие; сообщения подождут в очереди.",
                    topic_id=topic_id,
                    reply_markup={"inline_keyboard": [[{
                        "text": option.label[:60],
                        "callback_data": f"startup:{fingerprint}:{option.value}",
                    }] for option in options]},
                )
                if response.get("ok"):
                    _startup_notices[pane_id] = fingerprint
        elif state and time.time() - state["started_at"] > 15 and pane_id not in _startup_notices:
            response = tg_send(
                "⏳ CLI ещё не готов. Если в терминале открыт диалог, заверши его там; сообщения сохранены.\n\n"
                + output.strip()[-1800:], topic_id=topic_id,
            )
            if response.get("ok"):
                _startup_notices[pane_id] = "waiting"


def _input_line(pane_id):
    """Нижняя строка ввода Claude (`❯`) или Codex (`›` / `»`)."""
    lines = [
        line for line in _capture_pane(pane_id).split("\n")
        if line.lstrip().startswith(("❯", "›", "»"))
    ]
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


def _wait_for_picker(pane_id, marker, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        output = _capture_pane(pane_id)
        if marker in output:
            return output
        time.sleep(0.2)
    return ""


def _open_codex_model_picker(pane_id):
    if not pane_exists(pane_id):
        return ""
    output = _capture_pane(pane_id)
    # Recover menus left open by a timeout or an unavailable effort.
    for _ in range(3):
        if "Select Model and Effort" in output and parse_codex_model_picker(output):
            return output
        if "Select Reasoning Level" not in output and "Advanced Reasoning" not in output:
            break
        tmux_send_keys(pane_id, "Escape")
        time.sleep(0.2)
        output = _capture_pane(pane_id)
    if not terminal_ready(output, AgentKind.CODEX) or not _type_command(pane_id, "/model"):
        return ""
    return _wait_for_picker(pane_id, "Select Model and Effort")


def _choose_picker_option(pane_id, options, target):
    try:
        keys = picker_navigation_keys(options, target)
    except ValueError:
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "Escape"])
        return False
    if keys and not tmux_send_keys(pane_id, *keys):
        return False
    return tmux_send_keys(pane_id, "Enter")


def _choose_codex_effort_from_open_picker(pane_id, level):
    output = _wait_for_picker(pane_id, "Select Reasoning Level")
    options = parse_codex_effort_picker(output)
    if not options:
        return False
    if level in ("max", "ultra"):
        if not _choose_picker_option(pane_id, options, "more"):
            return False
        advanced = _wait_for_picker(pane_id, "Advanced Reasoning")
        return _choose_picker_option(
            pane_id,
            parse_codex_effort_picker(advanced),
            level,
        )
    return _choose_picker_option(pane_id, options, level)


def _set_codex_effort(pane_id, level):
    output = _open_codex_model_picker(pane_id)
    models = parse_codex_model_picker(output)
    current = next((option for option in models if option.current), None)
    if current is None or not _choose_picker_option(pane_id, models, current.value):
        return False
    try:
        return (_choose_codex_effort_from_open_picker(pane_id, level)
                and _finish_codex_selection(pane_id, current.value, level))
    finally:
        _close_codex_picker(pane_id)


def _probe_codex_models(pane_id):
    output = _open_codex_model_picker(pane_id)
    try:
        options = parse_codex_model_picker(output)
        if not options:
            return None
        return [
            {
                "alias": option.value,
                "label": option.label,
                "desc": "",
                "current": option.current,
            }
            for option in options
        ]
    finally:
        _close_codex_picker(pane_id)


def probe_models(pane_id, agent=AgentKind.CLAUDE):
    """Открывает пикер /model в pane, парсит модели и активную (✔), закрывает Esc.

    Возвращает [{alias, label, desc, current}] или None, если пикер не открылся.
    """
    if not pane_exists(pane_id):
        return None
    if agent is AgentKind.CODEX:
        return _probe_codex_models(pane_id)
    if not _type_command(pane_id, "/model"):
        return None
    time.sleep(2)
    out = _capture_pane(pane_id)
    if CLAUDE_MODEL_PICKER_MARKER not in out:
        # Пикер не открылся — Escape не шлём (мог бы прервать текущий ход),
        # только чистим возможный остаток ввода.
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "C-u"])
        return None
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "Escape"])
    models = [
        {"alias": o.value, "label": o.label, "desc": o.description, "current": o.current}
        for o in parse_claude_model_picker(out)
    ]
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


def _current_effort(pane_id, agent):
    effort = terminal_effort(_capture_pane(pane_id), agent)
    if effort:
        tg_sessions.save_effort(pane_id, effort)
    return effort or tg_sessions.get_effort(pane_id)


def _model_text(current, effort=""):
    if current:
        return f"🤖 Модель сессии: *{current}*\n⚙️ Effort: *{effort or 'не определён'}*"
    return "🤖 Выбери модель для этой сессии:"


def _set_codex_model(pane_id, alias):
    output = _open_codex_model_picker(pane_id)
    try:
        models = parse_codex_model_picker(output)
        if not models or not _choose_picker_option(pane_id, models, alias):
            return False
        effort = tg_sessions.get_effort(pane_id)
        reasoning_output = _wait_for_picker(pane_id, "Select Reasoning Level")
        reasoning_options = parse_codex_effort_picker(reasoning_output)
        if not reasoning_options:
            return _finish_codex_selection(pane_id, alias)
        available = {option.value for option in reasoning_options}
        if effort in available or (effort in ("max", "ultra") and "more" in available):
            chosen = _choose_codex_effort_from_open_picker(pane_id, effort)
        else:
            current = next((option for option in reasoning_options if option.selected), None)
            chosen = bool(current and _choose_picker_option(pane_id, reasoning_options, current.value))
        return chosen and _finish_codex_selection(pane_id, alias)
    finally:
        _close_codex_picker(pane_id)


def _finish_codex_selection(pane_id, model, effort=None, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        output = _capture_pane(pane_id)
        if terminal_ready(output, AgentKind.CODEX) and terminal_model(output, AgentKind.CODEX) == model:
            actual_effort = terminal_effort(output, AgentKind.CODEX)
            if effort is None or actual_effort == effort:
                if actual_effort:
                    tg_sessions.save_effort(pane_id, actual_effort)
                return True
        time.sleep(0.2)
    return False


def _close_codex_picker(pane_id):
    for _ in range(3):
        output = _capture_pane(pane_id)
        if not any(marker in output for marker in ("Select Model and Effort", "Select Reasoning Level", "Advanced Reasoning")):
            return
        if not tmux_send_keys(pane_id, "Escape"):
            return
        time.sleep(0.2)


def _tmux_type(pane_id, text):
    """Литеральный ввод без интерпретации имён клавиш tmux."""
    result = subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", text], capture_output=True)
    return result.returncode == 0


def _confirm_claude_switch(pane_id, output):
    """В диалоге «Switch model?» вариант 1 «Yes, switch» выделен по умолчанию."""
    if re.search(r"❯\s*2\.", output) and not tmux_send_keys(pane_id, "Up"):
        return False
    return tmux_send_keys(pane_id, "Enter")


def _latest_claude_result(output):
    """Последняя строка `Set model to…`/`Kept model as…` среди пяти непустых
    строк над композером — свежая запись транскрипта, а не история выше."""
    lines = output.split("\n")
    composer = None
    for index in range(len(lines) - 1, -1, -1):
        body = lines[index].lstrip()
        if body.startswith("❯") and not re.match(r"❯\s*\d+\.", body):
            composer = index
            break
    if composer is None:
        return ""
    recent = [line.strip() for line in lines[:composer] if line.strip()][-5:]
    for line in reversed(recent):
        if _CLAUDE_MODEL_RESULT_RE.search(line):
            return line
    return ""


def _claude_model_is(pane_id, alias):
    """Проверка через живой пикер: ✔ стоит на нужной модели."""
    models = probe_models(pane_id) or []
    return any(m["current"] and m["alias"] == alias for m in models)


def _finish_claude_model_switch(pane_id, before, verify=None, timeout=None):
    """Ждёт итог `/model`: подтверждает диалог про закэшированную историю,
    зависший пикер или диалог закрывает Escape.

    Успех — новая строка результата над композером. Если она совпадает с уже
    видимой до команды и счётчик не вырос (старая могла уйти за верх экрана),
    через паузу решает `verify`; без него такой итог считается успехом.
    """
    timeout = CLAUDE_MODEL_SWITCH_TIMEOUT if timeout is None else timeout
    before_result = _latest_claude_result(before)
    before_count = len(_CLAUDE_MODEL_RESULT_RE.findall(before))
    confirmed = False
    started = time.time()
    deadline = started + timeout
    while True:
        output = _capture_pane(pane_id)
        if CLAUDE_SWITCH_DIALOG_MARKER in output:
            if not confirmed:
                confirmed = _confirm_claude_switch(pane_id, output)
        elif CLAUDE_MODEL_PICKER_MARKER not in output:
            result = _latest_claude_result(output)
            if result and (result != before_result
                           or len(_CLAUDE_MODEL_RESULT_RE.findall(output)) > before_count):
                return True
            if result and time.time() - started >= CLAUDE_MODEL_SWITCH_GRACE:
                return verify() if verify else True
        if time.time() >= deadline:
            break
        time.sleep(0.3)
    if CLAUDE_SWITCH_DIALOG_MARKER in output or CLAUDE_MODEL_PICKER_MARKER in output:
        tmux_send_keys(pane_id, "Escape")
    return False


def _set_claude_model(pane_id, alias):
    """Меняет модель только для сессии: пикер `/model` → нужная строка → `s`.

    Прямая команда `/model <имя>` в Claude Code 2.1.29x сохраняет выбор ещё и
    как default для новых сессий, поэтому используется лишь для имён, которых
    в пикере нет (полные идентификаторы). В обоих случаях CLI может спросить
    «Switch model?», если история закэширована под текущую модель.
    """
    before = _capture_pane(pane_id)
    if not _type_command(pane_id, "/model"):
        return False
    options = parse_claude_model_picker(_wait_for_picker(pane_id, CLAUDE_MODEL_PICKER_MARKER))
    if not options:
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "C-u"])
        return False
    if any(option.value == alias for option in options):
        try:
            keys = picker_navigation_keys(options, alias)
        except ValueError:
            tmux_send_keys(pane_id, "Escape")
            return False
        if keys and not tmux_send_keys(pane_id, *keys):
            return False
        if not _tmux_type(pane_id, "s"):
            return False
        verify = None if alias == "default" else (lambda: _claude_model_is(pane_id, alias))
        return _finish_claude_model_switch(pane_id, before, verify)
    # Имени нет в пикере: закрываем его ("Kept model as …" попадёт в before)
    # и отдаём имя напрямую CLI.
    tmux_send_keys(pane_id, "Escape")
    time.sleep(0.5)
    before = _capture_pane(pane_id)
    if not _type_command(pane_id, f"/model {alias}"):
        return False
    return _finish_claude_model_switch(pane_id, before)


def _session_model(pane_id, agent, screen):
    """Модель сессии. У Claude сохранённый выбор свежее приветственной шапки:
    после смены через /model шапка показывает прежнюю модель."""
    if agent is AgentKind.CLAUDE:
        return tg_sessions.get_model(pane_id) or terminal_model(screen, agent)
    return terminal_model(screen, agent) or tg_sessions.get_model(pane_id)


def set_session_model(pane_id, alias, agent=AgentKind.CLAUDE):
    """Меняет модель через нативный интерфейс активного агента."""
    if not pane_exists(pane_id):
        return False
    if agent is AgentKind.CODEX:
        return _set_codex_model(pane_id, alias)
    return _set_claude_model(pane_id, alias)


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

    agent = AgentKind(tg_sessions.get_agent(pane_id))

    parts = text.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""

    if not arg:
        effort = _current_effort(pane_id, agent)
        models = probe_models(pane_id, agent)
        note = ""
        if models is None:
            if agent is AgentKind.CODEX:
                saved = tg_sessions.get_model(pane_id)
                models = ([{
                    "alias": saved,
                    "label": saved,
                    "desc": "",
                    "current": True,
                }] if saved else [])
            else:
                models = MODEL_FALLBACK
            if agent is AgentKind.CODEX and not terminal_ready(_capture_pane(pane_id), agent):
                note = "\n⏳ Сейчас агент занят — открой /model после завершения ответа"
            else:
                note = "\n⚠️ Не смог прочитать пикер — список может быть неполным"
        current = next(
            (m["alias"] for m in models if m["current"]),
            tg_sessions.get_model(pane_id),
        )
        if current:
            tg_sessions.save_model(pane_id, current)
        tg_send(
            _model_text(current, effort) + note,
            topic_id=topic_id, parse_mode="Markdown",
            reply_markup={"inline_keyboard": _model_keyboard(models, current) + _effort_keyboard(effort, agent)},
        )
        return

    if not re.fullmatch(r"[a-z0-9.\-]+", arg):
        tg_send("⚠️ Некорректное имя модели", topic_id=topic_id)
        return
    if not set_session_model(pane_id, arg, agent):
        tg_send("⚠️ Не удалось изменить модель в активной сессии", topic_id=topic_id)
        return
    tg_sessions.save_model(pane_id, arg)
    effort = _current_effort(pane_id, agent)
    tg_send(_model_text(arg, effort), topic_id=topic_id, parse_mode="Markdown",
            reply_markup={"inline_keyboard": _effort_keyboard(effort, agent)})


# Один кадр /usage вмещает не все окна лимитов в узком терминале, поэтому
# оверлей прокручивается и кадры накапливаются (парсер схлопывает повторы).
STATS_SCROLL_PAGES = 6
STATS_SCROLL_STEP = ("Down",) * 8
# Шапка оверлея /usage: она закреплена сверху при любой ширине терминала,
# тогда как блоки лимитов в узком окне уходят ниже кадра.
CLAUDE_USAGE_OVERLAY_RE = re.compile(r"Settings\s+Status\s+Config\s+Usage\s+Stats")
# Конец блока /context. Его шапку с "38.5k/1m tokens" узкий терминал выталкивает
# за кадр, поэтому ждать появления именно её нельзя.
CLAUDE_CONTEXT_TAIL_RE = re.compile(r"Auto-compact window:|/context all to expand")
CODEX_STATUS_RE = re.compile(re.escape(tg_stats.CODEX_STATUS_MARKER))


def _wait_for_render(pane_id, pattern, before, timeout=8.0):
    """Ждёт, пока вывод команды отрисуется и экран перестанет меняться.

    Ориентироваться на первое появление маркера нельзя: вывод предыдущего
    `/context` или `/status` ещё виден, и проверка проходит до перерисовки.
    """
    started = time.time()
    deadline = started + timeout
    previous = None
    while time.time() < deadline:
        output = _capture_pane(pane_id)
        if output != before and pattern.search(output):
            # Обычно ждём остановки перерисовки, но анимация в футере может
            # никогда не дать двух одинаковых кадров — тогда берём последний.
            if output == previous or time.time() - started > timeout / 2:
                return output
        previous = output
        time.sleep(0.35)
    return ""


def _collect_usage_screen(pane_id, first_page):
    """Снимает оверлей /usage целиком, прокручивая его вниз.

    Down-клавиши безопасны только внутри оверлея: на обычном композере они
    листают историю сообщений и оставляют мусор в строке ввода.
    """
    pages = [first_page]
    for _ in range(STATS_SCROLL_PAGES):
        if not CLAUDE_USAGE_OVERLAY_RE.search(pages[-1]):
            break
        if not tmux_send_keys(pane_id, *STATS_SCROLL_STEP):
            break
        time.sleep(0.45)
        page = _capture_pane(pane_id)
        if page == pages[-1]:
            break
        pages.append(page)
    return "\n".join(pages)


def _close_claude_overlay(pane_id):
    """Закрывает оверлей /usage. Второй Escape шлём только если он ещё открыт:
    на пустом композере двойной Escape открывает выбор точки возврата."""
    for _ in range(2):
        if not CLAUDE_USAGE_OVERLAY_RE.search(_capture_pane(pane_id)):
            return
        tmux_send_keys(pane_id, "Escape")
        time.sleep(0.4)


def _claude_transcript_context(pane_id, window):
    """Фолбэк по контексту: точные токены последнего запроса из транскрипта."""
    record = tg_stats.find_claude_session(pane_id)
    if not record:
        return None
    session_id = record.get("sessionId") or ""
    if session_id:
        tg_sessions.save_session_id(pane_id, session_id)
    return tg_stats.read_claude_transcript_context(
        tg_stats.claude_transcript_path(record), window,
    )


def probe_claude_stats(pane_id):
    """Контекст через `/context`, лимиты плана через `/usage`."""
    pane = _capture_pane(pane_id)
    stats = tg_stats.SessionStats(
        agent=AgentKind.CLAUDE.value,
        effort=_current_effort(pane_id, AgentKind.CLAUDE),
    )
    if not terminal_ready(pane, AgentKind.CLAUDE):
        stats.model = _session_model(pane_id, AgentKind.CLAUDE, pane)
        stats.context = _claude_transcript_context(
            pane_id, tg_stats.claude_context_window(pane),
        )
        stats.notes.append("Сессия занята — лимиты читаются только в простое")
        return stats

    window = tg_stats.claude_context_window(pane)
    if _type_command(pane_id, "/context"):
        screen = _wait_for_render(pane_id, CLAUDE_CONTEXT_TAIL_RE, pane)
        stats.context = tg_stats.parse_claude_context(screen)
        window = tg_stats.parse_claude_autocompact_window(screen) or window
        # Блок /context — единственный живой источник модели сессии.
        stats.model = tg_stats.parse_claude_context_model(screen)
    stats.model = stats.model or _session_model(pane_id, AgentKind.CLAUDE, pane)
    if stats.context is None:
        # Узкий терминал выталкивает шапку блока за кадр: токены берём из
        # транскрипта, размер окна — из уцелевшей строки Auto-compact window.
        stats.context = _claude_transcript_context(pane_id, window)
    if stats.context is None:
        stats.notes.append("Контекст не прочитал: в сессии ещё нет ответов модели")

    before = _capture_pane(pane_id)
    if _type_command(pane_id, "/usage"):
        screen = _wait_for_render(pane_id, CLAUDE_USAGE_OVERLAY_RE, before)
        if screen:
            stats.limits = tg_stats.parse_claude_usage(
                _collect_usage_screen(pane_id, screen),
            )
            _close_claude_overlay(pane_id)
    if not stats.limits:
        stats.notes.append("Не смог прочитать экран /usage")
    return stats


def probe_codex_stats(pane_id):
    """`/status` отдаёт модель, план, контекст и лимиты одним экраном."""
    pane = _capture_pane(pane_id)
    stats = None
    if terminal_ready(pane, AgentKind.CODEX) and _type_command(pane_id, "/status"):
        screen = _wait_for_render(pane_id, CODEX_STATUS_RE, pane)
        if screen:
            stats = tg_stats.parse_codex_status(screen)
            if stats.session_id:
                tg_sessions.save_session_id(pane_id, stats.session_id)
    if stats is None:
        stats = tg_stats.SessionStats(
            agent=AgentKind.CODEX.value,
            model=terminal_model(pane, AgentKind.CODEX),
            effort=_current_effort(pane_id, AgentKind.CODEX),
            session_id=tg_sessions.get_session_id(pane_id),
        )
        stats.notes.append("Данные из журнала сессии: /status не открылся")
    if stats.context and stats.limits:
        return stats

    # Экран занят, обрезан или показал не всё — добираем снимок из rollout-журнала.
    payload = tg_stats.read_codex_token_count(
        tg_stats.find_codex_rollout(stats.session_id),
    )
    stats.context = stats.context or tg_stats.codex_context_from_payload(payload)
    stats.limits = stats.limits or tg_stats.codex_limits_from_payload(payload)
    return stats


def handle_stats(msg):
    """`/stats` — контекст окна и лимиты плана для сессии текущего топика."""
    user_msg_id = msg.get("message_id")
    topic_id = get_msg_topic(msg)

    if not topic_id:
        tg_send("📊 /stats работает только внутри топика сессии", reply_to=user_msg_id)
        return

    pane_id = tg_sessions.get_pane_by_topic(topic_id)
    if not pane_id or not pane_exists(pane_id):
        tg_send("⚠️ Нет активной сессии в этом топике", topic_id=topic_id)
        return

    agent = AgentKind(tg_sessions.get_agent(pane_id))
    tg_react(user_msg_id)
    probe = probe_codex_stats if agent is AgentKind.CODEX else probe_claude_stats
    stats = probe(pane_id)
    tg_send(tg_stats.render_stats(stats), topic_id=topic_id, parse_mode="Markdown")


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
    live = live_tmux_panes()
    try:
        tg_resume.refresh(data, live)
    except OSError as e:
        logger.error(f"[Resume] snapshot refresh failed: {e}")
    pane_to_topic = data.get("pane_to_topic", {})
    for pane_id, topic_id in list(pane_to_topic.items()):
        if not pane_exists(pane_id):
            # Снимок восстановления не трогаем: по нему следующее сообщение
            # в топике поднимет сессию с той же историей.
            restorable = tg_resume.resume_decision(tg_resume.get(topic_id)) == tg_resume.RESTORE
            tg_send(
                "⚠️ Сессия завершилась" + (
                    ". Напиши сообщение — подниму её с той же историей." if restorable else ""
                ),
                topic_id=topic_id,
            )
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

        snapshot = _capture_pane_content(pane_id)
        if snapshot != activity["last_snapshot"]:
            activity["last_snapshot"] = snapshot
            activity["last_change"] = now
            activity["notified_working"] = False
            activity["notified_stuck"] = False
            continue

        idle_time = now - activity["last_change"]
        agent_name = (
            "Codex"
            if tg_sessions.get_agent(pane_id) == AgentKind.CODEX.value
            else "Claude"
        )

        if idle_time >= ACTIVITY_STUCK_TIMEOUT and not activity["notified_stuck"]:
            tg_send(
                f"⚠️ {agent_name} не отвечает уже 5 минут — возможно завис",
                topic_id=topic_id,
            )
            activity["notified_stuck"] = True
            logger.info(f"[Activity] pane {pane_id} stuck ({idle_time:.0f}s)")
        elif idle_time >= ACTIVITY_WORKING_TIMEOUT and not activity["notified_working"]:
            tg_send(f"⏳ {agent_name} всё ещё работает...", topic_id=topic_id)
            activity["notified_working"] = True
            logger.info(f"[Activity] pane {pane_id} still working ({idle_time:.0f}s)")


def _codex_commentary(record):
    if record.get("type") != "response_item":
        return ""
    item = record.get("payload", {})
    if item.get("type") != "message" or item.get("role") != "assistant" or item.get("phase") != "commentary":
        return ""
    return "".join(
        part.get("text", "") for part in item.get("content", [])
        if isinstance(part, dict) and part.get("type") == "output_text"
    ).strip()


def _codex_user_message_id(record):
    if record.get("type") != "response_item":
        return None
    item = record.get("payload", {})
    if item.get("type") != "message" or item.get("role") != "user":
        return None
    for part in item.get("content", []):
        if isinstance(part, dict) and part.get("type") == "input_text":
            message_id = message_id_from_prompt(part.get("text", ""))
            if message_id is not None:
                return message_id
    return None


def _codex_progress_records(path, offset, limit=100):
    """Read complete new JSONL records; leave a partially written line for later."""
    with open(path, "rb") as handle:
        handle.seek(offset)
        for _ in range(limit):
            line = handle.readline(2 * 1024 * 1024 + 1)
            if not line:
                break
            if not line.endswith(b"\n"):
                if len(line) <= 2 * 1024 * 1024:
                    break
                # Oversized tool output: skip the record without loading it.
                while line and not line.endswith(b"\n"):
                    line = handle.readline(64 * 1024)
                if line:
                    yield handle.tell(), {}
                continue
            end = handle.tell()
            try:
                yield end, json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                yield end, {}


def poll_codex_progress():
    """Forward only assistant commentary from Telegram-initiated Codex turns."""
    states = tg_sessions._load().get("pane_to_codex_progress", {})
    for pane_id, state in states.items():
        if not state.get("active") or tg_sessions.get_agent(pane_id) != AgentKind.CODEX.value:
            continue
        session_id = state.get("session_id", "")
        path = tg_stats.find_codex_rollout(session_id)
        if not path:
            continue
        try:
            records = list(_codex_progress_records(path, state.get("offset", 0)))
        except OSError:
            continue
        turn_id = state.get("turn_id", "")
        completed = any(
            record.get("type") == "event_msg"
            and record.get("payload", {}).get("type") == "task_complete"
            and (not turn_id or record["payload"].get("turn_id") == turn_id)
            for _, record in records
        )
        topic_id = tg_sessions.get_topic(pane_id)
        for offset, record in records:
            message_id = _codex_user_message_id(record)
            if message_id is not None:
                response = _set_reaction(message_id, REACTION_READING, quick=True)
                if not response.get("ok"):
                    return
            message = _codex_commentary(record)
            if message and not completed and topic_id:
                # Plain text avoids Telegram HTML parse errors for arbitrary Markdown.
                reply_to = tg_sessions.get_reply_to(pane_id)
                chunks = [message[i:i + 3500] for i in range(0, len(message), 3500)]
                for chunk in chunks:
                    response = tg_send("💬 Codex: " + chunk, topic_id=topic_id, reply_to=reply_to)
                    if not response.get("ok"):
                        return
            tg_sessions.update_codex_progress(pane_id, session_id, turn_id, offset)
        if completed and records:
            tg_sessions.update_codex_progress(pane_id, session_id, turn_id, records[-1][0], active=False)


def handle_callback(callback_query):
    """Обработка нажатий инлайн-кнопок."""
    cb_id = callback_query.get("id")
    data = callback_query.get("data", "")
    msg = callback_query.get("message", {})

    if data.startswith("startup:"):
        parts = data.split(":")
        topic_id = get_msg_topic(msg)
        pane_id = tg_sessions.get_pane_by_topic(topic_id) if topic_id else ""
        options = (parse_codex_update_picker(_capture_pane(pane_id))
                   if pane_id and tg_sessions.get_agent(pane_id) == AgentKind.CODEX.value else [])
        if len(parts) != 3 or not options or _update_fingerprint(options) != parts[1]:
            tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Кнопка устарела: диалог уже изменился", "show_alert": True})
            return
        option = next((o for o in options if o.value == parts[2]), None)
        if option is None:
            return
        if option.label.lower().startswith("update"):
            state = tg_sessions._load().get("pane_to_startup", {}).get(pane_id)
            if state is None:
                tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Обнови эту сессию в терминале; сообщения сохранены", "show_alert": True})
                return
            result = subprocess.run(["tmux", "set-option", "-p", "-t", pane_id, "remain-on-exit", "on"], capture_output=True)
            if result.returncode != 0:
                return
            state["updating"] = True
            tg_sessions.save_startup(pane_id, state)
        chosen = _choose_picker_option(pane_id, options, option.value)
        tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Выбрано: " + option.label if chosen else "Не удалось выбрать действие"})
        if chosen:
            tg_edit(msg.get("message_id"), "⏳ " + option.label, reply_markup={"inline_keyboard": []})
            _startup_notices.pop(pane_id, None)
        return

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

        if tg_sessions.get_agent(pane_id) != AgentKind.CLAUDE.value:
            tg_api("answerCallbackQuery", {
                "callback_query_id": cb_id,
                "text": "⚠️ Codex запущен без approval-диалогов",
                "show_alert": True,
            })
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
        payload = data[6:]
        if payload.startswith("codex:"):
            agent = AgentKind.CODEX
            project = payload[6:]
        else:
            agent = AgentKind.CLAUDE
            project = payload
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
        agent_arg = "codex " if agent is AgentKind.CODEX else ""
        handle_newstart(msg, f"/newstart {agent_arg}{project}")
    elif data.startswith("nav:"):
        payload = data[4:]
        if payload.startswith("codex:"):
            agent = AgentKind.CODEX
            rel_path = payload[6:]
        else:
            agent = AgentKind.CLAUDE
            rel_path = payload
        if ".." in rel_path:
            tg_api("answerCallbackQuery", {
                "callback_query_id": cb_id,
                "text": "⚠️ Недопустимый путь",
                "show_alert": True,
            })
            return
        tg_api("answerCallbackQuery", {"callback_query_id": cb_id})
        chat_msg_id = msg.get("message_id")

        keyboard = _folder_keyboard(rel_path, agent=agent)
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
                    if is_managed_session(name):
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
        agent = AgentKind(tg_sessions.get_agent(pane_id))
        levels = CODEX_EFFORT_LEVELS if agent is AgentKind.CODEX else CLAUDE_EFFORT_LEVELS
        if level not in levels:
            tg_api("answerCallbackQuery", {"callback_query_id": cb_id})
            return

        if not set_session_effort(pane_id, level, agent):
            tg_api("answerCallbackQuery", {
                "callback_query_id": cb_id,
                "text": "⚠️ Не удалось изменить effort",
                "show_alert": True,
            })
            return
        tg_sessions.save_effort(pane_id, level)
        tg_api("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": f"Эффорт: {level}",
        })
        model = _session_model(pane_id, agent, _capture_pane(pane_id))
        model_rows = [
            [button for button in row if button.get("callback_data", "").startswith("model:")]
            for row in (msg.get("reply_markup") or {}).get("inline_keyboard", [])
        ]
        tg_edit(
            chat_msg_id,
            _model_text(model, level) if model else _effort_text(level, agent),
            parse_mode="Markdown",
            reply_markup={"inline_keyboard": [row for row in model_rows if row] + _effort_keyboard(level, agent)},
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

        agent = AgentKind(tg_sessions.get_agent(pane_id))
        if not set_session_model(pane_id, alias, agent):
            tg_api("answerCallbackQuery", {
                "callback_query_id": cb_id,
                "text": "⚠️ Не удалось изменить модель",
                "show_alert": True,
            })
            return
        tg_sessions.save_model(pane_id, alias)
        effort = _current_effort(pane_id, agent)
        tg_api("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": f"Модель: {alias} · {effort or 'effort не определён'}",
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
                if not cb_data.startswith("model:"):
                    continue
                new_row.append({
                    "text": ("✅ " + base) if cb_data == data else base,
                    "callback_data": cb_data,
                })
            if new_row:
                new_kb.append(new_row)
        tg_edit(
            chat_msg_id,
            _model_text(alias, effort),
            parse_mode="Markdown",
            reply_markup={"inline_keyboard": new_kb + _effort_keyboard(effort, agent)},
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
    # Меню Telegram держим коротким: /newstart, /projects, /sessions и /cleanup
    # из него убраны, но обработчики на месте — команды работают, если набрать
    # их руками. /close идёт последней, чтобы не жать её по ошибке.
    commands = json.dumps([
        {"command": "newcodex", "description": "🤖 Запустить Codex"},
        {"command": "newclaude", "description": "🟠 Запустить Claude Code"},
        {"command": "effort", "description": "⚙️ Эффорт активного агента"},
        {"command": "model", "description": "🤖 Модель активного агента"},
        {"command": "stats", "description": "📊 Контекст сессии и лимиты плана"},
        {"command": "close", "description": "🔴 Закрыть сессию"},
    ])
    tg_api("setMyCommands", {"commands": commands})
    tg_api("setMyCommands", {
        "commands": commands,
        "scope": json.dumps({"type": "all_group_chats"}),
    })
    logger.info("Telegram Reply Watcher started")
    backoff = 5

    while True:
        try:
            check_starting_sessions()
            sync_topic_titles()
            poll_codex_progress()
        except Exception:
            logger.exception("[Startup] Cannot process startup or pending messages")
        session_data = tg_sessions._load()
        waiting = bool(
            session_data.get("pane_to_startup") or session_data.get("pane_to_pending")
            or any(s.get("active") for s in session_data.get("pane_to_codex_progress", {}).values())
            or "codex" in session_data.get("pane_to_agent", {}).values()
        )
        offset = get_offset()
        data = tg_api("getUpdates", {
            "offset": offset,
            "timeout": 2 if waiting else 30,
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
            cmd_text, command = parse_bot_command(msg.get("text", ""))
            if command in ("/newstart", "/newcodex", "/newclaude"):
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
            if command in ("/stats", "/usage"):
                try:
                    handle_stats(msg)
                except Exception as e:
                    logger.error(f"[Stats] {e}")
                continue

            # Текст, голосовое или медиа (фото/видео/GIF/кружок/аудио/документ)
            text = msg.get("text", "").strip()
            voice = msg.get("voice")
            user_msg_id = msg.get("message_id")
            media_file_id, media_name, media_size, media_label = extract_media(msg, user_msg_id)

            if not text and voice:
                file_id = voice.get("file_id")
                if file_id:
                    tg_react(user_msg_id, REACTION_VOICE)
                    text = transcribe_voice(file_id)
                    if not text:
                        logger.info("Voice transcription failed, skipping")
                        continue

            # Caption от медиа
            if not text and media_file_id:
                text = msg.get("caption", "").strip()

            if not text and not media_file_id:
                continue

            # Маршрутизация: топик → pane (приоритет), fallback на reply
            topic_id = get_msg_topic(msg)
            pane_id = None

            if topic_id:
                pane_id = tg_sessions.get_pane_by_topic(topic_id)
                if not (pane_id and pane_exists(pane_id)):
                    # Нет живого pane (перезагрузка, падение tmux): поднимаем
                    # сессию по снимку, сообщение встанет в очередь запуска.
                    try:
                        restored = restore_topic_session(topic_id, user_msg_id)
                    except Exception:
                        logger.exception(f"[Resume] topic {topic_id} failed")
                        tg_send("⚠️ Не удалось восстановить сессию, подробности в логе watcher", topic_id=topic_id)
                        continue
                    if restored == "":
                        continue
                    if restored:
                        pane_id = restored

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
            if media_file_id:
                if media_size and media_size > TG_DOWNLOAD_LIMIT:
                    media_path = None
                    media_error = f"{media_size / 1024 / 1024:.0f} МБ, а Bot API отдаёт ботам максимум 20 МБ"
                else:
                    media_path = download_media(media_file_id, pane_id, media_name)
                    media_error = None if media_path else "Telegram не отдал файл (обычно он больше 20 МБ)"

                if media_path:
                    prefix = f"[Файл ({media_label}): {media_path}]"
                    text = f"{prefix} {text}" if text else prefix
                else:
                    logger.warning(f"[Media] {media_label}: {media_error}")
                    tg_react(user_msg_id, REACTION_REJECTED)
                    tg_send(
                        f"⚠️ Не принял «{media_label}»: {media_error}. "
                        "Сожми файл, вырежи фрагмент или пришли локальный путь.",
                        topic_id=topic_id,
                    )
                    if not text:
                        continue

            if not text:
                continue

            # Quick replies для permission-промптов (Do you want to make this edit?)
            lower = text.lower()
            is_claude = tg_sessions.get_agent(pane_id) == AgentKind.CLAUDE.value
            if (
                is_claude
                and lower in ("y", "yes", "да", "1", "ок", "ok", "+")
                and is_permission_prompt(pane_id)
            ):
                logger.info(f"Permission confirm -> pane {pane_id}")
                sent = tmux_send_keys(pane_id, "Enter")
            elif (
                is_claude
                and lower in ("n", "no", "нет", "3", "-")
                and is_permission_prompt(pane_id)
            ):
                logger.info(f"Permission decline -> pane {pane_id}")
                sent = tmux_send_keys(pane_id, "Escape")
            else:
                # Правила ответа и переименования топика живут в скилле tg-chat,
                # префикс служит его триггером.
                text = agent_prompt(text, AgentKind.CLAUDE if is_claude else AgentKind.CODEX, user_msg_id)
                if not queue_if_blocked(pane_id, text, user_msg_id, topic_id):
                    continue
                logger.info("Reply: %s chars -> pane %s", len(text), pane_id)
                delivery = _codex_delivery_context(pane_id) if not is_claude else None
                sent = (
                    tmux_send(pane_id, text)
                    if is_claude
                    else tmux_send_codex(pane_id, text)
                )

            if sent:
                tg_sessions.save_reply_to(pane_id, user_msg_id)
                if not is_claude and delivery:
                    tg_sessions.start_codex_progress(
                        pane_id, delivery[0], "", delivery[1],
                    )
                tg_react(user_msg_id)
                mark_pane_waiting(pane_id)
                logger.info("  OK")
            else:
                if pane_exists(pane_id):
                    tg_sessions.queue_message(pane_id, text, user_msg_id)
                    tg_send("⏳ Не удалось передать сообщение. Оно сохранено для повторной отправки.", topic_id=topic_id)
                else:
                    restorable = topic_id and tg_resume.resume_decision(tg_resume.get(topic_id)) == tg_resume.RESTORE
                    tg_send(
                        "⚠️ Сессия завершилась. Сообщение не отправлено — "
                        + ("повтори его, и я подниму сессию с той же историей." if restorable
                           else "создай новый чат и повтори его."),
                        topic_id=topic_id,
                    )
                    cleanup_media(pane_id)
                    tg_sessions.cleanup_pane(pane_id)

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
