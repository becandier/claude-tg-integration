#!/usr/bin/env python3
"""Agent-specific behavior for Telegram-managed tmux sessions."""

from dataclasses import dataclass
from enum import Enum
import json
import os
import re


class AgentKind(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"


@dataclass(frozen=True)
class LaunchSpec:
    agent: AgentKind
    session_name: str
    project_dir: str
    model: str = ""
    effort: str = ""
    notify_command: tuple[str, ...] = ()
    # Id сессии CLI для продолжения истории (восстановление после перезагрузки).
    resume_session_id: str = ""


@dataclass(frozen=True)
class PickerOption:
    value: str
    label: str
    selected: bool = False
    current: bool = False
    description: str = ""


_SAFE_SESSION_RE = re.compile(r"^(?:claude|codex)-[a-zA-Z0-9_-]+$")
_SAFE_MODEL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$")
_CODEX_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
# Значения флага `claude --effort` (claude --help); ultracode флагом не задаётся.
_CLAUDE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_RESUME_ID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,63}$")


def parse_newstart_args(text: str) -> tuple[AgentKind, str | None]:
    parts = text.strip().split()
    command = parts[0].split("@", 1)[0].lower() if parts else ""
    args = parts[1:]
    if command == "/newcodex":
        project = " ".join(args).strip() or None
        return AgentKind.CODEX, project
    if command == "/newclaude":
        project = " ".join(args).strip() or None
        return AgentKind.CLAUDE, project
    if args and args[0].lower() == AgentKind.CODEX.value:
        project = " ".join(args[1:]).strip() or None
        return AgentKind.CODEX, project
    project = " ".join(args).strip() or None
    return AgentKind.CLAUDE, project


def make_session_name(agent: AgentKind, timestamp: int) -> str:
    return f"{agent.value}-{timestamp}"


def is_managed_session(session_name: str) -> bool:
    return session_name.startswith(("claude-", "codex-"))


def project_callback_data(agent: AgentKind, project: str) -> str:
    if agent is AgentKind.CODEX:
        return f"start:codex:{project}"
    return f"start:{project}"


def resolve_project_dir(project: str, projects_root: str, home_dir: str) -> str:
    if project == "__home__":
        return os.path.realpath(home_dir)
    if project == "__projects__":
        return os.path.realpath(projects_root)

    root = os.path.realpath(projects_root)
    candidate = os.path.realpath(os.path.join(root, project))
    if os.path.commonpath((root, candidate)) != root:
        raise ValueError("project path escapes projects root")
    return candidate


def _picker_line(line: str) -> tuple[str, bool] | None:
    body = line.lstrip()
    selected = body.startswith("›")
    if selected:
        body = body[1:].lstrip()
    match = re.match(r"\d+\.\s+(.+)$", body)
    if not match:
        return None
    return match.group(1), selected


def terminal_model(output: str, agent: AgentKind) -> str:
    """Read the live footer first; the Codex welcome header can be stale."""
    if agent is AgentKind.CODEX:
        for line in reversed(output.splitlines()):
            match = re.match(r"\s*((?:gpt-|codex-|o\d)[\w.:/-]+)\s+.*·", line, re.I)
            if match:
                return match.group(1).lower()
        match = re.search(r"model:\s+([\w.:/-]+)", output, re.I)
        return match.group(1).lower() if match else ""
    for line in output.splitlines():
        match = re.search(r"\b((?:Opus|Sonnet|Haiku|Fable)\s+[\d.]+(?:\s*\([^)]*\))?)(?:\s+with\s+\w+\s+effort)?\s*·", line)
        if match:
            return match.group(1)
    return ""


def terminal_effort(output: str, agent: AgentKind) -> str:
    if agent is AgentKind.CODEX:
        for line in reversed(output.splitlines()):
            match = re.match(r"\s*(?:gpt-|codex-|o\d)[\w.:/-]+\s+(\w+)\s*·", line, re.I)
            if match:
                return match.group(1).lower()
    else:
        match = re.search(r"\bwith\s+(\w+)\s+effort\s*·", output)
        if match:
            return match.group(1)
    return ""


