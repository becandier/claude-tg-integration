import json
import os
import tempfile
import unittest
from unittest.mock import patch

import agent_runtime as runtime
import tg_resume
import tg_reply_watcher as watcher

SID = "464cdc52-68d7-480b-b569-8df294336644"
NEW_SID = "0488ab39-1354-48f4-aa19-6d73e49d029c"


class TempStateMixin:
    """Снимок, хранилище сессий и ~/.claude/sessions — во временном каталоге."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.sessions_dir = os.path.join(self.tmp, "claude-sessions")
        os.makedirs(self.sessions_dir)
        for target, name, value in (
            (tg_resume, "RESUME_FILE", os.path.join(self.tmp, "state", "resume.json")),
            (watcher.tg_sessions, "SESSIONS_FILE", os.path.join(self.tmp, "sessions.json")),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_claude_session(self, pid, session_id, tmux, updated_at=1):
        with open(os.path.join(self.sessions_dir, f"{pid}.json"), "w") as f:
            json.dump({"pid": pid, "sessionId": session_id, "tmux": tmux, "updatedAt": updated_at}, f)


class ClaudeSessionLookupTests(TempStateMixin, unittest.TestCase):
    def test_matches_pane_and_tmux_session_and_prefers_newest(self):
        self.write_claude_session(1, SID, "claude-1:@3.%3", updated_at=10)
        self.write_claude_session(2, NEW_SID, "claude-1:@3.%3", updated_at=20)
        self.assertEqual(tg_resume.claude_session_id("%3", "claude-1", self.sessions_dir), NEW_SID)

    def test_ignores_same_pane_id_of_other_tmux_session(self):
        # После перезапуска tmux pane id повторяются: старый файл не подходит.
        self.write_claude_session(1, SID, "claude-old:@3.%3")
        self.assertEqual(tg_resume.claude_session_id("%3", "claude-new", self.sessions_dir), "")

    def test_does_not_match_pane_suffix_prefix(self):
        self.write_claude_session(1, SID, "claude-1:@3.%31")
        self.assertEqual(tg_resume.claude_session_id("%3", "claude-1", self.sessions_dir), "")


class SnapshotRefreshTests(TempStateMixin, unittest.TestCase):
    def sessions(self, **extra):
        data = {
            "pane_to_topic": {"%3": 100, "%4": 200},
            "pane_to_agent": {"%3": "claude", "%4": "codex"},
            "pane_to_project": {"%3": "/p/a", "%4": "/p/b"},
            "pane_to_model": {"%3": "Fable 5.1", "%4": "gpt-6-sol"},
            "pane_to_effort": {"%3": "xhigh", "%4": "ultra"},
            "pane_to_session_id": {"%4": NEW_SID},
            "pane_to_title": {"%3": "Рефакторинг"},
        }
        data.update(extra)
        return data

    def test_refresh_writes_records_for_live_panes(self):
        self.write_claude_session(1, SID, "claude-1:@3.%3")
        tg_resume.refresh(self.sessions(), {"%3": "claude-1", "%4": "codex-1"},
                          sessions_dir=self.sessions_dir, now=5.0)
        self.assertEqual(tg_resume.get(100), {
            "agent": "claude", "project": "/p/a", "session_id": SID,
            "model": "Fable 5.1", "effort": "xhigh", "title": "Рефакторинг",
            "topic_name": "", "updated_at": 5.0,
        })
        self.assertEqual(tg_resume.get(200)["session_id"], NEW_SID)
        self.assertEqual(tg_resume.get(200)["agent"], "codex")

    def test_session_id_follows_resume_and_unknown_id_keeps_previous(self):
        self.write_claude_session(1, SID, "claude-1:@3.%3")
        live = {"%3": "claude-1"}
        tg_resume.refresh(self.sessions(), live, sessions_dir=self.sessions_dir, now=1.0)
        self.write_claude_session(1, NEW_SID, "claude-1:@3.%3", updated_at=2)
        tg_resume.refresh(self.sessions(), live, sessions_dir=self.sessions_dir, now=2.0)
        self.assertEqual(tg_resume.get(100)["session_id"], NEW_SID)
        os.unlink(os.path.join(self.sessions_dir, "1.json"))
        tg_resume.refresh(self.sessions(), live, sessions_dir=self.sessions_dir, now=3.0)
        self.assertEqual(tg_resume.get(100)["session_id"], NEW_SID)

    def test_dead_pane_keeps_its_record(self):
        self.write_claude_session(1, SID, "claude-1:@3.%3")
        tg_resume.refresh(self.sessions(), {"%3": "claude-1"}, sessions_dir=self.sessions_dir)
        tg_resume.refresh({}, {}, sessions_dir=self.sessions_dir)
        self.assertEqual(tg_resume.get(100)["session_id"], SID)

    def test_forget_removes_record(self):
        self.write_claude_session(1, SID, "claude-1:@3.%3")
        tg_resume.refresh(self.sessions(), {"%3": "claude-1"}, sessions_dir=self.sessions_dir)
        tg_resume.forget(100)
        self.assertIsNone(tg_resume.get(100))

    def test_unchanged_record_is_not_rewritten(self):
        self.write_claude_session(1, SID, "claude-1:@3.%3")
        live = {"%3": "claude-1"}
        tg_resume.refresh(self.sessions(), live, sessions_dir=self.sessions_dir, now=1.0)
        tg_resume.refresh(self.sessions(), live, sessions_dir=self.sessions_dir, now=9.0)
        self.assertEqual(tg_resume.get(100)["updated_at"], 1.0)


class ResumeDecisionTests(unittest.TestCase):
    record = {"agent": "claude", "project": "/p", "session_id": SID}

    def test_restores_complete_record(self):
        self.assertEqual(tg_resume.resume_decision(
            self.record, project_exists=lambda _p: True, history_exists=lambda _r: True,
        ), tg_resume.RESTORE)

    def test_refusals(self):
        exists = lambda _p: True  # noqa: E731
        self.assertEqual(tg_resume.resume_decision(None), tg_resume.NO_SNAPSHOT)
        self.assertEqual(tg_resume.resume_decision(
            dict(self.record, session_id=""), project_exists=exists,
        ), tg_resume.NO_SESSION_ID)
        self.assertEqual(tg_resume.resume_decision(
            dict(self.record, session_id="--model"), project_exists=exists,
        ), tg_resume.NO_SESSION_ID)
        self.assertEqual(tg_resume.resume_decision(
            self.record, project_exists=lambda _p: False,
        ), tg_resume.NO_PROJECT)
        self.assertEqual(tg_resume.resume_decision(
            self.record, project_exists=exists, history_exists=lambda _r: False,
        ), tg_resume.NO_HISTORY)


class ResumeArgvTests(unittest.TestCase):
    def test_claude_resume_with_model_and_effort(self):
        spec = runtime.LaunchSpec(
            agent=runtime.AgentKind.CLAUDE, session_name="claude-1", project_dir="/p",
            model="fable", effort="xhigh", resume_session_id=SID,
        )
        self.assertEqual(runtime.build_launch_argv(spec)[7:], [
            "claude", "--dangerously-skip-permissions",
            "--resume", SID, "--model", "fable", "--effort", "xhigh",
        ])

    def test_codex_resume_puts_flags_after_subcommand_and_id_last(self):
        spec = runtime.LaunchSpec(
            agent=runtime.AgentKind.CODEX, session_name="codex-1", project_dir="/p",
            model="gpt-6-sol", effort="high", resume_session_id=SID,
        )
        self.assertEqual(runtime.build_launch_argv(spec)[7:], [
            "codex", "resume", "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust", "--no-alt-screen",
            "-m", "gpt-6-sol", "-c", 'model_reasoning_effort="high"', SID,
        ])

    def test_rejects_option_like_session_id(self):
        spec = runtime.LaunchSpec(
            agent=runtime.AgentKind.CLAUDE, session_name="claude-1", project_dir="/p",
            resume_session_id="--print",
        )
        with self.assertRaises(ValueError):
            runtime.build_launch_argv(spec)

    def test_settings_split_launch_flags_and_late_effort(self):
        claude, codex = runtime.AgentKind.CLAUDE, runtime.AgentKind.CODEX
        self.assertEqual(runtime.resume_cli_settings(claude, "Fable 5.1", "xhigh"), ("fable", "xhigh", ""))
        self.assertEqual(runtime.resume_cli_settings(claude, "Opus 5 (1M context)", "ultracode"),
                         ("opus", "", "ultracode"))
        self.assertEqual(runtime.resume_cli_settings(codex, "gpt-6-sol", "ultra"), ("gpt-6-sol", "", "ultra"))
        self.assertEqual(runtime.resume_cli_settings(codex, "", ""), ("", "", ""))


class WatcherResumeTests(TempStateMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.project = os.path.join(self.tmp, "proj")
        os.makedirs(self.project)
        self.sent = []
        for name, value in (
            ("tg_send", {"ok": True, "result": {"message_id": 900}}), ("tg_react", {"ok": True}),
            ("tg_api", {"ok": True}), ("open_terminal_with_tmux", None),
            ("mark_pane_waiting", None), ("_resume_history_exists", True),
        ):
            patcher = patch.object(watcher, name, return_value=value)
            mock = patcher.start()
            self.addCleanup(patcher.stop)
            if name == "tg_send":
                self.tg_send = mock
        which = patch.object(watcher.shutil, "which", return_value="/bin/claude")
        which.start()
        self.addCleanup(which.stop)

    def save_snapshot(self, **extra):
        record = {"agent": "claude", "project": self.project, "session_id": SID,
                  "model": "Fable 5.1", "effort": "xhigh", "title": "Тема", "topic_name": "", "updated_at": 1}
        record.update(extra)
        tg_resume._with_lock(lambda data: data.update({"100": record}) or True)

    def test_restore_spawns_resumed_session_and_second_message_reuses_it(self):
        self.save_snapshot()
        live = set()

        def spawn(spec, topic_id):
            watcher.tg_sessions.save_topic("%9", topic_id)
            watcher.tg_sessions.save_agent("%9", spec.agent.value)
            watcher.tg_sessions.save_project("%9", spec.project_dir)
            live.add("%9")
            return "%9", None

        with patch.object(watcher, "spawn_agent_session", side_effect=spawn) as spawner, \
                patch.object(watcher, "pane_exists", side_effect=lambda p: p in live):
            self.assertEqual(watcher.restore_topic_session(100, 7), "%9")
            self.assertEqual(watcher.restore_topic_session(100, 8), "%9")
            self.assertEqual(spawner.call_count, 1)
            spec = spawner.call_args.args[0]
            self.assertEqual((spec.resume_session_id, spec.model, spec.effort), (SID, "fable", "xhigh"))

            state = watcher.tg_sessions._load()
            self.assertEqual(state["pane_to_startup"]["%9"]["resume_session_id"], SID)
            self.assertEqual(state["pane_to_title"]["%9"], "Тема")
            self.assertEqual(watcher.tg_sessions.get_model("%9"), "Fable 5.1")
            self.assertIn("♻️ Восстанавливаю сессию", self.tg_send.call_args_list[0].args[0])

            # Сообщение ждёт готовности CLI в общей очереди запуска.
            self.assertFalse(watcher.queue_if_blocked("%9", "привет", 7, 100))
            self.assertEqual(watcher.tg_sessions.pending_messages("%9"), [{"text": "привет", "message_id": 7}])

    def test_missing_project_is_reported_without_spawning(self):
        self.save_snapshot(project=os.path.join(self.tmp, "gone"))
        with patch.object(watcher, "spawn_agent_session") as spawner, \
                patch.object(watcher, "pane_exists", return_value=False):
            self.assertEqual(watcher.restore_topic_session(100, 7), "")
        spawner.assert_not_called()
        self.assertIn("каталог проекта не найден", self.tg_send.call_args.args[0])

    def test_without_snapshot_keeps_previous_behavior(self):
        with patch.object(watcher, "spawn_agent_session") as spawner, \
                patch.object(watcher, "pane_exists", return_value=False):
            self.assertIsNone(watcher.restore_topic_session(100, 7))
        spawner.assert_not_called()
        self.tg_send.assert_not_called()

    def test_stale_pane_queue_moves_to_restored_pane(self):
        self.save_snapshot()
        watcher.tg_sessions.save_topic("%1", 100)
        watcher.tg_sessions.queue_message("%1", "старое", 5)
        live = set()

        def spawn(spec, topic_id):
            watcher.tg_sessions.save_topic("%9", topic_id)
            live.add("%9")
            return "%9", None

        with patch.object(watcher, "spawn_agent_session", side_effect=spawn), \
                patch.object(watcher, "pane_exists", side_effect=lambda p: p in live), \
                patch.object(watcher, "cleanup_media"):
            self.assertEqual(watcher.restore_topic_session(100, 7), "%9")
        self.assertEqual(watcher.tg_sessions.get_pane_by_topic(100), "%9")
        self.assertEqual(watcher.tg_sessions.pending_messages("%9"), [{"text": "старое", "message_id": 5}])

    def test_resumed_startup_delivers_after_screen_settles_and_applies_late_effort(self):
        watcher.tg_sessions.save_topic("%9", 100)
        watcher.tg_sessions.save_startup("%9", {
            "session": "claude-1", "project": self.project, "started_at": 0, "message_id": 900,
            "resume_session_id": SID, "late_effort": "ultracode",
        })
        watcher.tg_sessions.queue_message("%9", "привет", 7)
        screen = "Fable 5.1 with xhigh effort · Claude Max\n❯ "
        with patch.object(watcher, "pane_exists", return_value=True), \
                patch.object(watcher, "_capture_pane", return_value=screen), \
                patch.object(watcher, "tg_edit", return_value={"ok": True}) as edit, \
                patch.object(watcher, "set_session_effort", return_value=True) as effort, \
                patch.object(watcher, "tmux_send", return_value=True) as send:
            watcher.check_starting_sessions()  # первый снимок — ещё не доверяем
            send.assert_not_called()
            edit.assert_not_called()
            watcher.check_starting_sessions()  # экран стабилен: готов, effort командой
            effort.assert_called_once_with("%9", "ultracode", watcher.AgentKind.CLAUDE)
            self.assertIn("восстановлен и готов", edit.call_args.args[1])
            send.assert_not_called()
            watcher.check_starting_sessions()  # затем очередь
            send.assert_called_once_with("%9", "привет")
        self.assertEqual(watcher.tg_sessions.get_effort("%9"), "ultracode")


class ClosePreservationTests(TempStateMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        tg_resume._with_lock(lambda data: data.update({"100": {"agent": "claude", "session_id": SID}}) or True)
        for name in ("tg_send", "tg_api", "cleanup_media", "clear_pane_waiting"):
            patcher = patch.object(watcher, name, return_value={"ok": True})
            patcher.start()
            self.addCleanup(patcher.stop)
        last = patch.object(watcher, "_last_pane_check", 0.0)
        last.start()
        self.addCleanup(last.stop)

    def test_dead_pane_keeps_snapshot(self):
        watcher.tg_sessions.save_topic("%1", 100)
        with patch.object(watcher, "pane_exists", return_value=False), \
                patch.object(watcher, "live_tmux_panes", return_value={}):
            watcher.check_dead_panes()
        self.assertEqual(watcher.tg_sessions.get_topic("%1"), "")
        self.assertEqual(tg_resume.get(100)["session_id"], SID)

    def test_close_in_topic_forgets_snapshot(self):
        with patch.object(watcher.tg_sessions, "get_pane_by_topic", return_value=""):
            watcher.handle_close({"message_id": 1, "message_thread_id": 100})
        self.assertIsNone(tg_resume.get(100))


if __name__ == "__main__":
    unittest.main()
