import tempfile
import unittest

import tg_hook_dedupe


class HookDedupeTests(unittest.TestCase):
    def test_native_and_legacy_payloads_share_one_turn_claim(self):
        native = {
            "hook_event_name": "Stop",
            "turn_id": "turn-123",
        }
        legacy = {
            "type": "agent-turn-complete",
            "turn-id": "turn-123",
        }

        with tempfile.TemporaryDirectory() as marker_dir:
            first = tg_hook_dedupe.claim(native, marker_dir=marker_dir)
            second = tg_hook_dedupe.claim(legacy, marker_dir=marker_dir)

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_payload_without_turn_id_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as marker_dir:
            self.assertEqual(
                tg_hook_dedupe.claim({}, marker_dir=marker_dir),
                "",
            )
