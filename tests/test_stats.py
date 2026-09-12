import json
import os
import tempfile
import unittest

import tg_stats as stats

# Экран /usage Claude Code: состав окон зависит от плана, отдельные модели идут
# своими блоками. Ширина 120 — шапка блока контекста видна целиком.
CLAUDE_USAGE = """\
   Settings  Status   Config   Usage   Stats

   Session

   Total cost:            $0.0000
   Usage:                 0 input, 0 output, 0 cache read, 0 cache write

   Plugin skill-listing footprint
   Notion                      10 skills · ~226 tok/turn

   Total                       ~395 tok/turn

   Current session
   █                                                  2% used
   Resets 1:10pm (Europe/Moscow)

   Current week (all models)
   ████████                                   32% used
   Resets Sep 12 at 11am (Europe/Moscow)
   +50% weekly limits promo through Sep 13 · clau.de/cc-50-promo

   Current week (Fable)
   █████████████                        52% used
   Resets Sep 12 at 11am (Europe/Moscow)

   What's contributing to your limits usage?

   84% of your usage was at >150k context
   47% of your usage came from subagent-heavy sessions
"""

CLAUDE_CONTEXT = """\
❯ /context
  ⌎  Context Usage
     ⛁ ⛁ ⛀ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶   Opus 5 (1M context)
     ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶   claude-opus-5[1m]
     ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶   38.5k/1m tokens (4%)
     ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶   Estimated usage by category
     ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶   ⛁ System prompt: 4.7k tokens (0.5%)
     ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶   ⛁ System tools: 20.1k tokens (2.0%)
     ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶ ⚶   ⛁ Messages: 1.3k tokens (0.1%)
                                   ⚶ Free space: 928.5k (92.8%)
                                   ⛝ Autocompact buffer: 33k tokens (3.3%)

     Auto-compact window: 1m tokens
"""

# То же, но узкий терминал: шапка с "38.5k/1m tokens" вытолкнута за кадр,
# а проценты категорий перенесены на следующую строку.
CLAUDE_CONTEXT_NARROW = """\
     (0.2%)
     ⚶ ⚶ ⚶ ⛝ ⛝   ⛁ Skills: 10k tokens (1.0%)
                   ⛁ Messages: 19.4k tokens (1.9%)
                   ⚶ Free space: 910.4k (91.0%)
                   ⛝ Autocompact buffer: 33k tokens
     (3.3%)

     Auto-compact window: 1m tokens
"""

CODEX_STATUS = """\
╭───────────────╮
│  Model:                       gpt-5.6-sol (reasoning medium, summaries auto) │
│  Model provider:              openai                                         │
│  Account:                     user@example.com (Pro Lite)                    │
│  Session:                     01a09461-2263-7451-8763-d264429bb1f8           │
│                                                                              │
│  Context window:              97% left (19.5K used / 258K)                   │
│  Weekly limit:                [██░░░░] 10% left                            │
│                               (resets 11:49 on 15 Sep)                       │
│  GPT-5.3-Codex-Spark limit:                                                   │
│  5h limit:                    [██████] 100% left (resets 14:57)             │
╰───────────────╯
"""


class TokenHelperTests(unittest.TestCase):
    def test_parse_token_count_units(self):
        self.assertEqual(stats.parse_token_count("38.5k"), 38_500)
        self.assertEqual(stats.parse_token_count("1m"), 1_000_000)
        self.assertEqual(stats.parse_token_count("258K"), 258_000)
        self.assertEqual(stats.parse_token_count("314"), 314)

    def test_parse_token_count_rejects_garbage(self):
        for raw in ("", "n/a", "1.2.3k", None):
            self.assertEqual(stats.parse_token_count(raw), 0)

    def test_format_tokens_drops_trailing_zero(self):
        self.assertEqual(stats.format_tokens(1_000_000), "1M")
        self.assertEqual(stats.format_tokens(258_400), "258.4k")
        self.assertEqual(stats.format_tokens(314), "314")

    def test_progress_bar_is_clamped(self):
        self.assertEqual(stats.progress_bar(0), "░" * 10)
        self.assertEqual(stats.progress_bar(100), "█" * 10)
        self.assertEqual(stats.progress_bar(52), "█" * 5 + "░" * 5)
        self.assertEqual(stats.progress_bar(150), "█" * 10)


