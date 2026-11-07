import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import tg_reply_watcher as watcher


class ActivityMonitoringTests(unittest.TestCase):
    def setUp(self):
        # Любая запись в хранилище из этих тестов не должна попасть в боевой /tmp.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target, name in ((watcher.tg_sessions, "SESSIONS_FILE"), (watcher.tg_resume, "RESUME_FILE")):
            patcher = patch.object(target, name, os.path.join(tmp.name, name.lower()))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.original_activity = watcher._pane_activity
        self.original_last_check = watcher._last_pane_check
        watcher._pane_activity = {
            "%1": {
                "sent_at": 1.0,
                "last_snapshot": "old",
                "last_change": 1.0,
                "notified_working": False,
                "notified_stuck": False,
            }
        }
        watcher._last_pane_check = 0.0

    def tearDown(self):
        watcher._pane_activity = self.original_activity
        watcher._last_pane_check = self.original_last_check

    @patch.object(watcher.tg_resume, "refresh")
    @patch.object(watcher, "live_tmux_panes", return_value={"%1": "claude-1"})
    @patch.object(watcher.os.path, "exists", return_value=False)
    @patch.object(watcher, "_capture_pane_content", return_value="new")
    @patch.object(watcher, "pane_exists", return_value=True)
    @patch.object(watcher.tg_sessions, "_load", return_value={
        "pane_to_topic": {"%1": 42},
    })
    @patch.object(watcher.time, "time", return_value=100.0)
    def test_updates_snapshot_when_pane_output_changes(
        self,
        _time,
        _load,
        _pane_exists,
        _capture,
        _path_exists,
        _live,
        _refresh,
    ):
        watcher.check_dead_panes()

        self.assertEqual(watcher._pane_activity["%1"]["last_snapshot"], "new")
        self.assertEqual(watcher._pane_activity["%1"]["last_change"], 100.0)


class CodexDirectoryTrustTests(unittest.TestCase):
    @patch.object(watcher.time, "sleep")
    @patch.object(watcher.subprocess, "run")
    def test_confirms_only_exact_codex_directory_trust_prompt(self, run, _sleep):
        run.side_effect = [
            Mock(
                returncode=0,
                stdout=(
                    "Do you trust the contents of this directory?\n"
                    "› 1. Yes, continue\n"
                    "  2. No, quit\n"
                ),
            ),
            Mock(returncode=0, stdout=""),
        ]

        self.assertTrue(watcher.wait_and_trust_codex_folder("%1", timeout=1))

        self.assertEqual(
            run.call_args_list[1].args[0],
            ["tmux", "send-keys", "-t", "%1", "Enter"],
        )


class ClaudeDirectoryTrustTests(unittest.TestCase):
    def test_confirms_when_yes_is_already_selected(self):
        content = (
            "Accessing workspace:\n\n"
            "❯ Yes, I trust this folder\n"
            "  No, exit\n\n"
            "Enter to confirm · Esc to cancel\n"
        )

        self.assertEqual(
            watcher.claude_trust_navigation_keys(content),
            ("Enter",),
        )

    def test_does_not_accept_unrelated_confirmation(self):
        content = "Delete all files?\n❯ Yes\n  No\nEnter to confirm"

        self.assertIsNone(watcher.claude_trust_navigation_keys(content))

    @patch.object(watcher.time, "sleep")
    @patch.object(watcher.subprocess, "run")
    def test_moves_from_no_to_yes_before_confirming(self, run, _sleep):
        run.side_effect = [
            Mock(
                returncode=0,
                stdout=(
                    "Accessing workspace:\n\n"
                    "❯ No, exit\n"
                    "  Yes, I trust this folder\n\n"
                    "Enter to confirm · Esc to cancel\n"
                ),
            ),
            Mock(returncode=0, stdout=""),
            Mock(returncode=0, stdout=""),
        ]

        self.assertTrue(watcher.wait_and_trust_folder("%1", timeout=1))

        self.assertEqual(
            run.call_args_list[1].args[0],
            ["tmux", "send-keys", "-t", "%1", "Down"],
        )
        self.assertEqual(
            run.call_args_list[2].args[0],
            ["tmux", "send-keys", "-t", "%1", "Enter"],
        )


