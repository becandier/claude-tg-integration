import unittest
from unittest.mock import Mock, patch

import tg_reply_watcher as watcher


class ActivityMonitoringTests(unittest.TestCase):
    def setUp(self):
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


if __name__ == "__main__":
    unittest.main()
