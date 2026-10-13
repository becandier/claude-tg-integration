import os
import tempfile
import unittest
from unittest.mock import patch

import tg_reply_watcher as watcher


# Codex CLI 0.157.0, captured from the pane with the stuck voice message.
RATE_LIMIT_PROMPT = """  Approaching rate limits
  Switch to gpt-6-luna for lower credit usage?

› 1. Switch to gpt-6-luna                   Fast and affordable model for easier tasks.
  2. Keep current model
  3. Keep current model (never show again)  Hide future rate limit reminders about switching models

  enter select · esc back
"""
READY = "› Ask Codex to do anything\n  GPT-6-Sol ultra · ~/projects"


class RateLimitRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = patch.object(watcher.tg_sessions, "SESSIONS_FILE", os.path.join(tmp.name, "sessions.json"))
        config.start()
        self.addCleanup(config.stop)
        watcher.tg_sessions.save_agent("%1", "codex")
        watcher.tg_sessions.save_topic("%1", 42)
        watcher.tg_sessions.save_model("%1", "gpt-6-sol")
        watcher.tg_sessions.save_effort("%1", "ultra")
        for name, value in (
            ("pane_exists", True), ("tg_send", {"ok": True}), ("tg_react", {"ok": True}),
            ("mark_pane_waiting", None), ("_codex_delivery_context", None),
        ):
            mock = patch.object(watcher, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def test_pending_transcript_keeps_model_then_delivers_once_when_composer_returns(self):
        transcript = "[TG | ~/.claude/tg-integration] [tg-message-id:7] Поменяй кнопку местами"
        with patch.object(watcher, "_capture_pane", return_value=RATE_LIMIT_PROMPT) as capture, \
                patch.object(watcher, "tmux_send_keys", return_value=True) as keys, \
                patch.object(watcher, "tmux_send_codex", return_value=True) as send:
            self.assertFalse(watcher.queue_if_blocked("%1", transcript, 7, 42))
            watcher.check_starting_sessions()
            self.assertEqual([call.args for call in keys.call_args_list], [("%1", "Down"), ("%1", "Enter")])
            send.assert_not_called()
            self.assertEqual(watcher.tg_sessions.pending_messages("%1"), [{"text": transcript, "message_id": 7}])
            capture.return_value = READY
            watcher.check_starting_sessions()
            watcher.check_starting_sessions()
            send.assert_called_once_with("%1", transcript)
        self.assertEqual(watcher.tg_sessions.pending_messages("%1"), [])
        self.assertEqual(watcher.tg_sessions.get_reply_to("%1"), 7)
        self.assertEqual(watcher.tg_sessions.get_model("%1"), "gpt-6-sol")
        self.assertEqual(watcher.tg_sessions.get_effort("%1"), "ultra")

    def test_navigation_follows_cursor_without_selecting_never_show_again(self):
        for selected, expected in ((2, [("%1", "Enter")]), (3, [("%1", "Up"), ("%1", "Enter")])):
            with self.subTest(selected=selected):
                output = RATE_LIMIT_PROMPT.replace("› 1.", "  1.").replace(f"  {selected}.", f"» {selected}.")
                watcher.tg_sessions.queue_message("%1", "transcript", 7)
                with patch.object(watcher, "_capture_pane", return_value=output), \
                        patch.object(watcher, "tmux_send_keys", return_value=True) as keys:
                    watcher.check_starting_sessions()
                self.assertEqual([call.args for call in keys.call_args_list], expected)

    def test_incomplete_or_unrelated_dialogs_remain_queued(self):
        screens = [
            RATE_LIMIT_PROMPT.replace("› 1.", "  1."),
            RATE_LIMIT_PROMPT.replace("  2. Keep current model\n", ""),
            RATE_LIMIT_PROMPT.replace("Approaching rate limits", "Usage limit reached"),
            RATE_LIMIT_PROMPT + "\nHooks need review\n› 1. Review hooks\n  2. Trust all\n  enter select · esc back",
            "Hooks need review\n› 1. Review hooks\n  2. Trust all",
        ]
        for output in screens:
            with self.subTest(output=output):
                watcher.tg_sessions.queue_message("%1", "transcript", 7)
                with patch.object(watcher, "_capture_pane", return_value=output), \
                        patch.object(watcher, "tmux_send_keys") as keys, \
                        patch.object(watcher, "tmux_send_codex") as send:
                    watcher.check_starting_sessions()
                keys.assert_not_called()
                send.assert_not_called()
                self.assertEqual(len(watcher.tg_sessions.pending_messages("%1")), 1)

    def test_old_dialog_in_scrollback_does_not_trigger_keys(self):
        watcher.tg_sessions.queue_message("%1", "transcript", 7)
        with patch.object(watcher, "_capture_pane", return_value=RATE_LIMIT_PROMPT + READY), \
                patch.object(watcher, "tmux_send_keys") as keys, \
                patch.object(watcher, "tmux_send_codex", return_value=True) as send:
            watcher.check_starting_sessions()
        keys.assert_not_called()
        send.assert_called_once_with("%1", "transcript")

    def test_failed_navigation_does_not_press_enter_or_drop_transcript(self):
        watcher.tg_sessions.queue_message("%1", "transcript", 7)
        with patch.object(watcher, "_capture_pane", return_value=RATE_LIMIT_PROMPT), \
                patch.object(watcher, "tmux_send_keys", return_value=False) as keys, \
                patch.object(watcher, "tmux_send_codex") as send:
            watcher.check_starting_sessions()
        keys.assert_called_once_with("%1", "Down")
        send.assert_not_called()
        self.assertEqual(len(watcher.tg_sessions.pending_messages("%1")), 1)

    def test_claude_does_not_auto_answer_codex_dialog(self):
        watcher.tg_sessions.save_agent("%1", "claude")
        watcher.tg_sessions.queue_message("%1", "transcript", 7)
        with patch.object(watcher, "_capture_pane", return_value=RATE_LIMIT_PROMPT), \
                patch.object(watcher, "tmux_send_keys") as keys:
            watcher.check_starting_sessions()
        keys.assert_not_called()


if __name__ == "__main__":
    unittest.main()
