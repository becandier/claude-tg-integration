#!/usr/bin/env python3
"""Сбор статистики сессии: контекст окна и лимиты плана Claude Code / Codex.

Модуль не трогает tmux и Telegram — только парсит текст, снятый с pane,
и читает журналы сессий на диске. Это позволяет тестировать его на фикстурах.

Источники данных:
  Claude Code  — `/context` (контекст) и `/usage` (лимиты) в TUI;
                 фолбэк по контексту — транскрипт ~/.claude/projects/<proj>/<id>.jsonl
  Codex CLI    — `/status` в TUI (лимиты, план, session id);
                 контекст — событие token_count в rollout-журнале сессии
"""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field

CLAUDE_SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CODEX_HOME = os.path.expanduser("~/.codex")

CLAUDE_LONG_WINDOW = 1_000_000

_SESSION_ID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,63}$")


@dataclass(frozen=True)
class LimitWindow:
    """Одно окно лимита: процент израсходованного и время сброса."""

    name: str
    used_percent: float
    resets: str = ""
    group: str = ""


@dataclass(frozen=True)
class ContextUsage:
    used_tokens: int
    total_tokens: int
    percent: float
    breakdown: tuple[tuple[str, int], ...] = ()
    approximate: bool = False


@dataclass
class SessionStats:
    agent: str
    model: str = ""
    effort: str = ""
    plan: str = ""
    session_id: str = ""
    context: ContextUsage | None = None
    limits: list[LimitWindow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Числа и строки
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmg]?)$", re.I)
_MULTIPLIER = {"": 1, "k": 1_000, "m": 1_000_000, "g": 1_000_000_000}


def parse_token_count(raw: str) -> int:
    """`38.5k` → 38500, `1m` → 1000000, `314` → 314. Нераспознанное → 0."""
    match = _TOKEN_RE.match((raw or "").strip().replace(",", ""))
    if not match:
        return 0
    return int(float(match.group(1)) * _MULTIPLIER[match.group(2).lower()])


def format_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"{value / 1_000:.1f}k".replace(".0k", "k")
    return str(value)


def progress_bar(percent: float, width: int = 10) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)


_CLAUDE_LIMIT_LABELS = {
    "current session": "Сессия",
    "current week (all models)": "Неделя · все модели",
    "session": "Сессия",
}
_CODEX_LIMIT_LABELS = {
    "weekly limit": "Неделя",
    "5h limit": "5 часов",
    "hourly limit": "Час",
    "monthly limit": "Месяц",
}


def humanize_limit_name(name: str) -> str:
    """Переводит известные подписи лимитов; незнакомые оставляет как есть."""
    key = name.strip().lower()
    for table in (_CLAUDE_LIMIT_LABELS, _CODEX_LIMIT_LABELS):
        if key in table:
            return table[key]
    match = re.fullmatch(r"current week \((.+)\)", key)
    if match:
        return f"Неделя · {name.strip()[14:-1]}"
    return name.strip()


# ---------------------------------------------------------------------------
# Claude Code: /usage
# ---------------------------------------------------------------------------

# "   ████████████████                                   32% used"
_CC_PERCENT_RE = re.compile(r"^[\s█▓▒░▏▎▍▌▋▊▉]*?([\d.]+)%\s+used\b")
_CC_RESETS_RE = re.compile(r"^Resets\b(.*?)[\s↑↓]*$")


def parse_claude_usage(output: str) -> list[LimitWindow]:
    """Вытаскивает блоки лимитов из экрана `/usage`.

    Блок выглядит так:
        Current week (Fable)
        ██████████████  52% used
        Resets Sep 12 at 11am (Europe/Moscow)
    Подпись берём из ближайшей строки выше, чтобы не зависеть от набора окон
    (состав зависит от плана: сессия, неделя, отдельные модели).
    """
    lines = output.splitlines()
    found: dict[str, LimitWindow] = {}

    for index, line in enumerate(lines):
        match = _CC_PERCENT_RE.match(line.strip())
        if not match:
            continue
        name = _preceding_label(lines, index)
        if not name:
            continue
        resets = ""
        for follow in lines[index + 1:index + 3]:
            resets_match = _CC_RESETS_RE.match(follow.strip())
            if resets_match:
                resets = resets_match.group(1).strip()
                break
        # Экран /usage собирается из нескольких прокрученных кадров, поэтому
        # один блок встречается не раз; на границе кадра строка Resets может
        # быть обрезана — достраиваем запись из более полного повтора.
        previous = found.get(name)
        if previous and (not resets or previous.resets):
            continue
        found[name] = LimitWindow(
            name=humanize_limit_name(name),
            used_percent=float(match.group(1)),
            resets=resets,
        )
    return list(found.values())


