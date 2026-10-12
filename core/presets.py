"""Download presets shared by the GUI and CLI menus.

Both interfaces number their choices "1", "2", ... and used to carry
their own copies of these tables. Menu labels stay in each interface
(they are worded differently); only the yt-dlp values live here.
Unknown choices fall back to "1".
"""

from __future__ import annotations

import os
import re
from typing import Optional

__all__ = [
    "format_for",
    "audio_opts_for",
    "audio_lang_for",
    "normalize_audio_lang",
    "prefer_audio_language",
    "subtitle_opts_for",
    "output_template_for",
]

# H.264 (avc1) + AAC (m4a) preference: YouTube applies stricter SABR /
# second-stage throttling to AV1 (vcodec=av01) and Opus (251.webm) even
# with a valid PO Token, causing late-stage truncation around the 90%
# mark. The double-fallback chain keeps us downloading on the rare
# videos that lack H.264/AAC tracks.
_FORMATS = {
    "1": "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    "2": "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "3": "bestvideo[height<=720][vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]",
    "4": "bestvideo[height<=480][vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]",
    "5": "bestaudio[ext=m4a]/bestaudio/best",
    "6": "bestaudio[ext=m4a]/bestaudio/best",
}

# Quality choices that extract audio, and the target codec.
_AUDIO_FORMATS = {"5": "mp3", "6": "m4a"}

# Preferred audio-track language per menu choice. None keeps yt-dlp's
# own order (original track, then YouTube's default track), which picks
# the English original of a video that also has a Korean dub.
_AUDIO_LANGS: dict[str, Optional[str]] = {"1": "ko", "2": None, "3": "en", "4": "ja"}

# A whole audio selector inside one "/"-alternative: "bestaudio" or "ba"
# (not "ba*" / "ba.2"), then its filters (group 2), between "+" joins.
_AUDIO_SELECTOR_RE = re.compile(r"(?:^|(?<=\+))(bestaudio|ba)((?:\[[^\]]*\])*)(?=\+|$)")
# An [ext...] filter with any operator: [ext=m4a], [ext!=webm], [ext^=m4].
_EXT_FILTER_RE = re.compile(r"\[\s*ext\s*!?\s*[\^$*~]?=[^\]]*\]")
_LANG_CODE_RE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*")

# (subtitleslangs, include auto-generated). "1" = no subtitles.
_SUBTITLES = {
    "2": (["ko"], False),
    "3": (["en"], False),
    "4": (["all"], False),
    "5": (["ko", "en"], True),
}

_FILENAME_TEMPLATES = {
    "1": "%(title)s.%(ext)s",
    "2": "%(channel)s - %(title)s.%(ext)s",
    "3": "%(upload_date)s - %(title)s.%(ext)s",
    "4": "%(title)s (%(id)s).%(ext)s",
}


def format_for(quality_choice: str) -> str:
    """yt-dlp ``-f`` selector for a quality menu choice."""
    return _FORMATS.get(quality_choice, _FORMATS["1"])


def audio_opts_for(quality_choice: str) -> Optional[dict]:
    """``DownloadOptions.audio_opts`` for a quality choice, or ``None``."""
    audio_format = _AUDIO_FORMATS.get(quality_choice)
    return {"format": audio_format} if audio_format else None


def audio_lang_for(audio_choice: str) -> Optional[str]:
    """Preferred audio language for an audio-track menu choice.

    ``None`` means "original track" (yt-dlp's default order).
    """
    return _AUDIO_LANGS.get(audio_choice, _AUDIO_LANGS["1"])


def normalize_audio_lang(lang: Optional[str]) -> Optional[str]:
    """Validate an audio language code; ``None`` / ``""`` mean original.

    Returns the code in the case yt-dlp reports, which it compares
    case-sensitively: language lower (``"KO"`` -> ``"ko"``), two-letter
    region upper and four-letter script title case (``"en-us"`` ->
    ``"en-US"``, ``"zh-hans"`` -> ``"zh-Hans"``). Raises ``ValueError``
    for anything else.
    """
    if not lang or not lang.strip():
        return None
    lang = lang.strip()
    if not _LANG_CODE_RE.fullmatch(lang):
        raise ValueError(f"invalid audio language code: {lang!r}")
    primary, *subtags = lang.split("-")
    cased = [
        tag.upper() if len(tag) == 2 and tag.isalpha()
        else tag.title() if len(tag) == 4 and tag.isalpha()
        else tag
        for tag in subtags
    ]
    return "-".join([primary.lower(), *cased])


def prefer_audio_language(format_opt: str, lang: Optional[str]) -> str:
    """Make *format_opt* pick the *lang* audio track when the video has one.

    For every ``/``-separated alternative that selects audio, two copies
    go in front of the unchanged chain: one with ``[language^=<lang>]``
    added to the audio selector, then one without the audio's ``[ext=…]``
    filter, so a dub that exists only as Opus still comes with the
    preferred (H.264) video. Audio-description tracks (``ko-desc``) are
    left out. The original chain follows as the fallback, so a video
    without that language downloads exactly as before. ``^=`` also
    matches regional codes such as ``en-US``.

    *format_opt* comes back unchanged when *lang* is ``None`` / ``""``,
    and for specs with groups or several downloads (``(``, ``,``), which
    this plain rewrite cannot handle.
    """
    lang = normalize_audio_lang(lang)
    if not lang or "(" in format_opt or "," in format_opt:
        return format_opt
    lang_filter = f"[language^={lang}][language!$=-desc]"

    def any_ext(match: re.Match) -> str:
        filters = _EXT_FILTER_RE.sub("", match.group(2))
        return match.group(1) + filters + lang_filter

    alternatives = format_opt.split("/")
    preferred: list[str] = []
    for alt in alternatives:
        if not _AUDIO_SELECTOR_RE.search(alt):
            continue
        with_filters = _AUDIO_SELECTOR_RE.sub(lambda m: m.group(0) + lang_filter, alt)
        for copy in (with_filters, _AUDIO_SELECTOR_RE.sub(any_ext, alt)):
            if copy not in preferred:
                preferred.append(copy)
    return "/".join(preferred + alternatives)


def subtitle_opts_for(subtitle_choice: str) -> dict:
    """Fresh ``DownloadOptions.subtitle_opts`` dict for a subtitle choice."""
    entry = _SUBTITLES.get(subtitle_choice)
    if entry is None:
        return {}
    langs, auto = entry
    opts: dict = {"writesubtitles": True}
    if auto:
        opts["writeautomaticsub"] = True
    opts["subtitleslangs"] = list(langs)
    return opts


def output_template_for(output_dir: str, filename_choice: str) -> str:
    """Full yt-dlp ``-o`` template: *output_dir* joined with the name."""
    template = _FILENAME_TEMPLATES.get(filename_choice, _FILENAME_TEMPLATES["1"])
    # os.path.join so we inherit the host separator on each platform.
    return os.path.join(output_dir, template)
