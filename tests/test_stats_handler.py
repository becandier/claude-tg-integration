import unittest
from unittest.mock import patch

import tg_reply_watcher as watcher
import tg_stats as stats
from agent_runtime import AgentKind

USAGE_OVERLAY = """\
   Settings  Status   Config   Usage   Stats

   Current session
   █  7% used
   Resets 1:10pm (Europe/Moscow)
"""
CONTEXT_BLOCK = """\
  Context Usage
     38.5k/1m tokens (4%)   Opus 5 (1M context)
     Auto-compact window: 1m tokens
     /context all to expand
"""


class StatsRoutingTests(unittest.TestCase):
    """`/stats` и синоним `/usage` должны попадать в один обработчик."""

    def test_both_spellings_are_recognised(self):
        for raw in ("/stats", "/usage", "/stats@claude_bot", "/usage extra"):
            _, command = watcher.parse_bot_command(raw)
            self.assertIn(command, ("/stats", "/usage"), raw)

    def test_other_commands_are_not_captured(self):
        for raw in ("/status", "/statsx", "/model"):
            _, command = watcher.parse_bot_command(raw)
            self.assertNotIn(command, ("/stats", "/usage"), raw)


class StatsGuardTests(unittest.TestCase):
    @patch.object(watcher, "tg_send")
    def test_outside_a_topic_it_explains_instead_of_probing(self, tg_send):
        watcher.handle_stats({"message_id": 5, "chat": {}})
        self.assertIn("только внутри топика", tg_send.call_args[0][0])

    @patch.object(watcher, "tg_send")
    @patch.object(watcher.tg_sessions, "get_pane_by_topic", return_value="")
    def test_topic_without_session_reports_it(self, _pane, tg_send):
        watcher.handle_stats({"message_id": 5, "message_thread_id": 42})
        self.assertIn("Нет активной сессии", tg_send.call_args[0][0])

    @patch.object(watcher, "tg_send")
    @patch.object(watcher, "pane_exists", return_value=False)
    @patch.object(watcher.tg_sessions, "get_pane_by_topic", return_value="%9")
    def test_dead_pane_reports_it(self, _pane, _exists, tg_send):
        watcher.handle_stats({"message_id": 5, "message_thread_id": 42})
        self.assertIn("Нет активной сессии", tg_send.call_args[0][0])


class StatsDispatchTests(unittest.TestCase):
    def _run(self, agent):
        with patch.object(watcher, "tg_send") as tg_send, \
                patch.object(watcher, "tg_react"), \
                patch.object(watcher, "pane_exists", return_value=True), \
                patch.object(watcher.tg_sessions, "get_pane_by_topic", return_value="%9"), \
                patch.object(watcher.tg_sessions, "get_agent", return_value=agent), \
                patch.object(watcher, "probe_claude_stats") as claude, \
                patch.object(watcher, "probe_codex_stats") as codex:
            claude.return_value = stats.SessionStats(agent="claude")
            codex.return_value = stats.SessionStats(agent="codex")
            watcher.handle_stats({"message_id": 5, "message_thread_id": 42})
            return claude, codex, tg_send

    def test_codex_session_uses_the_codex_probe(self):
        claude, codex, tg_send = self._run("codex")
        codex.assert_called_once_with("%9")
        claude.assert_not_called()
        self.assertEqual(tg_send.call_args.kwargs["topic_id"], 42)

    def test_claude_session_uses_the_claude_probe(self):
        claude, codex, _ = self._run("claude")
        claude.assert_called_once_with("%9")
        codex.assert_not_called()