class ClaudeUsageTests(unittest.TestCase):
    def test_parses_every_limit_window(self):
        limits = stats.parse_claude_usage(CLAUDE_USAGE)
        self.assertEqual(
            [(limit.name, limit.used_percent) for limit in limits],
            [("Сессия", 2.0), ("Неделя · все модели", 32.0), ("Неделя · Fable", 52.0)],
        )
        self.assertEqual(limits[0].resets, "1:10pm (Europe/Moscow)")

    def test_ignores_contributing_percentages(self):
        """«84% of your usage…» — не лимит и не должно попадать в список."""
        names = [limit.name for limit in stats.parse_claude_usage(CLAUDE_USAGE)]
        self.assertNotIn("What's contributing to your limits usage?", names)

    def test_repeated_frames_collapse_and_keep_resets(self):
        """Кадр без строки Resets не должен вытеснять полный повтор."""
        clipped = "   Current session\n   █  2% used\n"
        limits = stats.parse_claude_usage(clipped + CLAUDE_USAGE)
        session = [limit for limit in limits if limit.name == "Сессия"]
        self.assertEqual(len(session), 1)
        self.assertEqual(session[0].resets, "1:10pm (Europe/Moscow)")

    def test_scroll_indicator_is_not_part_of_the_reset_time(self):
        """Стрелка прокрутки оверлея стоит у правого края той же строки."""
        screen = (
            "   Current session\n"
            "   \u2588  2% used\n"
            "   Resets 1:10pm (Europe/Moscow)" + " " * 40 + "\u2193\n"
        )
        limit = stats.parse_claude_usage(screen)[0]
        self.assertEqual(limit.resets, "1:10pm (Europe/Moscow)")

    def test_empty_screen_gives_no_limits(self):
        self.assertEqual(stats.parse_claude_usage(""), [])


class ClaudeContextTests(unittest.TestCase):
    def test_parses_header_and_categories(self):
        context = stats.parse_claude_context(CLAUDE_CONTEXT)
        self.assertEqual(context.used_tokens, 38_500)
        self.assertEqual(context.total_tokens, 1_000_000)
        self.assertEqual(context.percent, 4.0)
        self.assertFalse(context.approximate)
        self.assertEqual(
            dict(context.breakdown),
            {"System prompt": 4_700, "System tools": 20_100, "Messages": 1_300},
        )

    def test_category_label_excludes_grid_glyphs(self):
        context = stats.parse_claude_context(CLAUDE_CONTEXT)
        for name, _ in context.breakdown:
            self.assertNotIn("⚶", name)

    def test_model_name_comes_from_the_context_block(self):
        """В длинной сессии шапка CLI уже за кадром, подпись блока — нет."""
        self.assertEqual(
            stats.parse_claude_context_model(CLAUDE_CONTEXT), "Opus 5 (1M context)",
        )
        self.assertEqual(stats.parse_claude_context_model(CLAUDE_CONTEXT_NARROW), "")

    def test_narrow_screen_without_header_has_no_context(self):
        self.assertIsNone(stats.parse_claude_context(CLAUDE_CONTEXT_NARROW))

    def test_autocompact_window_survives_narrow_screen(self):
        self.assertEqual(
            stats.parse_claude_autocompact_window(CLAUDE_CONTEXT_NARROW), 1_000_000,
        )
        self.assertEqual(stats.parse_claude_autocompact_window("ничего"), 0)