def _preceding_label(lines: list[str], index: int) -> str:
    """Ближайшая осмысленная строка-заголовок выше строки с процентом."""
    for candidate in reversed(lines[max(0, index - 4):index]):
        text = candidate.strip()
        if not text or len(text) > 60:
            continue
        if _CC_PERCENT_RE.match(text) or _CC_RESETS_RE.match(text):
            continue
        if ":" in text or text.startswith(("+", "⚠", "↑", "↓")):
            continue
        return text
    return ""


# ---------------------------------------------------------------------------
# Claude Code: /context
# ---------------------------------------------------------------------------

# "     ⛶ ⛶ ⛶ ...   38.5k/1m tokens (4%)"
_CC_CONTEXT_RE = re.compile(
    r"([\d.]+[kmg]?)\s*/\s*([\d.]+[kmg]?)\s+tokens\s*\(([\d.]+)%\)", re.I,
)
# "⛶ ⛶ ⛶ ...   ⛁ System tools: 20.1k tokens (2.0%)" — слева от подписи идёт
# сетка из тех же символов, поэтому подпись не должна их содержать.
_CC_GRID_GLYPHS = "⛁⛀⛶⛝⛃"
# Процент может уехать на следующую строку при узком окне, поэтому после числа
# достаточно слова tokens или открывающей скобки.
_CC_BREAKDOWN_RE = re.compile(
    rf"[{_CC_GRID_GLYPHS}]\s([A-Za-z][^:{_CC_GRID_GLYPHS}]{{1,38}}?):\s*([\d.]+[kmg]?)(?=\s*(?:tokens|\())",
    re.I,
)
_CC_SKIP_CATEGORIES = ("free space", "autocompact buffer")
# "     Auto-compact window: 1m tokens" — единственная короткая строка с
# размером окна, она остаётся видимой даже когда шапка блока ушла за кадр.
_CC_AUTOCOMPACT_RE = re.compile(r"Auto-compact window:\s*([\d.]+[kmg]?)\s*tokens", re.I)


# Блок /context подписан названием модели: "Opus 5 (1M context)". Это надёжнее
# приветственной шапки CLI, которая в длинной сессии уже ушла за кадр.
_CC_CONTEXT_MODEL_RE = re.compile(
    r"\b((?:Opus|Sonnet|Haiku|Fable)\s+[\d.]+(?:\s*\([^)]*context\))?)", re.I,
)


def parse_claude_context_model(output: str) -> str:
    """Название модели из блока `/context`; пустая строка, если его нет."""
    match = _CC_CONTEXT_MODEL_RE.search(output or "")
    return match.group(1).strip() if match else ""


def parse_claude_autocompact_window(output: str) -> int:
    """Размер окна контекста из блока `/context`; 0 — если строки нет."""
    match = _CC_AUTOCOMPACT_RE.search(output or "")
    return parse_token_count(match.group(1)) if match else 0


def parse_claude_context(output: str) -> ContextUsage | None:
    """Парсит блок `Context Usage`, отрендеренный командой `/context`."""
    match = None
    for line in output.splitlines():
        found = _CC_CONTEXT_RE.search(line)
        if found:
            match = found
    if not match:
        return None

    used = parse_token_count(match.group(1))
    total = parse_token_count(match.group(2))
    breakdown = []
    for line in output.splitlines():
        items = _CC_BREAKDOWN_RE.findall(line)
        if not items:
            continue
        label, amount = items[-1]
        name = label.strip()
        if name.lower() in _CC_SKIP_CATEGORIES:
            continue
        breakdown.append((name, parse_token_count(amount)))
    return ContextUsage(
        used_tokens=used,
        total_tokens=total,
        percent=float(match.group(3)),
        breakdown=tuple(breakdown),
    )


