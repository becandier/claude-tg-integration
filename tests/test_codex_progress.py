import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import tg_codex_submit_hook as submit_hook
import tg_reply_watcher as watcher
import tg_sessions


SESSION_ID = "01a0d47c-32d7-74f1-be91-85aaa333a978"


class CodexProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_sessions_file = tg_sessions.SESSIONS_FILE
        tg_sessions.SESSIONS_FILE = os.path.join(self.temp.name, "sessions.json")
        self.rollout = os.path.join(self.temp.name, "rollout.jsonl")
        open(self.rollout, "w").close()
        tg_sessions.save_agent("%1", "codex")
        tg_sessions.save_topic("%1", 42)
        tg_sessions.save_reply_to("%1", 9)

    def tearDown(self):
        tg_sessions.SESSIONS_FILE = self.original_sessions_file
        self.temp.cleanup()

    def append(self, record):
        with open(self.rollout, "a") as handle:
            handle.write(json.dumps(record) + "\n")

    def accept(self, message_id=9, turn_id="turn-1"):
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": SESSION_ID,
            "turn_id": turn_id,
            "prompt": f"{submit_hook.PROMPT_MARKER} [tg-message-id:{message_id}] hello",
        }
        with patch.object(submit_hook.tg_stats, "find_codex_rollout", return_value=self.rollout):
            return submit_hook.mark_prompt_accepted(payload, pane_id="%1")

    def test_submit_hook_acknowledges_codex_acceptance_and_tracks_turn(self):
        self.assertTrue(self.accept())
        state = tg_sessions._load()["pane_to_codex_progress"]["%1"]
        self.assertTrue(state["active"])
        self.assertEqual(state["session_id"], SESSION_ID)
        self.assertEqual(state["turn_id"], "turn-1")

    def test_codex_prompt_carries_message_id_and_claude_prefix_stays_compatible(self):
        self.assertEqual(
            watcher.agent_prompt("hello", watcher.AgentKind.CODEX, 9),
            f"{submit_hook.PROMPT_MARKER} [tg-message-id:9] hello",
        )
        self.assertEqual(
            watcher.agent_prompt("hello", watcher.AgentKind.CLAUDE, 9),
            f"{submit_hook.PROMPT_MARKER} hello",
        )

    def test_submit_hook_ignores_local_and_other_agent_prompts(self):
        with patch.object(submit_hook.tg_stats, "find_codex_rollout", return_value=self.rollout):
            self.assertFalse(submit_hook.mark_prompt_accepted({
                "hook_event_name": "UserPromptSubmit", "session_id": SESSION_ID,
                "prompt": "local prompt",
            }, pane_id="%1"))
            tg_sessions.save_agent("%1", "claude")
            self.assertFalse(submit_hook.mark_prompt_accepted({
                "hook_event_name": "UserPromptSubmit", "session_id": SESSION_ID,
                "prompt": f"{submit_hook.PROMPT_MARKER} [tg-message-id:9] hello",
            }, pane_id="%1"))

    def test_forwards_only_new_commentary_and_never_replays_after_poll(self):
        self.append({"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "old"}]}})
        self.accept()
        self.append({"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "checking files"}]}})
        self.append({"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "done"}]}})
        with patch.object(watcher.tg_stats, "find_codex_rollout", return_value=self.rollout), \
             patch.object(watcher, "tg_send", return_value={"ok": True}) as send:
            watcher.poll_codex_progress()
            watcher.poll_codex_progress()
        send.assert_called_once_with("💬 Codex: checking files", topic_id=42, reply_to=9)

    def test_reacts_only_after_codex_records_the_user_message(self):
        self.accept()
        with patch.object(watcher.tg_stats, "find_codex_rollout", return_value=self.rollout), \
             patch.object(watcher, "_set_reaction", return_value={"ok": True}) as react:
            watcher.poll_codex_progress()
            react.assert_not_called()
            self.append({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"{submit_hook.PROMPT_MARKER} [tg-message-id:9] hello"}]}})
            watcher.poll_codex_progress()
            watcher.poll_codex_progress()
        react.assert_called_once_with(9, "👀", quick=True)

    def test_failed_reaction_retries_the_same_user_record(self):
        self.accept()
        self.append({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"{submit_hook.PROMPT_MARKER} [tg-message-id:9] hello"}]}})
        with patch.object(watcher.tg_stats, "find_codex_rollout", return_value=self.rollout), \
             patch.object(watcher, "_set_reaction", side_effect=[{"ok": False}, {"ok": True}]) as react:
            watcher.poll_codex_progress()
            self.assertEqual(tg_sessions._load()["pane_to_codex_progress"]["%1"]["offset"], 0)
            watcher.poll_codex_progress()
        self.assertEqual(react.call_count, 2)

    def test_finds_rollout_for_an_already_running_codex_pane(self):
        root = os.path.join(self.temp.name, ".codex")
        path = os.path.join(root, "sessions", "2026", "09", "24", f"rollout-2026-09-24T10-00-00-{SESSION_ID}.jsonl")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as handle:
            handle.write("{}\n")

        def run(argv, **_):
            if argv[0] == "tmux":
                return subprocess.CompletedProcess(argv, 0, stdout="123\n")
            return subprocess.CompletedProcess(argv, 0, stdout=f"p123\nn{path}\n")

        with patch.object(watcher.tg_stats, "CODEX_HOME", root), \
             patch.object(watcher.tg_stats, "find_codex_rollout", side_effect=lambda sid: path if sid == SESSION_ID else ""), \
             patch.object(watcher.subprocess, "run", side_effect=run):
            self.assertEqual(watcher._codex_delivery_context("%1"), (SESSION_ID, 3))

    def test_completed_fast_turn_does_not_send_late_progress(self):
        self.accept()
        self.append({"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "working"}]}})
        self.append({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}})
        with patch.object(watcher.tg_stats, "find_codex_rollout", return_value=self.rollout), \
             patch.object(watcher, "tg_send") as send:
            watcher.poll_codex_progress()
        send.assert_not_called()
        self.assertFalse(tg_sessions._load()["pane_to_codex_progress"]["%1"]["active"])

    def test_incomplete_line_is_delivered_once_after_completion(self):
        self.accept()
        record = {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "half line"}]}}
        with open(self.rollout, "a") as handle:
            handle.write(json.dumps(record))
        with patch.object(watcher.tg_stats, "find_codex_rollout", return_value=self.rollout), \
             patch.object(watcher, "tg_send", return_value={"ok": True}) as send:
            watcher.poll_codex_progress()
            send.assert_not_called()
            with open(self.rollout, "a") as handle:
                handle.write("\n")
            watcher.poll_codex_progress()
            watcher.poll_codex_progress()
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