def terminal_ready(output: str, agent: AgentKind, *, allow_working: bool = False) -> bool:
    """Require a composer; active Codex turns accept text but not slash commands."""
    markers = ("›", "»") if agent is AgentKind.CODEX else ("❯",)
    cursors = [line.strip() for line in output.splitlines() if line.lstrip().startswith(markers)]
    if not cursors or re.match(r"[›»❯]\s*\d+\.", cursors[-1]):
        return False
    tail = output[output.rfind(cursors[-1]):]
    recent = "\n".join([line for line in output.splitlines() if line.strip()][-6:])
    if re.search(r"esc to interrupt|Working \(|esc to cancel", recent, re.I) and not (
        allow_working and agent is AgentKind.CODEX
    ):
        return False
    if agent is AgentKind.CODEX:
        return bool(terminal_model(tail, agent))
    return True


def parse_codex_update_picker(output: str) -> list[PickerOption]:
    if not re.search(r"update available", output, re.I):
        return []
    options = []
    for line in output.splitlines():
        match = re.match(r"\s*(›\s*)?(\d+)\.\s+(.+)", line)
        if match:
            options.append(PickerOption(match.group(2), match.group(3).strip(), bool(match.group(1))))
    if not any(option.label.lower().startswith("update") for option in options):
        return []
    return options