# ---------------------------------------------------------------------------
# Codex CLI: /status
# ---------------------------------------------------------------------------

# "│  Weekly limit:   [██░░░░] 10% left (resets 11:49 on 15 Sep)  │"
_CX_LIMIT_RE = re.compile(
    r"^(.+?):\s*\[[^\]]*\]\s*([\d.]+)%\s+(left|used)\b(?:\s*\(([^)]*)\))?",
)
# "│  GPT-5.3-Codex-Spark limit:                   │" — заголовок группы
_CX_GROUP_RE = re.compile(r"^(.+?\blimits?):\s*$", re.I)
_CX_FIELD_RE = re.compile(r"^([A-Za-z][\w .\-/]*?):\s{2,}(.+?)\s*$")
# "│  Context window:  97% left (19.5K used / 258K)  │"
_CX_CONTEXT_RE = re.compile(
    r"([\d.]+)%\s+left\s*\(\s*([\d.]+[kmg]?)\s+used\s*/\s*([\d.]+[kmg]?)\s*\)", re.I,
)
# В узком окне хвост строки лимита переносится: "(resets 14:57)" на своей строке.
_CX_RESETS_TAIL_RE = re.compile(r"^\((resets [^)]*)\)$", re.I)
CODEX_STATUS_MARKER = "Model provider:"
# Маркеры перерисовки экрана для ожидания вывода команд в TUI.
CLAUDE_CONTEXT_MARKER = "Context Usage"
CLAUDE_USAGE_MARKER = "% used"


def _strip_box(line: str) -> str:
    """Убирает рамку TUI-бокса Codex и внешние отбивки."""
    return line.strip().strip("│┃|").strip()


def last_codex_status_block(output: str) -> str:
    """Последний бокс `/status` в снимке pane.

    Codex печатает бокс в историю, поэтому предыдущие вызовы остаются на экране
    и без обрезки лимиты удваиваются.
    """
    lines = output.splitlines()
    anchors = [i for i, line in enumerate(lines) if CODEX_STATUS_MARKER in line]
    if not anchors:
        return output
    anchor = anchors[-1]
    # В узком терминале верхняя рамка нового бокса уезжает за кадр, поэтому
    # подъём вверх останавливаем и на нижней рамке предыдущего бокса.
    start = anchor
    while start > 0 and "╰" not in lines[start - 1]:
        start -= 1
        if "╭" in lines[start]:
            break
    end = anchor
    while end < len(lines) - 1 and "╰" not in lines[end]:
        end += 1
    return "\n".join(lines[start:end + 1])


def parse_codex_status(output: str) -> SessionStats:
    """Читает блок `/status`: модель, план, session id, контекст и лимиты."""
    stats = SessionStats(agent="codex")
    group = ""

    for raw in last_codex_status_block(output).splitlines():
        line = _strip_box(raw)
        if not line:
            continue

        tail = _CX_RESETS_TAIL_RE.match(line)
        if tail and stats.limits and not stats.limits[-1].resets:
            last = stats.limits.pop()
            stats.limits.append(LimitWindow(
                name=last.name,
                used_percent=last.used_percent,
                resets=tail.group(1)[6:].strip(),
                group=last.group,
            ))
            continue

        if line.lower().startswith("context window:"):
            context = _CX_CONTEXT_RE.search(line)
            if context:
                stats.context = ContextUsage(
                    used_tokens=parse_token_count(context.group(2)),
                    total_tokens=parse_token_count(context.group(3)),
                    percent=round(100.0 - float(context.group(1)), 1),
                )
            continue

        limit = _CX_LIMIT_RE.match(line)
        if limit:
            percent = float(limit.group(2))
            if limit.group(3).lower() == "left":
                percent = round(100.0 - percent, 1)
            resets = (limit.group(4) or "").strip()
            if resets.lower().startswith("resets"):
                resets = resets[6:].strip()
            stats.limits.append(LimitWindow(
                name=humanize_limit_name(limit.group(1)),
                used_percent=percent,
                resets=resets,
                group=group,
            ))
            continue

        group_match = _CX_GROUP_RE.match(line)
        if group_match:
            group = group_match.group(1).strip()
            continue

        field_match = _CX_FIELD_RE.match(line)
        if not field_match:
            continue
        key, value = field_match.group(1).strip().lower(), field_match.group(2).strip()
        if key == "session":
            stats.session_id = value
        elif key == "model":
            model = re.match(r"([\w.:/-]+)", value)
            if model:
                stats.model = model.group(1)
            effort = re.search(r"reasoning\s+(\w+)", value)
            if effort:
                stats.effort = effort.group(1)
        elif key == "account":
            plan = re.search(r"\(([^)]+)\)\s*$", value)
            if plan:
                stats.plan = plan.group(1)
    return stats


