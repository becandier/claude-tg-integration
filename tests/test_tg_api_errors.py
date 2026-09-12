import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import tg_reply_watcher as watcher


def _http_error(code, payload):
    body = json.dumps(payload).encode() if isinstance(payload, dict) else payload
    return urllib.error.HTTPError(
        "https://api.telegram.org", code, "Bad Request", {}, io.BytesIO(body),
    )


class TgApiHttpErrorTests(unittest.TestCase):
    def _error(self, code, payload):
        """HTTPError держит file-like объект — закрываем, чтобы не шуметь в выводе."""
        error = _http_error(code, payload)
        self.addCleanup(error.close)
        return error

    def test_4xx_body_is_returned_so_callers_see_description(self):
        """TOPIC_NOT_MODIFIED отличает «имя уже такое» от реальной ошибки."""
        error = self._error(400, {
            "ok": False, "error_code": 400,
            "description": "Bad Request: TOPIC_NOT_MODIFIED",
        })
        with patch.object(watcher.urllib.request, "urlopen", side_effect=error), \
                patch.object(watcher.time, "sleep") as sleep:
            response = watcher.tg_api("editForumTopic", {"name": "x"})
        self.assertIn("TOPIC_NOT_MODIFIED", response.get("description", ""))
        sleep.assert_not_called()

    def test_4xx_is_not_retried(self):
        error = self._error(400, {"ok": False, "description": "Bad Request: nope"})
        with patch.object(watcher.urllib.request, "urlopen", side_effect=error) as urlopen, \
                patch.object(watcher.time, "sleep"):
            watcher.tg_api("sendMessage", {"text": "x"})
        self.assertEqual(urlopen.call_count, 1)

    def test_non_json_4xx_body_still_reaches_the_caller(self):
        with patch.object(watcher.urllib.request, "urlopen",
                          side_effect=self._error(403, b"<html>forbidden</html>")), \
                patch.object(watcher.time, "sleep"):
            response = watcher.tg_api("sendMessage", {"text": "x"})
        self.assertFalse(response["ok"])
        self.assertIn("forbidden", response["description"])

    def test_5xx_is_retried_and_then_reports_failure(self):
        with patch.object(watcher.urllib.request, "urlopen",
                          side_effect=self._error(502, b"bad gateway")) as urlopen, \
                patch.object(watcher.time, "sleep"):
            response = watcher.tg_api("sendMessage", {"text": "x"})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(response, {"ok": False})


class TopicNotModifiedTests(unittest.TestCase):
    def test_sync_stops_renaming_after_topic_not_modified(self):
        """Иначе watcher переименовывает один и тот же топик в каждом цикле."""
        state = {
            "pane_to_topic": {"%1": 42},
            "pane_to_project": {"%1": "/Users/me/projects"},
            "pane_to_model": {"%1": "Opus 5 (1M context)"},
            "pane_to_agent": {"%1": "claude"},
            "pane_to_title": {"%1": "Тема"},
        }
        saved = {}
        response = {"ok": False, "description": "Bad Request: TOPIC_NOT_MODIFIED"}
        with patch.object(watcher, "pane_exists", return_value=True), \
                patch.object(watcher.tg_sessions, "_load", side_effect=lambda: state), \
                patch.object(watcher.tg_sessions, "save_topic_name",
                             side_effect=lambda pane, name: saved.setdefault(pane, name)), \
                patch.object(watcher, "tg_api", return_value=response) as api:
            watcher.sync_topic_titles()
            state["pane_to_topic_name"] = dict(saved)
            watcher.sync_topic_titles()
        self.assertEqual(api.call_count, 1)
        self.assertEqual(saved["%1"], "🚀 Opus 5 · projects · Тема")

    def test_unchanged_name_costs_no_tmux_or_telegram_call(self):
        """Цикл watcher-а крутится раз в пару секунд, пока сессия стартует."""
        state = {
            "pane_to_topic": {"%1": 42},
            "pane_to_project": {"%1": "/Users/me/projects"},
            "pane_to_model": {"%1": "Opus 5 (1M context)"},
            "pane_to_agent": {"%1": "claude"},
            "pane_to_title": {"%1": "Тема"},
            "pane_to_topic_name": {"%1": "🚀 Opus 5 · projects · Тема"},
        }
        with patch.object(watcher.tg_sessions, "_load", return_value=state), \
                patch.object(watcher, "pane_exists") as pane_exists, \
                patch.object(watcher, "tg_api") as api:
            watcher.sync_topic_titles()
        pane_exists.assert_not_called()
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
