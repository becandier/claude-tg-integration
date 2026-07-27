# Codex CLI adapter design

## Goal

Extend the Telegram-to-tmux bridge so one bot can run both Claude Code and
Codex CLI sessions. Existing Claude Code behavior remains the default and must
not regress.

## User-visible behavior

- `/newstart [project]` starts Claude Code, as it does today.
- `/newstart codex [project]` starts Codex CLI in a new forum topic.
- `/sessions`, `/close`, media upload, voice transcription, file sending,
  project navigation, activity notifications, and topic routing work for both
  agents.
- `/model` and `/effort` work within either session type. The selected values
  are stored with that session and applied through its adapter.
- Both agents launch with maximum local access. Claude keeps its existing
  bypassed-permissions invocation; Codex uses its documented bypass flag.
  Consequently, Codex does not expose Telegram approval buttons.

## Architecture

Introduce an explicit agent abstraction while keeping Telegram transport and
tmux mechanics shared.

- `AgentKind`: `claude` and `codex`.
- `SessionManager` persists the agent kind alongside pane ID, forum topic,
  project path, model, and reasoning effort.
- `ClaudeAdapter` owns Claude-specific launch arguments, terminal-state
  parsing, native `/model` and `/effort` interactions, and its existing hooks.
- `CodexAdapter` owns Codex launch arguments, model and reasoning-effort
  configuration, state detection, and session lifecycle.
- Command handlers resolve the session from the Telegram topic, then delegate
  agent-specific behavior to the adapter. Shared handlers do not branch on
  terminal text or CLI commands.

## Codex launch contract

Each Codex session starts in the selected project directory with:

- the selected model passed as a CLI model override when present;
- `model_reasoning_effort` supplied through a Codex configuration override;
- full access enabled by Codex's documented bypass-approvals-and-sandbox flag;
- a stable tmux session naming scheme that encodes the agent kind.

The adapter must quote project paths and user-controlled values safely before
they reach a shell. It must never interpolate untrusted Telegram text into a
shell command.

## Compatibility and migration

Existing saved Claude sessions without an agent-kind field are treated as
`claude`. New records always include the field. Existing topic-to-pane mappings
remain valid. Claude-specific hooks stay installed and are not repurposed for
Codex.

## Error handling

- If the requested CLI is unavailable, report the agent-specific failure in the
  target topic and do not create a partial session mapping.
- If a model or effort value is unsupported by an adapter, preserve the running
  session and return a concise explanation.
- A dead pane removes only its own mapping; it must not affect sessions of the
  other agent kind.
- Codex maximum-access mode has no approval prompt flow. Claude's existing
  permission detection remains isolated to `ClaudeAdapter`.

## Verification

Add automated tests for session serialization and legacy migration, command
routing, launch-command construction, safe argument handling, and adapter
selection. Run existing regression checks for Claude command handling. Perform
manual smoke tests for one Claude and one Codex session: creation, plain text,
media, model/effort changes, `/sessions`, and `/close`.

## Non-goals

- Replacing the Telegram transport or forum-topic model.
- Automating the Codex Desktop application.
- Changing Claude Code's current default behavior or access mode.
