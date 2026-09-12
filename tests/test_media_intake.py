import unittest

import tg_reply_watcher as watcher


class ExtractMediaTests(unittest.TestCase):
    def test_photo_takes_largest_size(self):
        msg = {"photo": [
            {"file_id": "small", "file_size": 100},
            {"file_id": "big", "file_size": 900},
        ]}
        file_id, name, size, label = watcher.extract_media(msg, 7)
        self.assertEqual(file_id, "big")
        self.assertEqual(name, "photo_7.jpg")
        self.assertEqual(size, 900)
        self.assertEqual(label, "Фото")

    def test_video_sent_as_video_is_accepted(self):
        msg = {"video": {
            "file_id": "vid", "mime_type": "video/mp4", "file_size": 5_000_000,
        }}
        file_id, name, size, label = watcher.extract_media(msg, 11)
        self.assertEqual(file_id, "vid")
        self.assertEqual(name, "video_11.mp4")
        self.assertEqual(size, 5_000_000)
        self.assertEqual(label, "Видео")

    def test_video_note_and_animation_and_audio(self):
        for kind, expected_label in (
            ("video_note", "Видеокружок"),
            ("animation", "GIF"),
            ("audio", "Аудио"),
        ):
            with self.subTest(kind=kind):
                file_id, name, _, label = watcher.extract_media(
                    {kind: {"file_id": "x"}}, 3
                )
                self.assertEqual(file_id, "x")
                self.assertEqual(label, expected_label)
                self.assertTrue(name.startswith(f"{kind}_3."))

    def test_document_keeps_original_name(self):
        msg = {"document": {"file_id": "doc", "file_name": "отчёт v2.pdf"}}
        file_id, name, _, label = watcher.extract_media(msg, 1)
        self.assertEqual(file_id, "doc")
        self.assertEqual(name, "отчёт_v2.pdf")
        self.assertEqual(label, "отчёт_v2.pdf")

    def test_document_name_cannot_escape_media_dir(self):
        msg = {"document": {"file_id": "doc", "file_name": "../../../etc/passwd"}}
        _, name, _, _ = watcher.extract_media(msg, 1)
        self.assertEqual(name, "passwd")

    def test_document_without_name_uses_mime_extension(self):
        msg = {"document": {"file_id": "doc", "mime_type": "video/mp4"}}
        _, name, _, _ = watcher.extract_media(msg, 42)
        self.assertEqual(name, "document_42.mp4")

    def test_sticker_and_plain_text_are_not_media(self):
        self.assertEqual(
            watcher.extract_media({"text": "привет"}, 1), (None, None, None, None)
        )
        self.assertEqual(
            watcher.extract_media({"sticker": {"file_id": "s"}}, 1),
            (None, None, None, None),
        )


class DownloadLimitTests(unittest.TestCase):
    def test_limit_matches_bot_api(self):
        self.assertEqual(watcher.TG_DOWNLOAD_LIMIT, 20 * 1024 * 1024)

    def test_oversized_video_is_detected_before_download(self):
        _, _, size, _ = watcher.extract_media(
            {"video": {"file_id": "v", "file_size": 48 * 1024 * 1024}}, 1
        )
        self.assertGreater(size, watcher.TG_DOWNLOAD_LIMIT)


if __name__ == "__main__":
    unittest.main()
