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
        # sync_topic_titles не переименовывает топики мёртвых сессий
        self.pane_patcher = patch.object(watcher, 'pane_exists', return_value=True)
        self.pane_patcher.start()

    def tearDown(self):
        self.pane_patcher.stop()
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

    def test_topic_name_respects_limit_with_a_model(self):
        import tg_topic_title
        name = tg_topic_title.format_topic_name(
            'claude', 'проект' * 40, '🦊' * 100, 'Opus 5 (1M context)',
        )
        self.assertLessEqual(len(name.encode('utf-16-le')) // 2, 128)
        self.assertTrue(name.startswith('🚀 Opus 5 · '))

    def test_sync_uses_the_session_model_instead_of_the_agent_kind(self):
        import tg_topic_title
        tg_sessions.save_project('%1', '/Users/me/projects')
        tg_sessions.save_model('%1', 'gpt-5.6-sol')
        tg_topic_title.set_title('%1', 'Разбор лимитов')
        with patch.object(watcher, 'tg_api', return_value={'ok': True}) as api:
            watcher.sync_topic_titles()
        self.assertEqual(
            api.call_args[0][1]['name'],
            '🚀 gpt-5.6-sol · projects · Разбор лимитов',
        )

    def test_sync_names_a_topic_before_the_agent_picks_a_title(self):
        """Модель известна сразу после старта — название не должно ждать темы."""
        tg_sessions.save_project('%1', '/Users/me/projects')
        tg_sessions.save_model('%1', 'Opus 5 (1M context)')
        with patch.object(watcher, 'tg_api', return_value={'ok': True}) as api:
            watcher.sync_topic_titles()
        self.assertEqual(api.call_args[0][1]['name'], '🚀 Opus 5 · projects')

    def test_sync_skips_topics_of_dead_panes(self):
        import tg_topic_title
        tg_sessions.save_project('%1', '/Users/me/projects')
        tg_topic_title.set_title('%1', 'Тема')
        with patch.object(watcher, 'pane_exists', return_value=False), \
                patch.object(watcher, 'tg_api') as api:
            watcher.sync_topic_titles()
        api.assert_not_called()

    def test_cleanup_removes_title_metadata(self):
        import tg_topic_title
        tg_topic_title.set_title('%1', 'Название')
        tg_sessions.save_project('%1', '/tmp/demo')
        tg_sessions.cleanup_pane('%1')
        self.assertNotIn('%1', tg_sessions._load()['pane_to_title'])
        self.assertNotIn('%1', tg_sessions._load()['pane_to_project'])


class ModelLabelTests(unittest.TestCase):
    def test_display_name_loses_the_context_parenthetical(self):
        import tg_topic_title
        self.assertEqual(
            tg_topic_title.model_label('Opus 5 (1M context)'), 'Opus 5',
        )
        self.assertEqual(tg_topic_title.model_label('Fable 5.1'), 'Fable 5.1')

    def test_lowercase_alias_from_model_command_is_capitalised(self):
        import tg_topic_title
        for alias, expected in (
            ('opus', 'Opus'), ('sonnet', 'Sonnet'),
            ('haiku', 'Haiku'), ('fable', 'Fable'),
        ):
            self.assertEqual(tg_topic_title.model_label(alias), expected)

    def test_codex_model_name_is_kept_as_is(self):
        import tg_topic_title
        self.assertEqual(
            tg_topic_title.model_label('gpt-5.6-sol'), 'gpt-5.6-sol',
        )

    def test_empty_parts_do_not_leave_a_dangling_separator(self):
        import tg_topic_title
        self.assertEqual(
            tg_topic_title.format_topic_name('claude', '', '', ''),
            '🚀 Claude Code',
        )
        self.assertEqual(
            tg_topic_title.format_topic_name('codex', '', 'Тема', 'gpt-5.6-sol'),
            '🚀 gpt-5.6-sol · Тема',
        )

    def test_unknown_or_default_model_falls_back_to_the_agent_label(self):
        import tg_topic_title
        for model in ('', None, 'default', '   '):
            self.assertEqual(tg_topic_title.model_label(model), '')
            self.assertEqual(
                tg_topic_title.format_topic_name('codex', 'projects', 'Тема', model),
                '🚀 Codex · projects · Тема',
            )
