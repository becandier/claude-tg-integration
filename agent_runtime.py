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


@dataclass(frozen=True)
class PickerOption:
    value: str
    label: str
    selected: bool = False
    current: bool = False


_SAFE_SESSION_RE = re.compile(r"^(?:claude|codex)-[a-zA-Z0-9_-]+$")
_SAFE_MODEL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$")
_CODEX_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})


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
        model = model_match.group(1)
        if not model.startswith(("gpt-", "o", "codex-")):
            continue
        options.append(PickerOption(
            value=model,
            label=model,
            selected=selected,
            current="(current)" in payload,
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
    if spec.effort and spec.effort not in _CODEX_EFFORTS:
        raise ValueError("invalid effort")

    argv = [
        "tmux", "new-session", "-d", "-s", spec.session_name,
        "-c", spec.project_dir,
    ]
    if spec.agent is AgentKind.CLAUDE:
        return argv + ["claude", "--dangerously-skip-permissions"]

    codex_argv = [
        "codex", "--dangerously-bypass-approvals-and-sandbox",
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
    return argv + codex_argv
