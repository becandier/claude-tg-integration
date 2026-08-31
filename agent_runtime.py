#!/usr/bin/env python3
"""Agent-specific behavior for Telegram-managed tmux sessions."""

from dataclasses import dataclass
from enum import Enum
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


_SAFE_SESSION_RE = re.compile(r"^(?:claude|codex)-[a-zA-Z0-9_-]+$")
_SAFE_MODEL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$")
_CODEX_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})


def parse_newstart_args(text: str) -> tuple[AgentKind, str | None]:
    parts = text.strip().split()
    args = parts[1:]
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
        "--no-alt-screen",
    ]
    if spec.model:
        codex_argv += ["-m", spec.model]
    if spec.effort:
        codex_argv += ["-c", f'model_reasoning_effort="{spec.effort}"']
    return argv + codex_argv
