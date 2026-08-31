# Codex CLI Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add Codex CLI sessions to the Telegram-to-tmux bridge while retaining Claude Code as the default path.

**Architecture:** Isolate agent-dependent launch and settings behavior in a Python runtime module. Keep Telegram updates, forum topics, media, voice, and tmux routing in the watcher; persist the agent kind for every pane.

**Tech Stack:** Python standard library, unittest, tmux, Claude Code CLI, Codex CLI, Telegram Bot API.

## Global Constraints

- `/newstart [project]` launches Claude Code with its current maximum access.
- `/newstart codex [project]` launches Codex with its documented maximum-access flag.
- Legacy session records without an agent kind default to `claude`.
- No Telegram-controlled value is interpolated into a shell command.
- Claude approval handling stays Claude-only; Codex maximum-access sessions have no Telegram approval buttons.

### Task 1: Agent runtime and safe launch construction

**Files:** create `agent_runtime.py`; create `tests/test_agent_runtime.py`.

**Produces:** `AgentKind`, `LaunchSpec`, `parse_newstart_args(text)`, `build_launch_argv(spec)`.

- [ ] Write failing tests: `/newstart mobile` selects Claude; `/newstart codex mobile` selects Codex; the Codex argument vector includes the documented full-access flag, optional model, and reasoning-effort config.
- [ ] Run `python3 -m unittest tests.test_agent_runtime -v`; expect import failure.
- [ ] Implement enum-backed parsing and `subprocess.run` argument vectors. Claude launches as `claude --dangerously-skip-permissions`; Codex launches with `--dangerously-bypass-approvals-and-sandbox`. Validate agent, model, and effort before building arguments.
- [ ] Re-run the test; expect pass.
- [ ] Commit as `feat: add dual-agent runtime`.

### Task 2: Persist agent kind and migrate legacy data

**Files:** modify `tg_sessions.py`; create `tests/test_tg_sessions.py`.

**Produces:** `save_agent(pane_id, agent)` and `get_agent(pane_id)`.

- [ ] Write failing tests proving a legacy pane defaults to Claude and `cleanup_pane` removes a saved Codex kind.
- [ ] Run `python3 -m unittest tests.test_tg_sessions -v`; expect missing API failure.
- [ ] Add `pane_to_agent` initialization, `save_agent`, a Claude default in `get_agent`, and cleanup. Tests redirect `SESSIONS_FILE` to a temporary location.
- [ ] Run both test modules; expect pass.
- [ ] Commit as `feat: persist session agent kind`.

### Task 3: Route session lifecycle commands by agent

**Files:** modify `tg_reply_watcher.py`; modify `tests/test_agent_runtime.py`.

**Consumes:** parser, launch spec, launch vector, `save_agent`, and `get_agent`.

- [ ] Write failing tests ensuring Claude session names start with `claude-`, Codex names start with `codex-`, and existing project-picker actions retain the Claude default.
- [ ] Run focused runtime tests; expect failure.
- [ ] Refactor `handle_newstart`: only `codex` as the first argument selects Codex; all existing invocations remain Claude. Launch via the runtime vector, persist the agent after obtaining a pane, show agent labels in topics and `/sessions`, and allow `/close` to cover both prefixes. Run trust-folder confirmation only for Claude.
- [ ] Run `python3 -m unittest discover -s tests -v` plus `python3 -m py_compile tg_reply_watcher.py tg_sessions.py agent_runtime.py`; expect pass and no compiler output.
- [ ] Commit as `feat: launch Codex sessions from Telegram`.

### Task 4: Route model and effort safely

**Files:** modify `agent_runtime.py`; modify `tg_reply_watcher.py`; modify `tests/test_agent_runtime.py`.

**Produces:** agent-aware model and effort results with an explicit `APPLIED` or `RESTART_REQUIRED` status.

- [ ] Write failing tests showing Claude sends native slash commands while Codex settings return restart-required status.
- [ ] Run focused runtime tests; expect failure.
- [ ] Preserve Claude picker behavior. For Codex, validate and persist values but state that the interactive session must restart for launch-time configuration; never represent this as a live CLI change. Resolve agent before callbacks and never invoke Claude permission detection for Codex.
- [ ] Run the full tests and `py_compile`; expect pass.
- [ ] Commit as `feat: route model and effort by agent`.

### Task 5: Document and smoke-test both paths

**Files:** modify `README.md`.

- [ ] Document default Claude behavior, `/newstart codex [project]`, maximum access for both, no Codex approval buttons, and restart-required Codex setting changes.
- [ ] Run the full automated suite and syntax validation; expect pass.
- [ ] Perform Telegram smoke tests for one Claude and one Codex session: topic creation, text, media, `/sessions`, `/model`, `/effort`, and `/close`. Verify default `/newstart` remains Claude.
- [ ] Commit documentation as `docs: describe Codex Telegram sessions`.
