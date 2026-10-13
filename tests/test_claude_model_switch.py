"""Смена модели Claude Code из Telegram: пикер `/model`, клавиша `s`, диалог «Switch model?»."""
import unittest
from unittest.mock import patch

import agent_runtime as runtime
import tg_reply_watcher as watcher
import tg_stats

PICKER = """\
❯ /model
   Select model
   Switch between Claude models. Your pick becomes the default for new sessions. For other/previous model names, specify with --model.

     1. Default (recommended)  Opus 5.5 · Best for everyday, complex tasks
     2. Opus                   Opus 5.5 · Best for everyday, complex tasks
   ❯ 3. Fable ✔                Fable 5.1 · Most capable for your hardest and longest-running tasks
     4. Sonnet                 Sonnet 5.5 · Efficient for routine tasks
     5. Haiku                  Haiku 5.5 · Fastest for quick answers

   ◉ xHigh effort ←/→ to adjust

   Use /fast to turn on Fast mode (Opus 5.5).

   Enter to set as default · s to use this session only · Esc to cancel
"""
DIALOG = """\
   Switch model?
   Your next response will be slower and use more tokens
   This conversation is cached for the current model. Switching to Opus 5.5 means the full history gets re-read on your next message.
   ❯ 1. Yes, switch to Opus 5.5
     2. No, go back
"""
READY = "❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
DONE = "❯ /model\n  ⎿  Set model to Opus 5.5 for this session only\n" + READY
KEPT = "❯ /model\n  ⎿  Kept model as Fable 5.1\n" + READY
DIRECT_DONE = KEPT + "\n❯ /model claude-opus-4-1\n  ⎿  Set model to Opus 4.1 and saved as your default for new sessions\n" + READY


def _frames(*screens):
    """Кадры экрана по порядку; последний повторяется."""
    pending = list(screens)

    def capture(_pane):
        if len(pending) > 1:
            return pending.pop(0)
        return pending[0]
    return capture


class ClaudePickerParseTests(unittest.TestCase):
    def test_parses_rows_with_current_and_selected(self):
        options = runtime.parse_claude_model_picker(PICKER)
        self.assertEqual([o.value for o in options], ["default", "opus", "fable", "sonnet", "haiku"])
        fable = options[2]
        self.assertTrue(fable.current)
        self.assertTrue(fable.selected)
        self.assertEqual(fable.label, "Fable")
        self.assertEqual(options[0].label, "Default (recommended)")
        self.assertEqual(options[1].description, "Opus 5.5 · Best for everyday, complex tasks")
        self.assertFalse(any(o.selected for o in options if o.value != "fable"))

    def test_without_picker_returns_nothing(self):
        self.assertEqual(runtime.parse_claude_model_picker(READY), [])

    def test_probe_models_lists_every_row_and_closes_picker(self):
        with patch.object(watcher, "pane_exists", return_value=True), \
                patch.object(watcher, "_type_command", return_value=True), \
                patch.object(watcher, "_capture_pane", return_value=PICKER), \
                patch.object(watcher.subprocess, "run") as run, \
                patch.object(watcher.time, "sleep"):
            models = watcher.probe_models("%9")
        self.assertEqual([m["alias"] for m in models], ["default", "opus", "fable", "sonnet", "haiku"])
        self.assertEqual([m["alias"] for m in models if m["current"]], ["fable"])
        self.assertEqual(models[2]["desc"], "Fable 5.1 · Most capable for your hardest and longest-running tasks")
        self.assertIn("Escape", run.call_args.args[0])


