import os
import tempfile
import unittest

import tg_sessions


class SessionAgentPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_sessions_file = tg_sessions.SESSIONS_FILE
        tg_sessions.SESSIONS_FILE = os.path.join(self.temp_dir.name, "sessions.json")

    def tearDown(self):
        tg_sessions.SESSIONS_FILE = self.original_sessions_file
        self.temp_dir.cleanup()

    def test_legacy_session_defaults_to_claude(self):
        tg_sessions._save({"pane_to_topic": {"%1": 42}})

        self.assertEqual(tg_sessions.get_agent("%1"), "claude")

    def test_saves_codex_agent_kind(self):
        tg_sessions.save_agent("%1", "codex")

        self.assertEqual(tg_sessions.get_agent("%1"), "codex")

    def test_cleanup_removes_agent_kind(self):
        tg_sessions.save_agent("%1", "codex")

        tg_sessions.cleanup_pane("%1")

        self.assertEqual(tg_sessions.get_agent("%1"), "claude")


if __name__ == "__main__":
    unittest.main()

class PendingMessageTests(SessionAgentPersistenceTests):
    def test_queue_survives_reload_and_deduplicates_telegram_retry(self):
        tg_sessions.queue_message('%1', 'hello', 7)
        tg_sessions.queue_message('%1', 'hello', 7)
        self.assertEqual(tg_sessions.pending_messages('%1'), [{'text': 'hello', 'message_id': 7}])
        tg_sessions.ack_message('%1', 7)
        self.assertEqual(tg_sessions.pending_messages('%1'), [])

    def test_startup_is_persisted_and_cleaned(self):
        tg_sessions.save_startup('%1', {'project': 'demo'})
        self.assertEqual(tg_sessions._load()['pane_to_startup']['%1']['project'], 'demo')
        tg_sessions.cleanup_pane('%1')
        self.assertNotIn('%1', tg_sessions._load()['pane_to_startup'])
