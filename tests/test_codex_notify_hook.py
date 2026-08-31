import unittest

import tg_codex_notify_hook


class CodexNotifyHookTests(unittest.TestCase):
    def test_forwards_codex_payload_to_shared_stop_hook(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))

        result = tg_codex_notify_hook.main(
            ["tg_codex_notify_hook.py", '{"last-assistant-message":"done"}'],
            runner=runner,
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            calls[0][1]["input"],
            '{"last_assistant_message":"done"}',
        )
        self.assertEqual(calls[0][0][0], "bash")
        self.assertTrue(calls[0][0][1].endswith("tg_stop_hook.sh"))

    def test_rejects_missing_payload(self):
        self.assertEqual(tg_codex_notify_hook.main(["tg_codex_notify_hook.py"]), 2)


if __name__ == "__main__":
    unittest.main()