class ClaudeModelSwitchTests(unittest.TestCase):
    def _run(self, alias, *screens):
        typed, keys, literal = [], [], []
        with patch.object(watcher, "_capture_pane", side_effect=_frames(*screens)), \
                patch.object(watcher, "_type_command", side_effect=lambda _p, cmd: typed.append(cmd) or True), \
                patch.object(watcher, "tmux_send_keys", side_effect=lambda _p, *k: keys.append(k) or True), \
                patch.object(watcher, "_tmux_type", side_effect=lambda _p, text: literal.append(text) or True), \
                patch.object(watcher.time, "sleep"):
            result = watcher._set_claude_model("%9", alias)
        return result, typed, keys, literal

    def test_switch_moves_cursor_presses_s_and_confirms_cache_dialog(self):
        result, typed, keys, literal = self._run("opus", READY, PICKER, DIALOG, DONE)
        self.assertTrue(result)
        self.assertEqual(typed, ["/model"])          # не `/model opus`: тот сохраняет default
        self.assertEqual(literal, ["s"])
        self.assertEqual(keys, [("Up",), ("Enter",)])  # Fable(3) → Opus(2), затем «Yes, switch»

    def test_switch_without_dialog_needs_no_confirmation(self):
        result, _typed, keys, literal = self._run("haiku", READY, PICKER, DONE.replace("Opus 5.5", "Haiku 5.5"))
        self.assertTrue(result)
        self.assertEqual(literal, ["s"])
        self.assertEqual(keys, [("Down", "Down")])

    def test_dialog_with_cursor_on_no_is_moved_to_yes(self):
        dialog = DIALOG.replace("❯ 1.", "  1.").replace("  2.", "❯ 2.")
        result, _typed, keys, _literal = self._run("opus", READY, PICKER, dialog, DONE)
        self.assertTrue(result)
        self.assertEqual(keys, [("Up",), ("Up",), ("Enter",)])

    def test_unknown_name_closes_picker_and_uses_direct_command(self):
        result, typed, keys, literal = self._run("claude-opus-4-1", READY, PICKER, KEPT, DIRECT_DONE)
        self.assertTrue(result)
        self.assertEqual(typed, ["/model", "/model claude-opus-4-1"])
        self.assertEqual(keys, [("Escape",)])
        self.assertEqual(literal, [])

    def test_unconfirmed_switch_closes_picker_and_reports_failure(self):
        with patch.object(watcher, "CLAUDE_MODEL_SWITCH_TIMEOUT", 0):
            result, _typed, keys, literal = self._run("opus", READY, PICKER, PICKER)
        self.assertFalse(result)
        self.assertEqual(literal, ["s"])
        self.assertEqual(keys[-1], ("Escape",))

    def test_success_when_identical_old_line_scrolled_off_the_top(self):
        # До команды вверху экрана виден старый итог; после — он ушёл за кадр,
        # а снизу появился новый с тем же текстом: счётчик не вырос.
        before = DONE + "\n" + "обсуждение\n" * 3 + READY
        after = "обсуждение\n" * 3 + "❯ /model\n  ⎿  Set model to Opus 5.5 for this session only\n" + READY
        with patch.object(watcher, "CLAUDE_MODEL_SWITCH_GRACE", 0), \
                patch.object(watcher, "probe_models", return_value=[
                    {"alias": "opus", "label": "Opus", "desc": "", "current": True}]):
            result, *_ = self._run("opus", before, PICKER, after)
        self.assertTrue(result)

    def test_unchanged_screen_is_checked_through_the_picker_and_fails(self):
        before = DONE  # прежняя смена уже видна прямо над композером
        with patch.object(watcher, "CLAUDE_MODEL_SWITCH_GRACE", 0), \
                patch.object(watcher, "probe_models", return_value=[
                    {"alias": "fable", "label": "Fable", "desc": "", "current": True}]) as probe:
            result, *_ = self._run("opus", before, PICKER, before)
        self.assertFalse(result)
        probe.assert_called_once()

    def test_result_line_is_taken_from_above_the_composer_only(self):
        self.assertEqual(watcher._latest_claude_result(DONE), "⎿  Set model to Opus 5.5 for this session only")
        self.assertEqual(watcher._latest_claude_result(READY), "")
        history = "  ⎿  Set model to Opus 5.5 for this session only\n" + "строка\n" * 6 + READY
        self.assertEqual(watcher._latest_claude_result(history), "")
        self.assertEqual(watcher._latest_claude_result(PICKER), "")  # строки пикера — не композер

    def test_callback_keeps_saved_model_when_switch_fails(self):
        with patch.object(watcher, "pane_exists", return_value=True), \
                patch.object(watcher, "set_session_model", return_value=False), \
                patch.object(watcher.tg_sessions, "get_pane_by_topic", return_value="%1"), \
                patch.object(watcher.tg_sessions, "get_agent", return_value="claude"), \
                patch.object(watcher.tg_sessions, "save_model") as save, \
                patch.object(watcher, "tg_api") as api, \
                patch.object(watcher, "tg_edit") as edit:
            watcher.handle_callback({"id": "cb", "data": "model:opus", "message": {
                "message_thread_id": 42, "message_id": 5, "reply_markup": {"inline_keyboard": []},
            }})
        save.assert_not_called()
        edit.assert_not_called()
        self.assertTrue(api.call_args.args[1].get("show_alert"))


class SessionModelPrecedenceTests(unittest.TestCase):
    BANNER = "▝▜██████▀  Fable 5.1 with xhigh effort · Claude Max\n" + READY

    def test_saved_claude_choice_beats_stale_welcome_banner(self):
        with patch.object(watcher.tg_sessions, "get_model", return_value="opus"):
            self.assertEqual(watcher._session_model("%1", runtime.AgentKind.CLAUDE, self.BANNER), "opus")

    def test_banner_is_used_when_nothing_saved(self):
        with patch.object(watcher.tg_sessions, "get_model", return_value=""):
            self.assertEqual(watcher._session_model("%1", runtime.AgentKind.CLAUDE, self.BANNER), "Fable 5.1")

    def test_codex_prefers_live_footer(self):
        screen = "› Ask Codex to do anything\n gpt-5.6-sol medium · /tmp"
        with patch.object(watcher.tg_sessions, "get_model", return_value="gpt-old"):
            self.assertEqual(watcher._session_model("%1", runtime.AgentKind.CODEX, screen), "gpt-5.6-sol")

    def test_context_model_is_read_from_the_block_not_from_history_above(self):
        screen = (
            "▝▜██████▀  Fable 5.1 with xhigh effort · Claude Max\n"
            "❯ /model\n  ⎿  Set model to Opus 5.5 for this session only\n"
            "❯ /context\n  ⎿  Context Usage\n"
            "     ⛁ ⛁ ⛁ ⛶ ⛶   Opus 5.5\n"
            "     ⛶ ⛶ ⛶ ⛶ ⛶   claude-opus-5-5\n"
            "     ⛶ ⛶ ⛶ ⛶ ⛶   59.1k/1m tokens (6%)\n"
        )
        self.assertEqual(tg_stats.parse_claude_context_model(screen), "Opus 5.5")


if __name__ == "__main__":
    unittest.main()