class ProbeClaudeTests(unittest.TestCase):
    def test_busy_session_skips_the_tui_and_says_why(self):
        """Слэш-команды проходят только между ходами — иначе уйдут в промпт."""
        with patch.object(watcher, "_capture_pane", return_value="Working (esc to interrupt)"), \
                patch.object(watcher, "_current_effort", return_value="xhigh"), \
                patch.object(watcher, "_claude_transcript_context", return_value=None), \
                patch.object(watcher, "_type_command") as typed:
            result = watcher.probe_claude_stats("%9")
        typed.assert_not_called()
        self.assertEqual(result.limits, [])
        self.assertTrue(any("занята" in note for note in result.notes))

    def test_idle_session_reads_context_then_limits(self):
        screens = iter([
            "❯ ",                       # до /context
            CONTEXT_BLOCK, CONTEXT_BLOCK,    # рендер /context стабилизировался
            CONTEXT_BLOCK,                   # снимок до /usage
            USAGE_OVERLAY, USAGE_OVERLAY,    # оверлей /usage отрисован
            "❯ ",                       # оверлей закрыт
        ])
        with patch.object(watcher, "_capture_pane", side_effect=lambda _: next(screens)), \
                patch.object(watcher, "_current_effort", return_value="xhigh"), \
                patch.object(watcher, "_type_command", return_value=True), \
                patch.object(watcher, "_collect_usage_screen", return_value=USAGE_OVERLAY), \
                patch.object(watcher.tg_sessions, "get_model", return_value=""), \
                patch.object(watcher, "tmux_send_keys", return_value=True), \
                patch.object(watcher.time, "sleep"):
            result = watcher.probe_claude_stats("%9")
        self.assertEqual(result.context.used_tokens, 38_500)
        self.assertEqual(result.model, "Opus 5 (1M context)")
        self.assertEqual([limit.used_percent for limit in result.limits], [7.0])
        self.assertEqual(result.notes, [])


class UsageScrollTests(unittest.TestCase):
    def test_scrolling_stops_when_the_overlay_is_gone(self):
        """Down вне оверлея листает историю ввода — листать нельзя."""
        with patch.object(watcher, "tmux_send_keys") as keys, \
                patch.object(watcher, "_capture_pane", return_value="❯ "), \
                patch.object(watcher.time, "sleep"):
            watcher._collect_usage_screen("%9", "❯ обычный композер")
        keys.assert_not_called()

    def test_scrolling_accumulates_frames_until_they_repeat(self):
        frames = iter([USAGE_OVERLAY + "кадр2", USAGE_OVERLAY + "кадр2"])
        with patch.object(watcher, "tmux_send_keys", return_value=True), \
                patch.object(watcher, "_capture_pane", side_effect=lambda _: next(frames)), \
                patch.object(watcher.time, "sleep"):
            collected = watcher._collect_usage_screen("%9", USAGE_OVERLAY)
        self.assertIn("кадр2", collected)


class CloseOverlayTests(unittest.TestCase):
    def test_no_escape_when_nothing_is_open(self):
        """Двойной Escape на пустом композере открывает выбор точки возврата."""
        with patch.object(watcher, "_capture_pane", return_value="❯ "), \
                patch.object(watcher, "tmux_send_keys") as keys, \
                patch.object(watcher.time, "sleep"):
            watcher._close_claude_overlay("%9")
        keys.assert_not_called()

    def test_escape_repeats_while_the_overlay_stays(self):
        with patch.object(watcher, "_capture_pane", return_value=USAGE_OVERLAY), \
                patch.object(watcher, "tmux_send_keys") as keys, \
                patch.object(watcher.time, "sleep"):
            watcher._close_claude_overlay("%9")
        self.assertEqual(keys.call_count, 2)


