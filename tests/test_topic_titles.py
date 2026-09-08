import os
import tempfile
import unittest
from unittest.mock import patch

import tg_sessions
import tg_reply_watcher as watcher


class TopicTitleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patcher = patch.object(tg_sessions, 'SESSIONS_FILE', os.path.join(self.tmp.name, 'sessions.json'))
        self.patcher.start()
        tg_sessions.save_topic('%1', 42)
        tg_sessions.save_agent('%1', 'codex')

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_title_is_normalized_and_persisted(self):
        import tg_topic_title
        tg_topic_title.set_title('%1', '  Рефакторинг\n tg-integration  ')
        self.assertEqual(tg_sessions._load()['pane_to_title']['%1'], 'Рефакторинг tg-integration')

    def test_unmapped_pane_cannot_request_rename(self):
        import tg_topic_title
        with self.assertRaises(ValueError):
            tg_topic_title.set_title('%2', 'Название')

    @patch.object(watcher, 'tg_api', return_value={'ok': True})
    def test_sync_renames_once_and_preserves_prefix(self, api):
        import tg_topic_title
        tg_sessions.save_project('%1', '/Users/me/projects')
        tg_topic_title.set_title('%1', 'Рефакторинг tg-integration')
        watcher.sync_topic_titles()
        watcher.sync_topic_titles()
        api.assert_called_once_with('editForumTopic', {
            'chat_id': watcher.CHAT_ID, 'message_thread_id': 42,
            'name': '🚀 Codex · projects · Рефакторинг tg-integration',
        })

    @patch.object(watcher, 'tg_api', side_effect=[{'ok': False}, {'ok': True}])
    def test_failed_edit_is_retried(self, api):
        import tg_topic_title
        tg_sessions.save_project('%1', '/tmp/demo')
        tg_topic_title.set_title('%1', 'Новое название')
        watcher.sync_topic_titles()
        self.assertNotIn('%1', tg_sessions._load().get('pane_to_topic_name', {}))
        watcher.sync_topic_titles()
        self.assertEqual(api.call_count, 2)

    def test_topic_name_respects_telegram_limit(self):
        import tg_topic_title
        name = tg_topic_title.format_topic_name('claude', 'проект' * 40, '🦊' * 100)
        self.assertLessEqual(len(name.encode('utf-16-le')) // 2, 128)
        self.assertTrue(name.startswith('🚀 Claude Code · '))

    def test_cleanup_removes_title_metadata(self):
        import tg_topic_title
        tg_topic_title.set_title('%1', 'Название')
        tg_sessions.save_project('%1', '/tmp/demo')
        tg_sessions.cleanup_pane('%1')
        self.assertNotIn('%1', tg_sessions._load()['pane_to_title'])
        self.assertNotIn('%1', tg_sessions._load()['pane_to_project'])
