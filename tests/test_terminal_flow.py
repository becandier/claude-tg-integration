import unittest
from unittest.mock import patch
import agent_runtime as runtime
import tg_reply_watcher as watcher


class TerminalStateTests(unittest.TestCase):
    def test_codex_uses_current_footer_not_old_header(self):
        output = '│ model: gpt-6-astra medium /model to change │\n› Ask Codex to do anything\n  gpt-5.6-sol low · /tmp'
        self.assertEqual(runtime.terminal_model(output, runtime.AgentKind.CODEX), 'gpt-5.6-sol')

    def test_codex_accepts_new_uppercase_model_footer(self):
        output = '› Ask Codex to do anything\n  GPT-6-Sol medium · ~/projects'
        self.assertEqual(runtime.terminal_model(output, runtime.AgentKind.CODEX), 'gpt-6-sol')
        self.assertEqual(runtime.terminal_effort(output, runtime.AgentKind.CODEX), 'medium')
        self.assertTrue(runtime.terminal_ready(output, runtime.AgentKind.CODEX))

    def test_claude_startup_model(self):
        output = '▐▛███▜▌   Claude Code v2.1.260\n▝▜█████▛▘  Fable 5 · Claude Max\n❯ '
        self.assertEqual(runtime.terminal_model(output, runtime.AgentKind.CLAUDE), 'Fable 5')

    def test_menu_selection_is_not_composer(self):
        self.assertFalse(runtime.terminal_ready('Select Model and Effort\n› 1. gpt-6-astra (current)\nPress enter to confirm or esc to go back', runtime.AgentKind.CODEX))

    def test_update_options(self):
        output = 'Update available! 0.1 -> 0.2\n› 1. Update now (runs `npm install`)\n  2. Skip\n  3. Skip until next version\nPress enter to continue'
        options = runtime.parse_codex_update_picker(output)
        self.assertEqual([o.value for o in options], ['1', '2', '3'])
        self.assertTrue(options[0].selected)

    def test_unknown_prompt_is_not_ready(self):
        self.assertFalse(runtime.terminal_ready('Hooks need review\n› 1. Review hooks\n  2. Trust all and continue', runtime.AgentKind.CODEX))

    def test_idle_composer_is_ready(self):
        self.assertTrue(runtime.terminal_ready('› Ask Codex to do anything\n  gpt-6-astra medium · /tmp', runtime.AgentKind.CODEX))

    def test_codex_alternate_composer_is_ready_but_menu_is_not(self):
        self.assertTrue(runtime.terminal_ready('» Ask Codex to do anything\n  GPT-6-Sol ultra · /tmp', runtime.AgentKind.CODEX))
        self.assertFalse(runtime.terminal_ready('Select Model and Effort\n» 1. gpt-6-sol (current)\n  GPT-6-Sol ultra · /tmp', runtime.AgentKind.CODEX))

    def test_working_codex_accepts_messages_but_not_commands(self):
        output = '• Working (5m 46s • esc to interrupt)\n\n› Ask Codex to do anything\n  GPT-6-Sol medium · /tmp'
        self.assertFalse(runtime.terminal_ready(output, runtime.AgentKind.CODEX))
        self.assertTrue(runtime.terminal_ready(output, runtime.AgentKind.CODEX, allow_working=True))


class ModelRecoveryTests(unittest.TestCase):
    @patch.object(watcher, 'pane_exists', return_value=True)
    @patch.object(watcher, '_capture_pane', return_value='Select Model and Effort\n› 1. gpt-6-astra (current)')
    @patch.object(watcher, '_type_command', return_value=False)
    def test_reuses_already_open_model_picker(self, _type, _capture, _exists):
        self.assertIn('gpt-6-astra', watcher._open_codex_model_picker('%1'))

    @patch.object(watcher, '_close_codex_picker')
    @patch.object(watcher, '_open_codex_model_picker', return_value='Select Model and Effort\n› 1. unknown-model')
    def test_unreadable_picker_is_closed(self, _open, close):
        self.assertIsNone(watcher._probe_codex_models('%1'))
        close.assert_called_once_with('%1')


if __name__ == '__main__':
    unittest.main()

class StartupFlowTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import os
        self.tmp = tempfile.TemporaryDirectory()
        self.config_patch = patch.object(watcher.tg_sessions, 'SESSIONS_FILE', os.path.join(self.tmp.name, 'sessions.json'))
        self.config_patch.start()
        watcher.tg_sessions.save_agent('%1', 'codex')
        watcher.tg_sessions.save_topic('%1', 42)

    def tearDown(self):
        self.config_patch.stop()
        self.tmp.cleanup()

    @patch.object(watcher, 'tg_send', return_value={'ok': True})
    @patch.object(watcher, 'pane_exists', return_value=True)
    @patch.object(watcher, '_capture_pane', return_value='Update available!\n› 1. Update now\n  2. Skip')
    @patch.object(watcher, 'tmux_send_codex')
    def test_blocked_message_is_saved_without_sending_to_menu(self, send, *_):
        self.assertFalse(watcher.queue_if_blocked('%1', 'hello', 7, 42))
        self.assertEqual(watcher.tg_sessions.pending_messages('%1')[0]['text'], 'hello')
        send.assert_not_called()

    @patch.object(watcher, 'pane_exists', return_value=True)
    @patch.object(watcher, '_capture_pane', return_value='• Working (2m • esc to interrupt)\n› Ask Codex to do anything\n  GPT-6-Sol medium · /tmp')
    def test_working_codex_routes_new_message_immediately(self, *_):
        self.assertTrue(watcher.queue_if_blocked('%1', 'follow-up', 9, 42))
        self.assertEqual(watcher.tg_sessions.pending_messages('%1'), [])

    @patch.object(watcher, 'tg_send')
    @patch.object(watcher, '_capture_pane', return_value='» Ask Codex to do anything\n  GPT-6-Sol ultra · /tmp')
    def test_alternate_codex_composer_does_not_queue_message(self, *_):
        self.assertTrue(watcher.queue_if_blocked('%1', 'follow-up', 9, 42))
        self.assertEqual(watcher.tg_sessions.pending_messages('%1'), [])

    @patch.object(watcher, 'tg_react')
    @patch.object(watcher, 'mark_pane_waiting')
    @patch.object(watcher, 'tg_send', return_value={'ok': True})
    @patch.object(watcher, 'pane_exists', return_value=True)
    @patch.object(watcher, '_capture_pane', return_value='› Ask Codex to do anything\n  gpt-6-astra medium · /tmp')
    @patch.object(watcher, 'tmux_send_codex', return_value=True)
    def test_ready_drains_one_queued_message(self, send, *_):
        watcher.tg_sessions.queue_message('%1', 'one', 7)
        watcher.tg_sessions.queue_message('%1', 'two', 8)
        watcher.check_starting_sessions()
        send.assert_called_once_with('%1', 'one')
        self.assertEqual(watcher.tg_sessions.pending_messages('%1'), [{'text': 'two', 'message_id': 8}])

    @patch.object(watcher, 'tg_react')
    @patch.object(watcher, 'mark_pane_waiting')
    @patch.object(watcher, 'pane_exists', return_value=True)
    @patch.object(watcher, '_capture_pane', return_value='• Working (2m • esc to interrupt)\n› Ask Codex to do anything\n  GPT-6-Sol medium · /tmp')
    @patch.object(watcher, 'tmux_send_codex', return_value=True)
    def test_working_codex_drains_pending_message(self, send, *_):
        watcher.tg_sessions.queue_message('%1', 'follow-up', 9)
        watcher.check_starting_sessions()
        send.assert_called_once_with('%1', 'follow-up')
        self.assertEqual(watcher.tg_sessions.pending_messages('%1'), [])

    @patch.object(watcher, 'tg_edit', return_value={'ok': True})
    @patch.object(watcher, 'pane_exists', return_value=True)
    @patch.object(watcher, '_capture_pane', return_value='› Ask Codex to do anything\n  gpt-6-astra medium · /tmp')
    def test_ready_notification_contains_model_and_session(self, _capture, _exists, edit):
        watcher.tg_sessions.save_startup('%1', {'session': 'codex-123', 'project': '/tmp', 'message_id': 12, 'started_at': 1})
        watcher.check_starting_sessions()
        self.assertIn('gpt-6-astra', edit.call_args.args[1])
        self.assertIn('codex-123', edit.call_args.args[1])
        self.assertNotIn('%1', watcher.tg_sessions._load()['pane_to_startup'])