class ProbeCodexTests(unittest.TestCase):
    STATUS = """\
│  Model:           gpt-5.6-sol (reasoning medium, summaries auto)  │
│  Model provider:  openai                                         │
│  Session:         01a09461-2263-7451-8763-d264429bb1f8            │
│  Weekly limit:    [██] 10% left (resets 11:49 on 15 Sep)           │
"""

    def test_status_screen_is_parsed_and_session_id_cached(self):
        ready = "› Ask Codex to do anything\n\n  gpt-5.6-sol medium · ~/projects"
        screens = iter([ready, self.STATUS, self.STATUS])
        with patch.object(watcher, "_capture_pane", side_effect=lambda _: next(screens)), \
                patch.object(watcher, "_type_command", return_value=True), \
                patch.object(watcher.tg_sessions, "save_session_id") as save, \
                patch.object(watcher.time, "sleep"):
            result = watcher.probe_codex_stats("%9")
        save.assert_called_once_with("%9", "01a09461-2263-7451-8763-d264429bb1f8")
        self.assertEqual(result.limits[0].used_percent, 90.0)
        self.assertEqual(result.notes, [])

    PAYLOAD = {
        "info": {
            "last_token_usage": {"total_tokens": 19_464},
            "model_context_window": 258_400,
        },
        "rate_limits": {"primary": {"used_percent": 90.0, "window_minutes": 10080}},
    }

    def test_truncated_status_is_completed_from_the_journal(self):
        """Узкий терминал срезает часть бокса — идентификация остаётся от /status."""
        trimmed = "\n".join(self.STATUS.splitlines()[:3])  # без строки лимита
        screens = iter([
            "\u203a Ask Codex to do anything\n\n  gpt-5.6-sol medium \u00b7 ~/projects",
            trimmed, trimmed,
        ])
        with patch.object(watcher, "_capture_pane", side_effect=lambda _: next(screens)), \
                patch.object(watcher, "_type_command", return_value=True), \
                patch.object(watcher.tg_sessions, "save_session_id"), \
                patch.object(watcher.tg_stats, "find_codex_rollout", return_value="/x.jsonl"), \
                patch.object(watcher.tg_stats, "read_codex_token_count", return_value=self.PAYLOAD), \
                patch.object(watcher.time, "sleep"):
            result = watcher.probe_codex_stats("%9")
        self.assertEqual(result.model, "gpt-5.6-sol")
        self.assertEqual(result.plan, "")
        self.assertEqual(result.limits[0].used_percent, 90.0)
        self.assertEqual(result.context.used_tokens, 19_464)

    def test_busy_session_falls_back_to_the_rollout_journal(self):
        payload = {
            "info": {
                "last_token_usage": {"total_tokens": 19_464},
                "model_context_window": 258_400,
            },
            "rate_limits": {"primary": {"used_percent": 90.0, "window_minutes": 10080}},
        }
        with patch.object(watcher, "_capture_pane", return_value="Working (esc to cancel)"), \
                patch.object(watcher, "_current_effort", return_value="medium"), \
                patch.object(watcher, "_type_command") as typed, \
                patch.object(watcher.tg_sessions, "get_session_id", return_value="abc12345"), \
                patch.object(watcher.tg_stats, "find_codex_rollout", return_value="/x.jsonl"), \
                patch.object(watcher.tg_stats, "read_codex_token_count", return_value=payload):
            result = watcher.probe_codex_stats("%9")
        typed.assert_not_called()
        self.assertEqual(result.context.used_tokens, 19_464)
        self.assertEqual(result.limits[0].used_percent, 90.0)
        self.assertTrue(any("журнала" in note for note in result.notes))


class WaitForRenderTests(unittest.TestCase):
    def test_stale_screen_is_not_mistaken_for_fresh_output(self):
        """Старый блок /context ещё виден — ждём именно новую отрисовку."""
        stale = CONTEXT_BLOCK
        screens = iter([stale, stale, stale + "новое", stale + "новое"])
        with patch.object(watcher, "_capture_pane", side_effect=lambda _: next(screens)), \
                patch.object(watcher.time, "sleep"):
            result = watcher._wait_for_render(
                "%9", watcher.CLAUDE_CONTEXT_TAIL_RE, before=stale, timeout=5.0,
            )
        self.assertIn("новое", result)

    def test_timeout_returns_empty(self):
        with patch.object(watcher, "_capture_pane", return_value="ничего"), \
                patch.object(watcher.time, "sleep"):
            result = watcher._wait_for_render(
                "%9", watcher.CLAUDE_USAGE_OVERLAY_RE, before="", timeout=0.0,
            )
        self.assertEqual(result, "")


class AgentKindTests(unittest.TestCase):
    def test_stats_agent_values_match_agent_kind(self):
        """render_stats подписывает заголовок по значению AgentKind."""
        for kind, title in ((AgentKind.CLAUDE, "Claude Code"), (AgentKind.CODEX, "Codex")):
            text = stats.render_stats(stats.SessionStats(agent=kind.value))
            self.assertIn(title, text)


if __name__ == "__main__":
    unittest.main()