class ClaudeWindowTests(unittest.TestCase):
    def test_window_from_pane_header(self):
        header = "Opus 5 (1M context) with xhigh effort · Claude Max"
        self.assertEqual(stats.claude_context_window(header), 1_000_000)
        self.assertEqual(
            stats.claude_context_window("Sonnet 4.5 (200k context) · Claude Max"),
            200_000,
        )

    def test_window_from_model_id_suffix(self):
        self.assertEqual(
            stats.claude_context_window(model_id="claude-opus-5[1m]"), 1_000_000,
        )

    def test_unknown_window_is_zero_not_a_guess(self):
        """Модель без маркера окна бывает и 200k, и 1M — угадывать нельзя."""
        self.assertEqual(stats.claude_context_window("", "claude-opus-5"), 0)


class CodexStatusTests(unittest.TestCase):
    def test_parses_identity_and_context(self):
        parsed = stats.parse_codex_status(CODEX_STATUS)
        self.assertEqual(parsed.model, "gpt-5.6-sol")
        self.assertEqual(parsed.effort, "medium")
        self.assertEqual(parsed.plan, "Pro Lite")
        self.assertEqual(parsed.session_id, "01a09461-2263-7451-8763-d264429bb1f8")
        self.assertEqual(parsed.context.used_tokens, 19_500)
        self.assertEqual(parsed.context.total_tokens, 258_000)
        self.assertEqual(parsed.context.percent, 3.0)

    def test_percent_left_becomes_percent_used(self):
        limits = stats.parse_codex_status(CODEX_STATUS).limits
        weekly = next(limit for limit in limits if not limit.group)
        self.assertEqual(weekly.used_percent, 90.0)

    def test_wrapped_resets_tail_attaches_to_its_limit(self):
        weekly = stats.parse_codex_status(CODEX_STATUS).limits[0]
        self.assertEqual(weekly.resets, "11:49 on 15 Sep")

    def test_sub_limits_keep_their_group(self):
        spark = stats.parse_codex_status(CODEX_STATUS).limits[1]
        self.assertEqual(spark.group, "GPT-5.3-Codex-Spark limit")
        self.assertEqual(spark.name, "5 часов")
        self.assertEqual(spark.used_percent, 0.0)

    def test_only_the_last_status_box_is_parsed(self):
        """Старый бокс остаётся на экране — лимиты не должны удваиваться."""
        stale = CODEX_STATUS.replace("10% left", "40% left")
        parsed = stats.parse_codex_status(stale + CODEX_STATUS)
        self.assertEqual(len(parsed.limits), 2)
        self.assertEqual(parsed.limits[0].used_percent, 90.0)

    def test_last_box_wins_when_its_top_border_is_off_screen(self):
        """Узкое окно срезает верхнюю рамку: границу ищем по рамке предыдущего."""
        stale = CODEX_STATUS.replace("10% left", "40% left")
        clipped = "\n".join(CODEX_STATUS.splitlines()[1:])
        parsed = stats.parse_codex_status(stale + clipped)
        self.assertEqual(len(parsed.limits), 2)
        self.assertEqual(parsed.limits[0].used_percent, 90.0)


class CodexRolloutTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.session_id = "01a09461-2263-7451-8763-d264429bb1f8"
        day = os.path.join(self.home.name, "sessions", "2026", "09", "12")
        os.makedirs(day)
        self.path = os.path.join(
            day, f"rollout-2026-09-12T09-49-48-{self.session_id}.jsonl",
        )
        stale = self._token_event(total=100, window=258_400, used_percent=10.0)
        fresh = self._token_event(total=19_464, window=258_400, used_percent=90.0)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "session_meta"}) + "\n")
            handle.write(json.dumps(stale) + "\n")
            handle.write(json.dumps(fresh) + "\n")
            handle.write(json.dumps({"type": "event_msg", "payload": {}}) + "\n")

    @staticmethod
    def _token_event(total, window, used_percent):
        return {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"total_tokens": total},
                    "model_context_window": window,
                },
                "rate_limits": {
                    "primary": {"used_percent": used_percent, "window_minutes": 10080},
                    "secondary": {"used_percent": 5.0, "window_minutes": 300},
                },
            },
        }

    def test_finds_rollout_by_session_id(self):
        self.assertEqual(
            stats.find_codex_rollout(self.session_id, self.home.name), self.path,
        )

    def test_rejects_unsafe_session_id(self):
        for bad in ("", "../../etc/passwd", "*"):
            self.assertEqual(stats.find_codex_rollout(bad, self.home.name), "")

    def test_reads_the_last_token_count_event(self):
        payload = stats.read_codex_token_count(self.path)
        context = stats.codex_context_from_payload(payload)
        self.assertEqual(context.used_tokens, 19_464)
        self.assertEqual(context.total_tokens, 258_400)
        self.assertEqual(context.percent, 7.5)
        self.assertTrue(context.approximate)

    def test_rate_limits_fallback_labels_windows(self):
        payload = stats.read_codex_token_count(self.path)
        limits = stats.codex_limits_from_payload(payload)
        self.assertEqual(
            [(limit.name, limit.used_percent) for limit in limits],
            [("Неделя", 90.0), ("5 ч.", 5.0)],
        )

    def test_missing_file_is_handled(self):
        self.assertIsNone(stats.read_codex_token_count("/nope/missing.jsonl"))
        self.assertIsNone(stats.codex_context_from_payload(None))
        self.assertEqual(stats.codex_limits_from_payload(None), [])


class ClaudeSessionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def _write(self, name, record):
        with open(os.path.join(self.dir.name, name), "w", encoding="utf-8") as handle:
            json.dump(record, handle)

    def test_matches_session_by_tmux_pane(self):
        self._write("1.json", {
            "sessionId": "aaaa1111-0000-0000-0000-000000000000",
            "cwd": "/Users/me/projects", "tmux": "claude-1:@1.%14", "updatedAt": 1,
        })
        self._write("2.json", {
            "sessionId": "bbbb2222-0000-0000-0000-000000000000",
            "cwd": "/Users/me/projects", "tmux": "claude-2:@2.%4", "updatedAt": 2,
        })
        record = stats.find_claude_session("%4", self.dir.name)
        self.assertEqual(record["sessionId"], "bbbb2222-0000-0000-0000-000000000000")

    def test_prefers_the_freshest_record_for_a_reused_pane(self):
        self._write("old.json", {
            "sessionId": "aaaa1111-0000-0000-0000-000000000000",
            "cwd": "/x", "tmux": "claude-1:@1.%7", "updatedAt": 10,
        })
        self._write("new.json", {
            "sessionId": "cccc3333-0000-0000-0000-000000000000",
            "cwd": "/x", "tmux": "claude-1:@1.%7", "updatedAt": 99,
        })
        record = stats.find_claude_session("%7", self.dir.name)
        self.assertEqual(record["sessionId"], "cccc3333-0000-0000-0000-000000000000")

    def test_unknown_pane_and_broken_json(self):
        self._write("broken.json", {})
        with open(os.path.join(self.dir.name, "bad.json"), "w") as handle:
            handle.write("{not json")
        self.assertIsNone(stats.find_claude_session("%99", self.dir.name))
        self.assertIsNone(stats.find_claude_session("", self.dir.name))

    def test_transcript_path_encodes_cwd(self):
        record = {
            "sessionId": "aaaa1111-0000-0000-0000-000000000000",
            "cwd": "/Users/me/projects/my_app",
        }
        path = stats.claude_transcript_path(record, "/root")
        self.assertEqual(
            path,
            "/root/-Users-me-projects-my-app/aaaa1111-0000-0000-0000-000000000000.jsonl",
        )

    def test_transcript_path_needs_both_fields(self):
        self.assertEqual(stats.claude_transcript_path({"cwd": "/x"}, "/root"), "")
        self.assertEqual(stats.claude_transcript_path({"sessionId": ".."}, "/root"), "")


class ClaudeTranscriptContextTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8",
        )
        self.addCleanup(os.unlink, self.file.name)
        self.addCleanup(self.file.close)

    def _write(self, *messages):
        for message in messages:
            self.file.write(json.dumps(message) + "\n")
        self.file.close()

    def test_sums_input_and_cache_of_the_last_answer(self):
        self._write(
            {"type": "assistant", "message": {"model": "claude-opus-5", "usage": {
                "input_tokens": 1, "cache_read_input_tokens": 10,
            }}},
            {"type": "assistant", "message": {"model": "claude-opus-5", "usage": {
                "input_tokens": 2,
                "cache_read_input_tokens": 79_894,
                "cache_creation_input_tokens": 724,
            }}},
        )
        context = stats.read_claude_transcript_context(self.file.name, 1_000_000)
        self.assertEqual(context.used_tokens, 80_620)
        self.assertEqual(context.percent, 8.1)
        self.assertTrue(context.approximate)

    def test_unknown_window_keeps_tokens_without_percent(self):
        self._write({"type": "assistant", "message": {
            "model": "claude-opus-5", "usage": {"input_tokens": 500},
        }})
        context = stats.read_claude_transcript_context(self.file.name)
        self.assertEqual(context.used_tokens, 500)
        self.assertEqual(context.total_tokens, 0)
        self.assertEqual(context.percent, 0.0)

    def test_session_without_answers_has_no_context(self):
        self._write({"type": "user", "message": {"content": "привет"}})
        self.assertIsNone(stats.read_claude_transcript_context(self.file.name))

    def test_missing_file(self):
        self.assertIsNone(stats.read_claude_transcript_context("/nope.jsonl"))


class RenderTests(unittest.TestCase):
    def _claude(self):
        return stats.SessionStats(
            agent="claude",
            model="Opus 5 (1M context)",
            effort="xhigh",
            context=stats.parse_claude_context(CLAUDE_CONTEXT),
            limits=stats.parse_claude_usage(CLAUDE_USAGE),
        )

    def test_claude_message_has_context_and_every_limit(self):
        text = stats.render_stats(self._claude())
        self.assertIn("Claude Code · Opus 5 (1M context) · xhigh", text)
        self.assertIn("38.5k из 1M", text)
        self.assertIn("Неделя · Fable", text)
        self.assertIn("52%", text)
        self.assertNotIn("Free space", text)

    def test_codex_message_groups_sub_limits(self):
        text = stats.render_stats(stats.parse_codex_status(CODEX_STATUS))
        self.assertIn("Codex · gpt-5.6-sol · medium · Pro Lite", text)
        self.assertIn("_GPT-5.3-Codex-Spark limit_", text)

    def test_approximate_context_is_marked(self):
        session = stats.SessionStats(agent="claude", context=stats.ContextUsage(
            used_tokens=80_620, total_tokens=1_000_000, percent=8.1, approximate=True,
        ))
        self.assertIn("≈8.1%", stats.render_stats(session))

    def test_unknown_window_is_reported_honestly(self):
        session = stats.SessionStats(agent="claude", context=stats.ContextUsage(
            used_tokens=80_620, total_tokens=0, percent=0.0, approximate=True,
        ))
        text = stats.render_stats(session)
        self.assertIn("размер окна не определён", text)
        self.assertNotIn("0% —", text)

    def test_missing_data_and_notes(self):
        session = stats.SessionStats(agent="codex", notes=["нет связи"])
        text = stats.render_stats(session)
        self.assertIn("Контекст: нет данных", text)
        self.assertIn("Лимиты: нет данных", text)
        self.assertIn("нет связи", text)

    def test_message_fits_telegram_limit(self):
        self.assertLess(len(stats.render_stats(self._claude())), 4096)


if __name__ == "__main__":
    unittest.main()