class UpdateCallbackTests(unittest.TestCase):
    @patch.object(watcher, 'tg_api')
    @patch.object(watcher, 'tmux_send_keys')
    @patch.object(watcher.tg_sessions, 'get_pane_by_topic', return_value='%1')
    @patch.object(watcher.tg_sessions, 'get_agent', return_value='codex')
    @patch.object(watcher, '_capture_pane', return_value='Update available!\n› 1. Update now\n  2. Skip')
    def test_stale_update_button_never_sends_keys(self, _capture, _agent, _pane, keys, api):
        watcher.handle_callback({'id': 'cb', 'data': 'startup:stale:1', 'message': {'message_thread_id': 42}})
        keys.assert_not_called()
        self.assertIn('устарела', api.call_args.args[1]['text'])

    @patch.object(watcher, 'tg_api')
    @patch.object(watcher, 'tg_edit')
    @patch.object(watcher, 'tmux_send_keys', return_value=True)
    @patch.object(watcher.tg_sessions, 'get_pane_by_topic', return_value='%1')
    @patch.object(watcher.tg_sessions, 'get_agent', return_value='codex')
    @patch.object(watcher, '_capture_pane', return_value='Update available!\n› 1. Update now\n  2. Skip')
    def test_skip_uses_live_cursor(self, capture, _agent, _pane, keys, *_):
        signature = watcher._update_fingerprint(runtime.parse_codex_update_picker(capture.return_value))
        watcher.handle_callback({'id': 'cb', 'data': f'startup:{signature}:2', 'message': {'message_thread_id': 42, 'message_id': 5}})
        self.assertEqual(keys.call_args_list[0].args, ('%1', 'Down'))
        self.assertEqual(keys.call_args_list[1].args, ('%1', 'Enter'))

class TerminalRegressionTests(unittest.TestCase):
    def test_claude_model_with_effort_in_header(self):
        output = '▝▜██████▀  Opus 5 (1M context) with xhigh effort · Claude Max\n❯ '
        self.assertEqual(runtime.terminal_model(output, runtime.AgentKind.CLAUDE), 'Opus 5 (1M context)')

    def test_working_codex_is_not_ready_for_slash_commands(self):
        output = '• Working (5m 46s • esc to interrupt)\n\n› Ask Codex to do anything\n  gpt-6-astra medium · ~'
        self.assertFalse(runtime.terminal_ready(output, runtime.AgentKind.CODEX))

class ModelConfirmationTests(unittest.TestCase):
    @patch.object(watcher, 'tmux_send_keys', return_value=False)
    def test_failed_key_delivery_is_not_reported_as_success(self, _keys):
        options = [runtime.PickerOption('gpt-6-astra', 'gpt-6-astra', True)]
        self.assertFalse(watcher._choose_picker_option('%1', options, 'gpt-6-astra'))

    @patch.object(watcher, '_close_codex_picker')
    @patch.object(watcher, '_finish_codex_selection', return_value=False)
    @patch.object(watcher, '_choose_picker_option', return_value=True)
    @patch.object(watcher, '_wait_for_picker', return_value='Select Reasoning Level\n› 1. Low (default)  Fast')
    @patch.object(watcher.tg_sessions, 'get_effort', return_value='')
    @patch.object(watcher, '_open_codex_model_picker', return_value='Select Model and Effort\n› 1. gpt-6-astra (current)')
    def test_no_success_until_live_footer_confirms_model(self, *_):
        self.assertFalse(watcher._set_codex_model('%1', 'gpt-6-astra'))

class StartupRecoveryTests(StartupFlowTests):
    @patch.object(watcher, 'tg_react')
    @patch.object(watcher, 'mark_pane_waiting')
    @patch.object(watcher, 'tg_send', return_value={'ok': True})
    @patch.object(watcher, 'tg_edit', return_value={'ok': False})
    @patch.object(watcher, 'pane_exists', return_value=True)
    @patch.object(watcher, '_capture_pane', return_value='› Ask Codex to do anything\n  gpt-6-astra medium · /tmp')
    @patch.object(watcher, 'tmux_send_codex', return_value=True)
    def test_deleted_start_notification_does_not_block_delivery(self, send, *_):
        watcher.tg_sessions.save_startup('%1', {'session': 'codex-123', 'project': '/tmp', 'message_id': 12, 'started_at': 1})
        watcher.tg_sessions.queue_message('%1', 'hello', 7)
        watcher.check_starting_sessions()
        self.assertEqual(watcher.tg_sessions.pending_messages('%1'), [])
        self.assertNotIn('%1', watcher.tg_sessions._load()['pane_to_startup'])

    @patch.object(watcher, 'tg_send', return_value={'ok': True})
    @patch.object(watcher, 'pane_exists', return_value=True)
    @patch.object(watcher, '_capture_pane', return_value='Update failed')
    @patch.object(watcher.subprocess, 'run')
    def test_failed_updater_restarts_installed_cli_and_keeps_queue(self, run, *_):
        from unittest.mock import Mock
        run.side_effect = [Mock(returncode=0, stdout='1 1'), Mock(returncode=0), Mock(returncode=0)]
        watcher.tg_sessions.save_startup('%1', {'session': 'codex-123', 'project': '/tmp', 'started_at': 1, 'updating': True})
        watcher.tg_sessions.queue_message('%1', 'hello', 7)
        watcher.check_starting_sessions()
        self.assertNotIn('updating', watcher.tg_sessions._load()['pane_to_startup']['%1'])
        self.assertEqual(watcher.tg_sessions.pending_messages('%1')[0]['text'], 'hello')