# ---------------------------------------------------------------------------
# Codex CLI: rollout-журнал
# ---------------------------------------------------------------------------

def find_codex_rollout(session_id: str, codex_home: str = CODEX_HOME) -> str:
    """Путь к rollout-журналу сессии Codex или пустая строка."""
    if not session_id or not _SESSION_ID_RE.match(session_id):
        return ""
    pattern = os.path.join(
        codex_home, "sessions", "*", "*", "*", f"rollout-*-{session_id}.jsonl",
    )
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else ""


def _tail_lines(path: str, chunk: int = 512 * 1024) -> list[str]:
    """Последние строки файла без чтения всего журнала целиком."""
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        if size > chunk:
            handle.seek(size - chunk)
            handle.readline()  # отбрасываем обрезанную строку
        payload = handle.read()
    return payload.decode("utf-8", "replace").splitlines()


def read_codex_token_count(path: str) -> dict | None:
    """Последнее событие token_count из rollout-журнала."""
    if not path or not os.path.exists(path):
        return None
    for attempt in (512 * 1024, None):
        lines = _tail_lines(path) if attempt else open(
            path, encoding="utf-8", errors="replace",
        ).read().splitlines()
        for line in reversed(lines):
            if '"token_count"' not in line:
                continue
            try:
                payload = json.loads(line).get("payload", {})
            except (json.JSONDecodeError, AttributeError):
                continue
            if payload.get("type") == "token_count":
                return payload
        if attempt is None:
            break
    return None


def codex_context_from_payload(payload: dict | None) -> ContextUsage | None:
    """Контекст последнего запроса Codex: токены против окна модели."""
    if not payload:
        return None
    info = payload.get("info") or {}
    window = info.get("model_context_window") or 0
    last = (info.get("last_token_usage") or {}).get("total_tokens") or 0
    if not window or not last:
        return None
    return ContextUsage(
        used_tokens=last,
        total_tokens=window,
        percent=round(last / window * 100, 1),
        approximate=True,
    )


def codex_limits_from_payload(payload: dict | None) -> list[LimitWindow]:
    """Фолбэк по лимитам, если `/status` не открылся: снимок из журнала."""
    if not payload:
        return []
    rate = payload.get("rate_limits") or {}
    limits = []
    for key in ("primary", "secondary"):
        window = rate.get(key)
        if not isinstance(window, dict) or window.get("used_percent") is None:
            continue
        limits.append(LimitWindow(
            name=_window_label(window.get("window_minutes")),
            used_percent=round(float(window["used_percent"]), 1),
        ))
    return limits


def _window_label(minutes) -> str:
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return "Лимит"
    if minutes % 10080 == 0:
        return "Неделя" if minutes == 10080 else f"{minutes // 10080} нед."
    if minutes % 1440 == 0:
        return f"{minutes // 1440} сут."
    if minutes % 60 == 0:
        return f"{minutes // 60} ч."
    return f"{minutes} мин."


# ---------------------------------------------------------------------------
# Claude Code: реестр сессий и транскрипт
# ---------------------------------------------------------------------------

