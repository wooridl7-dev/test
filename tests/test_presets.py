"""Unit tests for core.presets (shared GUI/CLI menu values)."""

from __future__ import annotations

import os
import unittest

from core import presets


class PresetsTest(unittest.TestCase):
    def test_formats_prefer_h264_aac_and_fall_back(self) -> None:
        self.assertTrue(presets.format_for("2").startswith("bestvideo[height<=1080][vcodec^=avc1]"))
        self.assertEqual(presets.format_for("9"), presets.format_for("1"))

    def test_audio_only_for_choices_5_and_6(self) -> None:
        self.assertEqual(presets.audio_opts_for("5"), {"format": "mp3"})
        self.assertEqual(presets.audio_opts_for("6"), {"format": "m4a"})
        for choice in ("1", "4", "9"):
            self.assertIsNone(presets.audio_opts_for(choice))

    def test_audio_lang_choices(self) -> None:
        self.assertEqual(presets.audio_lang_for("1"), "ko")
        self.assertIsNone(presets.audio_lang_for("2"))
        self.assertEqual(presets.audio_lang_for("3"), "en")
        self.assertEqual(presets.audio_lang_for("4"), "ja")
        self.assertEqual(presets.audio_lang_for("9"), "ko")

    KO = "[language^=ko][language!$=-desc]"

    def test_prefer_audio_language_puts_language_copies_first(self) -> None:
        ko = self.KO
        self.assertEqual(
            presets.prefer_audio_language(presets.format_for("1"), "ko"),
            f"bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]{ko}"
            # Dub only as Opus: keep H.264 video, drop only the audio's ext.
            f"/bestvideo[vcodec^=avc1]+bestaudio{ko}"
            f"/bestvideo+bestaudio{ko}"
            "/bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        )
        self.assertEqual(
            presets.prefer_audio_language(presets.format_for("5"), "ko"),
            f"bestaudio[ext=m4a]{ko}/bestaudio{ko}/bestaudio[ext=m4a]/bestaudio/best",
        )
        self.assertEqual(
            presets.prefer_audio_language("bv*+ba/b", "en-US"),
            "bv*+ba[language^=en-US][language!$=-desc]/bv*+ba/b",
        )

    def test_prefer_audio_language_keeps_original_chain_as_fallback(self) -> None:
        for choice in ("1", "2", "3", "4", "5", "6"):
            fmt = presets.format_for(choice)
            self.assertTrue(presets.prefer_audio_language(fmt, "ja").endswith("/" + fmt))

    def test_prefer_audio_language_none_is_unchanged(self) -> None:
        fmt = presets.format_for("2")
        self.assertEqual(presets.prefer_audio_language(fmt, None), fmt)
        self.assertEqual(presets.prefer_audio_language(fmt, ""), fmt)
        self.assertEqual(presets.prefer_audio_language("best", "ko"), "best")

    def test_prefer_audio_language_leaves_other_syntax_alone(self) -> None:
        for spec in (
            "(bv+ba/b)[height<=720]",   # group: "/" split would break it
            "bv,ba",                    # several downloads
            "bv+ba*", "bv+ba.2",        # not a plain audio selector
            "b[format_note*=basic]",    # "ba" inside a filter value
        ):
            self.assertEqual(presets.prefer_audio_language(spec, "ko"), spec)

    def test_prefer_audio_language_rejects_bad_codes(self) -> None:
        for bad in ("k", "ko]", "ko/best", "한국어", "ko x"):
            with self.assertRaises(ValueError):
                presets.prefer_audio_language("bestaudio", bad)

    def test_language_only_copy_relaxes_just_the_ext_filter(self) -> None:
        self.assertEqual(
            presets.prefer_audio_language("bv+ba[abr<=64][ext=m4a]/b", "ko"),
            f"bv+ba[abr<=64][ext=m4a]{self.KO}/bv+ba[abr<=64]{self.KO}"
            "/bv+ba[abr<=64][ext=m4a]/b",
        )

    def test_normalize_audio_lang(self) -> None:
        self.assertIsNone(presets.normalize_audio_lang(None))
        self.assertIsNone(presets.normalize_audio_lang(" "))
        self.assertEqual(presets.normalize_audio_lang(" KO "), "ko")
        # yt-dlp reports codes like en-US / zh-Hans and compares exactly.
        self.assertEqual(presets.normalize_audio_lang("EN-us"), "en-US")
        self.assertEqual(presets.normalize_audio_lang("zh-hans"), "zh-Hans")
        self.assertEqual(presets.normalize_audio_lang("es-419"), "es-419")

    def test_subtitles(self) -> None:
        self.assertEqual(presets.subtitle_opts_for("1"), {})
        self.assertEqual(
            presets.subtitle_opts_for("2"),
            {"writesubtitles": True, "subtitleslangs": ["ko"]},
        )
        self.assertEqual(
            presets.subtitle_opts_for("5"),
            {"writesubtitles": True, "writeautomaticsub": True, "subtitleslangs": ["ko", "en"]},
        )

    def test_subtitle_dicts_are_fresh(self) -> None:
        first = presets.subtitle_opts_for("5")
        first["subtitleslangs"].append("ja")
        self.assertEqual(presets.subtitle_opts_for("5")["subtitleslangs"], ["ko", "en"])

    def test_output_template(self) -> None:
        self.assertEqual(
            presets.output_template_for("out", "4"),
            os.path.join("out", "%(title)s (%(id)s).%(ext)s"),
        )
        self.assertEqual(
            presets.output_template_for("out", "9"),
            presets.output_template_for("out", "1"),
        )


if __name__ == "__main__":
    unittest.main()