def parse_codex_rate_limit_picker(output: str) -> list[PickerOption]:
    """Recognize the active model nudge, never a quota error or an old dialog."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines or lines[-1] != "enter select · esc back":
        return []
    headings = [i for i, line in enumerate(lines) if line == "Approaching rate limits"]
    if not headings:
        return []
    dialog = lines[headings[-1]:]
    if len(dialog) != 6 or not re.fullmatch(
        r"Switch to [\w./:-]+ for lower credit usage\?", dialog[1],
    ):
        return []
    options = []
    for number, line in enumerate(dialog[2:5], start=1):
        match = re.fullmatch(r"([›»])?\s*" + str(number) + r"\.\s+(.+)", line)
        if not match:
            return []
        label = re.split(r"\s{2,}", match.group(2), maxsplit=1)[0]
        options.append(PickerOption(str(number), label, bool(match.group(1))))
    if (
        options[0].label != dialog[1].removesuffix(" for lower credit usage?")
        or options[1].label != "Keep current model"
        or options[2].label != "Keep current model (never show again)"
        or sum(option.selected for option in options) != 1
    ):
        return []
    return options


def parse_codex_model_picker(output: str) -> list[PickerOption]:
    options = []
    for line in output.splitlines():
        parsed = _picker_line(line)
        if not parsed:
            continue
        payload, selected = parsed
        model_match = re.match(r"([a-zA-Z0-9][a-zA-Z0-9._:/-]*)", payload)
        if not model_match:
            continue
        model = model_match.group(1).lower()
        if not model.startswith(("gpt-", "o", "codex-")):
            continue
        options.append(PickerOption(
            value=model,
            label=model,
            selected=selected,
            current="(current)" in payload,
        ))
    return options


CLAUDE_MODEL_PICKER_MARKER = "Select model"
# Строка пикера Claude: "  ❯ 3. Fable ✔                Fable 5.1 · Most capable..."
_CLAUDE_MODEL_LINE_RE = re.compile(r"^\s*(?:❯\s*)?\d+\.\s+(.+?)\s{2,}(.+?)\s*$")


def parse_claude_model_picker(output: str) -> list[PickerOption]:
    """Пикер `/model` Claude Code: value — алиас CLI (первое слово строки),
    ✔ помечает модель сессии, ❯ — выделенную строку, description — колонка справа."""
    if CLAUDE_MODEL_PICKER_MARKER not in output:
        return []
    options = []
    for line in output.split(CLAUDE_MODEL_PICKER_MARKER, 1)[1].splitlines():
        match = _CLAUDE_MODEL_LINE_RE.match(line)
        if not match:
            continue
        label, description = match.group(1).strip(), match.group(2).strip()
        current = "✔" in label or "✔" in description
        label = label.replace("✔", "").strip()
        alias = label.split()[0].lower().strip(".")
        if not alias.isalpha():
            continue
        options.append(PickerOption(
            value=alias,
            label=label,
            selected=line.lstrip().startswith("❯"),
            current=current,
            description=description.replace("✔", "").strip(),
        ))
    return options


def parse_codex_effort_picker(output: str) -> list[PickerOption]:
    labels = {
        "low": "low",
        "medium": "medium",
        "high": "high",
        "extra high": "xhigh",
        "more reasoning…": "more",
        "more reasoning...": "more",
        "max": "max",
        "ultra": "ultra",
    }
    options = []
    for line in output.splitlines():
        parsed = _picker_line(line)
        if not parsed:
            continue
        payload, selected = parsed
        title = re.split(r"\s{2,}", payload, maxsplit=1)[0]
        clean_title = re.sub(r"\s*\([^)]*\)", "", title).strip()
        value = labels.get(clean_title.lower())
        if not value:
            continue
        options.append(PickerOption(
            value=value,
            label=clean_title,
            selected=selected,
            current="(current)" in payload,
        ))
    return options


def picker_navigation_keys(
    options: list[PickerOption],
    target: str,
) -> tuple[str, ...]:
    selected_index = next(
        (index for index, option in enumerate(options) if option.selected),
        next((index for index, option in enumerate(options) if option.current), None),
    )
    target_index = next(
        (index for index, option in enumerate(options) if option.value == target),
        None,
    )
    if selected_index is None or target_index is None:
        raise ValueError("picker target is unavailable")
    key = "Down" if target_index > selected_index else "Up"
    return tuple(key for _ in range(abs(target_index - selected_index)))


def build_launch_argv(spec: LaunchSpec) -> list[str]:
    if not _SAFE_SESSION_RE.fullmatch(spec.session_name):
        raise ValueError("invalid session name")
    if not spec.project_dir:
        raise ValueError("project directory is required")
    if spec.model and not _SAFE_MODEL_RE.fullmatch(spec.model):
        raise ValueError("invalid model")
    efforts = _CLAUDE_EFFORTS if spec.agent is AgentKind.CLAUDE else _CODEX_EFFORTS
    if spec.effort and spec.effort not in efforts:
        raise ValueError("invalid effort")
    if spec.resume_session_id and not _RESUME_ID_RE.fullmatch(spec.resume_session_id):
        raise ValueError("invalid resume session id")

    argv = [
        "tmux", "new-session", "-d", "-s", spec.session_name,
        "-c", spec.project_dir,
    ]
    if spec.agent is AgentKind.CLAUDE:
        claude_argv = ["claude", "--dangerously-skip-permissions"]
        if spec.resume_session_id:
            claude_argv += ["--resume", spec.resume_session_id]
        if spec.model:
            claude_argv += ["--model", spec.model]
        if spec.effort:
            claude_argv += ["--effort", spec.effort]
        return argv + claude_argv

    # `codex resume [OPTIONS] [SESSION_ID]`: флаги после подкоманды, id последним.
    codex_argv = ["codex"] + (["resume"] if spec.resume_session_id else []) + [
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--no-alt-screen",
    ]
    if spec.model:
        codex_argv += ["-m", spec.model]
    if spec.effort:
        codex_argv += ["-c", f'model_reasoning_effort="{spec.effort}"']
    if spec.notify_command:
        if any(not value or "\x00" in value for value in spec.notify_command):
            raise ValueError("invalid notify command")
        notify_toml = json.dumps(
            list(spec.notify_command),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        codex_argv += ["-c", f"notify={notify_toml}"]
    if spec.resume_session_id:
        codex_argv.append(spec.resume_session_id)
    return argv + codex_argv


def resume_cli_settings(agent: AgentKind, model: str, effort: str) -> tuple[str, str, str]:
    """Сохранённые модель/effort → (model, effort, late_effort): первые два идут
    флагами запуска, late_effort флагом не задаётся и применяется командой
    после готовности CLI (Claude `ultracode`, Codex `max`/`ultra`).

    Claude хранит то алиас (`opus`), то подпись (`Fable 5.1`, `Opus 5 (1M context)`):
    подпись сводится к алиасу семейства, версия и размер окна при этом теряются.
    """
    model = (model or "").strip()
    if model and not _SAFE_MODEL_RE.fullmatch(model):
        family = model.split()[0].lower() if agent is AgentKind.CLAUDE else ""
        model = family if family.isalpha() else ""
    efforts = _CLAUDE_EFFORTS if agent is AgentKind.CLAUDE else _CODEX_EFFORTS
    effort = (effort or "").strip().lower()
    if effort in efforts:
        return model, effort, ""
    return model, "", effort