def find_claude_session(pane_id: str, sessions_dir: str = CLAUDE_SESSIONS_DIR) -> dict | None:
    """Запись ~/.claude/sessions/<pid>.json, принадлежащая этому tmux pane."""
    if not pane_id:
        return None
    best = None
    for path in glob.glob(os.path.join(sessions_dir, "*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        tmux = record.get("tmux") or ""
        if not isinstance(tmux, str) or not tmux.endswith(f".{pane_id}"):
            continue
        if best is None or record.get("updatedAt", 0) > best.get("updatedAt", 0):
            best = record
    return best


def claude_transcript_path(record: dict, projects_dir: str = CLAUDE_PROJECTS_DIR) -> str:
    """Путь к транскрипту сессии по её cwd и id (кодировка как у Claude Code)."""
    session_id, cwd = record.get("sessionId"), record.get("cwd")
    if not session_id or not cwd or not _SESSION_ID_RE.match(session_id):
        return ""
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    return os.path.join(projects_dir, encoded, f"{session_id}.jsonl")


# "Opus 5 (1M context) with xhigh effort · Claude Max" — единственное место,
# где CLI печатает размер окна; в транскрипте его нет.
_CC_WINDOW_RE = re.compile(r"\(\s*([\d.]+)\s*([kKmM])\s+context\s*\)")


def claude_context_window(pane_output: str = "", model_id: str = "") -> int:
    """Размер окна контекста Claude или 0, если его нельзя установить точно.

    Угадывать нельзя: у одной и той же модели бывает 200k и 1M, а ошибка
    превращает 14% занятого контекста в 72%.
    """
    match = _CC_WINDOW_RE.search(pane_output or "")
    if match:
        return parse_token_count(match.group(1) + match.group(2))
    if "[1m]" in (model_id or "").lower():
        return CLAUDE_LONG_WINDOW
    return 0


def read_claude_transcript_context(path: str, window: int = 0) -> ContextUsage | None:
    """Оценка контекста по последнему ответу модели в транскрипте.

    Используется, когда `/context` недоступен (например, сессия занята).
    `window` — размер окна от `claude_context_window`; при 0 процент не считаем.
    """
    if not path or not os.path.exists(path):
        return None
    usage, model = None, ""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    message = (json.loads(line).get("message") or {})
                except (json.JSONDecodeError, AttributeError):
                    continue
                if isinstance(message, dict) and isinstance(message.get("usage"), dict):
                    usage, model = message["usage"], message.get("model") or model
    except OSError:
        return None
    if not usage:
        return None

    used = sum(int(usage.get(key) or 0) for key in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
    ))
    if not used:
        return None
    total = window or claude_context_window(model_id=model)
    return ContextUsage(
        used_tokens=used,
        total_tokens=total,
        percent=round(used / total * 100, 1) if total else 0.0,
        approximate=True,
    )


# ---------------------------------------------------------------------------
# Сообщение для Telegram
# ---------------------------------------------------------------------------

_AGENT_TITLE = {"claude": "Claude Code", "codex": "Codex"}


def render_stats(stats: SessionStats) -> str:
    """Markdown-сообщение для Telegram: заголовок, контекст, лимиты."""
    head = [_AGENT_TITLE.get(stats.agent, stats.agent)]
    head += [part for part in (stats.model, stats.effort, stats.plan) if part]
    lines = ["📊 *" + " · ".join(head) + "*"]

    if stats.context:
        context = stats.context
        mark = "≈" if context.approximate else ""
        lines.append("")
        if context.total_tokens:
            lines.append(
                f"🧠 Контекст {mark}{context.percent:g}% — "
                f"{format_tokens(context.used_tokens)} из {format_tokens(context.total_tokens)}"
            )
            lines.append(f"`{progress_bar(context.percent)}`")
        else:
            lines.append(
                f"🧠 Контекст {mark}{format_tokens(context.used_tokens)} токенов "
                "(размер окна не определён)"
            )
        top = [f"{name} {format_tokens(value)}" for name, value in context.breakdown if value]
        if top:
            lines.append("• " + " · ".join(top[:5]))
    else:
        lines.append("")
        lines.append("🧠 Контекст: нет данных")

    lines.append("")
    if stats.limits:
        lines.append("📈 *Лимиты*")
        group = ""
        for limit in stats.limits:
            if limit.group and limit.group != group:
                lines.append(f"_{limit.group}_")
            group = limit.group
            resets = f" · сброс {limit.resets}" if limit.resets else ""
            lines.append(
                f"`{progress_bar(limit.used_percent)}` {limit.used_percent:g}% — "
                f"{limit.name}{resets}"
            )
    else:
        lines.append("📈 Лимиты: нет данных")

    for note in stats.notes:
        lines.append("")
        lines.append(f"⚠️ {note}")
    return "\n".join(lines)
