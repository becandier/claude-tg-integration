import unittest
from unittest.mock import patch
import agent_runtime as runtime
import tg_reply_watcher as watcher

class ModelEffortDisplayTests(unittest.TestCase):
    def test_reads_current_effort_from_footer_not_welcome(self):
        output = '│ model: gpt-6-astra high /model to change │\n› Ask Codex to do anything\n gpt-5.6-sol medium · /tmp'
        self.assertEqual(runtime.terminal_effort(output, runtime.AgentKind.CODEX), 'medium')

    def test_model_text_includes_effort(self):
        self.assertIn('medium', watcher._model_text('gpt-5.6-sol', 'medium'))

    @patch.object(watcher, 'tg_send')
    @patch.object(watcher.tg_sessions, 'save_effort')
    @patch.object(watcher.tg_sessions, 'save_model')
    @patch.object(watcher.tg_sessions, 'get_model', return_value='gpt-5.6-sol')
    @patch.object(watcher.tg_sessions, 'get_agent', return_value='codex')
    @patch.object(watcher.tg_sessions, 'get_pane_by_topic', return_value='%1')
    @patch.object(watcher, '_capture_pane', return_value='› Ask Codex to do anything\n gpt-5.6-sol medium · /tmp')
    @patch.object(watcher, 'probe_models', return_value=[{'alias':'gpt-5.6-sol','label':'gpt-5.6-sol','desc':'','current':True}])
    def test_model_menu_shows_live_effort_and_selection_buttons(self, *mocks):
        send=mocks[-1]
        watcher.handle_model({'message_thread_id':42}, '/model')
        self.assertIn('medium',send.call_args.args[0])
        buttons=send.call_args.kwargs['reply_markup']['inline_keyboard']
        self.assertTrue(any(b['callback_data']=='effort:medium' and '✅' in b['text'] for row in buttons for b in row))

    def test_effort_button_keeps_model_menu_and_shows_both_settings(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for target, value in [
                ('pane_exists', True), ('set_session_effort', True),
                ('_capture_pane', '› Ask Codex to do anything\n gpt-5.6-sol medium · /tmp'),
                ('tg_api', {'ok':True}),
            ]:
                stack.enter_context(patch.object(watcher,target,return_value=value))
            stack.enter_context(patch.object(watcher.tg_sessions,'get_pane_by_topic',return_value='%1'))
            stack.enter_context(patch.object(watcher.tg_sessions,'get_agent',return_value='codex'))
            stack.enter_context(patch.object(watcher.tg_sessions,'save_effort'))
            edit=stack.enter_context(patch.object(watcher,'tg_edit'))
            watcher.handle_callback({'id':'cb','data':'effort:medium','message':{
                'message_thread_id':42,'message_id':5,'reply_markup':{'inline_keyboard':[
                    [{'text':'✅ gpt-5.6-sol','callback_data':'model:gpt-5.6-sol'}],
                    [{'text':'✅ Low','callback_data':'effort:low'}],
                ]},
            }})
            self.assertIn('gpt-5.6-sol',edit.call_args.args[1])
            self.assertIn('medium',edit.call_args.args[1])
            buttons=[b for row in edit.call_args.kwargs['reply_markup']['inline_keyboard'] for b in row]
            self.assertTrue(any(b['callback_data']=='model:gpt-5.6-sol' for b in buttons))
            self.assertTrue(any(b['callback_data']=='effort:medium' and '✅' in b['text'] for b in buttons))
            self.assertFalse(any(b['callback_data']=='effort:low' and '✅' in b['text'] for b in buttons))
