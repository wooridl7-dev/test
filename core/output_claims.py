"""Keep downloads that run at the same time from writing the same file.

yt-dlp names its output after the video (``%(title)s.%(ext)s``). Two
*different* videos with one title, started together, would then both write
``<title>.f137.mp4.part`` and friends: the second resumes the first's
half-file or overwrites it, and both end up broken or mixed.

:func:`core.downloader.run_download` knows the video's ``id`` / ``title`` /
``channel`` from its live probe before yt-dlp starts, so it can work out the
file name in advance (:func:`output_key`) and register it here. A run whose
name is already taken by another running download is renamed
(``<title> (<id>).mp4``, :func:`with_video_id`); one that is the *same video*
as the running download is refused, since there is nothing safe to rename.

The name is only worked out from the probe's fields, not by yt-dlp itself,
so this is a guard against the common case (same title), not a proof: names
that yt-dlp would make equal through its own character replacement, a probe
that failed, or a run with the probe turned off are not seen.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Optional

__all__ = [
    "OUTPUT_CLAIMS",
    "OutputClaim",
    "OutputClaims",
    "Reservation",
    "output_key",
    "reserve_output",
    "with_video_id",
]

# One piece of a yt-dlp output template: "%%" or "%(field...)s" (any
# conversion character and printf flags).
_TOKEN_RE = re.compile(r"%%|%\(([^)]*)\)([-#0 +,.\d]*)([a-zA-Z])")
_PRECISION_RE = re.compile(r"\.(\d+)")
_FIELD_NAME_RE = re.compile(r"[A-Za-z_]\w*")
# Characters Windows file names cannot hold; yt-dlp replaces or drops them,
# so two titles that differ only there can end up as one name.
_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SPACES_RE = re.compile(r"\s+")


def _comparable(value: Any) -> str:
    text = _UNSAFE_RE.sub("", str(value))
    return _SPACES_RE.sub(" ", text).strip().rstrip(". ").casefold()


def output_key(template: str, info: Mapping[str, Any]) -> str:
    """Identity of the file *template* gives the video described by *info*
    (the ``--dump-json`` fields): equal keys mean the same output path.

    The extension is left out, so an audio-only and a video run of one name
    count as the same, which they are for the ``.part`` files. A field that
    *info* lacks counts as ``"NA"``, as in yt-dlp. A bound such as
    ``%(title).150B`` (see :mod:`core.filename_limit`) is applied, so two long
    titles that only differ after the cut have the same key.
    """

    def expand(match: re.Match) -> str:
        if match.group(0) == "%%":
            return "%"
        name = _FIELD_NAME_RE.match(match.group(1) or "")
        if name is None:  # "%(.{id,title})j" and other forms: not a plain field
            return "NA"
        if name.group(0) == "ext":
            return ""
        value = info.get(name.group(0))
        if value in (None, ""):
            return "NA"
        # "%(title).150B" keeps 150 bytes, "%(title).20s" 20 characters (cut before
        # the name is made safe, as yt-dlp does).
        precision = _PRECISION_RE.fullmatch(match.group(2))
        if precision:
            size = int(precision.group(1))
            if match.group(3) == "B":
                value = str(value).encode("utf-8")[:size].decode("utf-8", "ignore")
            elif match.group(3) == "s":
                value = str(value)[:size]
        return _comparable(value)

    expanded = _TOKEN_RE.sub(expand, template)
    return os.path.normcase(os.path.abspath(expanded)).casefold()


def with_video_id(template: str) -> Optional[str]:
    """*template* with `` (<id>)`` before the extension (``%(title)s (%(id)s).%(ext)s``);
    ``None`` when its name already holds the id, so there is nothing to add."""
    head, tail = os.path.split(template)
    if "%(id)" in tail:
        return None
    marker = ".%(ext)s"
    at = tail.rfind(marker)
    tail = tail + " (%(id)s)" if at < 0 else tail[:at] + " (%(id)s)" + tail[at:]
    return os.path.join(head, tail)


@dataclass(frozen=True)
class OutputClaim:
    key: str
    video_id: str = ""
    label: str = ""  # what the holder is downloading (its URL), for messages


class OutputClaims:
    """The output names of the downloads running now. Thread-safe; one
    download is one *owner* (any hashable object)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # owner -> its claim, or None while it has not worked out its name
        self._held: dict[object, Optional[OutputClaim]] = {}

    def enter(self, owner: object) -> None:
        """Count *owner* as running, without a name yet."""
        with self._lock:
            self._held.setdefault(owner, None)

    def leave(self, owner: object) -> None:
        """*owner* ended: its name is free again."""
        with self._lock:
            self._held.pop(owner, None)

    def others_running(self, owner: object) -> int:
        """How many downloads other than *owner* are running."""
        with self._lock:
            return sum(1 for other in self._held if other is not owner)

    def claim(self, owner: object, claim: OutputClaim) -> Optional[OutputClaim]:
        """Take ``claim.key`` for *owner*, replacing its earlier claim.

        Returns ``None`` when it is now held, or the other owner's claim
        when that one already has the name (nothing then changes)."""
        with self._lock:
            for other, held in self._held.items():
                if other is not owner and held is not None and held.key == claim.key:
                    return held
            self._held[owner] = claim
            return None


OUTPUT_CLAIMS = OutputClaims()


@dataclass(frozen=True)
class Reservation:
    """What :func:`reserve_output` decided for one download.

    Attributes:
        template: The ``-o`` template to use.
        renamed: *template* is the id-suffixed form of the one asked for.
        busy: Set when the download must not start: the claim it collides
            with. ``same_video`` then says whether it is the same video.
    """

    template: str
    renamed: bool = False
    busy: Optional[OutputClaim] = None
    same_video: bool = False


def reserve_output(
    claims: OutputClaims,
    owner: object,
    template: str,
    info: Mapping[str, Any],
    label: str = "",
) -> Reservation:
    """Claim the file name *template* gives the video *info* for *owner*,
    or the id-suffixed name when another running download has it."""
    video_id = str(info.get("id") or "")
    taken = claims.claim(owner, OutputClaim(output_key(template, info), video_id, label))
    if taken is None:
        return Reservation(template)
    if video_id and taken.video_id == video_id:
        return Reservation(template, busy=taken, same_video=True)
    renamed = with_video_id(template)
    if renamed is None:
        return Reservation(template, busy=taken)
    retaken = claims.claim(owner, OutputClaim(output_key(renamed, info), video_id, label))
    if retaken is not None:
        return Reservation(template, busy=retaken)
    return Reservation(renamed, renamed=True)