class BotCommandParsingTests(unittest.TestCase):
    def test_video_only_message_has_no_command(self):
        command_text, command = watcher.parse_bot_command("")

        self.assertEqual(command_text, "")
        self.assertEqual(command, "")

    def test_group_command_strips_bot_username(self):
        command_text, command = watcher.parse_bot_command(
            "/newcodex@my_bot project"
        )

        self.assertEqual(command_text, "/newcodex project")
        self.assertEqual(command, "/newcodex")


class CodexInputTests(unittest.TestCase):
    def test_codex_launch_relies_on_native_stop_hook_only(self):
        spec = watcher.make_launch_spec(
            watcher.AgentKind.CODEX,
            "codex-123",
            "/tmp/project",
        )

        self.assertEqual(spec.notify_command, ())

    @patch.object(watcher.time, "time_ns", return_value=123)
    @patch.object(watcher.os, "getpid", return_value=456)
    @patch.object(watcher, "pane_exists", return_value=True)
    @patch.object(watcher.subprocess, "run")
    def test_uses_bracketed_paste_before_enter(
        self,
        run,
        _pane_exists,
        _getpid,
        _time_ns,
    ):
        run.return_value = Mock(returncode=0)

        self.assertTrue(watcher.tmux_send_codex("%1", "line one\nline two"))

        self.assertEqual(
            run.call_args_list[0].args[0],
            ["tmux", "load-buffer", "-b", "tg-codex-456-123", "-"],
        )
        self.assertEqual(run.call_args_list[0].kwargs["input"], "line one\nline two")
        self.assertEqual(
            run.call_args_list[1].args[0],
            [
                "tmux", "paste-buffer", "-p", "-r", "-d",
                "-b", "tg-codex-456-123", "-t", "%1",
            ],
        )
        self.assertEqual(
            run.call_args_list[2].args[0],
            ["tmux", "send-keys", "-t", "%1", "Enter"],
        )


class ClaudeInputTests(unittest.TestCase):
    @patch.object(watcher.time, "sleep")
    @patch.object(watcher.time, "time_ns", return_value=123)
    @patch.object(watcher.os, "getpid", return_value=456)
    @patch.object(watcher, "pane_exists", return_value=True)
    @patch.object(watcher.subprocess, "run")
    def test_long_message_is_pasted_whole_before_enter(
        self,
        run,
        _pane_exists,
        _getpid,
        _time_ns,
        sleep,
    ):
        run.return_value = Mock(returncode=0)
        text = "[TG | ~/.claude/tg-integration] " + "\n".join(
            f"{n}. пункт списка — 3,9 ГБ" for n in range(1, 55)
        )

        self.assertTrue(watcher.tmux_send("%1", text))

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands, [
            ["tmux", "load-buffer", "-b", "tg-claude-456-123", "-"],
            [
                "tmux", "paste-buffer", "-p", "-r", "-d",
                "-b", "tg-claude-456-123", "-t", "%1",
            ],
            ["tmux", "send-keys", "-t", "%1", "Enter"],
        ])
        self.assertEqual(run.call_args_list[0].kwargs["input"], text)
        sleep.assert_called_once_with(0.5)

    @patch.object(watcher.time, "sleep")
    @patch.object(watcher, "pane_exists", return_value=True)
    @patch.object(watcher.subprocess, "run")
    def test_failed_paste_is_not_submitted(self, run, _pane_exists, _sleep):
        run.side_effect = [
            Mock(returncode=0),
            Mock(returncode=1),
            Mock(returncode=0),
        ]

        self.assertFalse(watcher.tmux_send("%1", "строка один\nстрока два"))

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[2][:2], ["tmux", "delete-buffer"])
        self.assertNotIn(["tmux", "send-keys", "-t", "%1", "Enter"], commands)

    @patch.object(watcher.time, "sleep")
    @patch.object(watcher, "pane_exists", return_value=True)
    @patch.object(watcher.subprocess, "run")
    def test_short_single_line_is_typed(self, run, _pane_exists, sleep):
        run.return_value = Mock(returncode=0)
        text = "[TG | ~/.claude/tg-integration] да исправь"

        self.assertTrue(watcher.tmux_send("%1", text))

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands, [
            ["tmux", "send-keys", "-t", "%1", "-l", text],
            ["tmux", "send-keys", "-t", "%1", "Enter"],
        ])
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
