"""Keep output file names inside the file system's limit.

Linux (ext4, btrfs, xfs, ...) allows 255 *bytes* in one file name, and an
encrypted home (eCryptfs) only 143. Korean, Japanese and emoji take 3-4 bytes a
character, so a 98-character Korean title is 248 bytes and the temporary
``<title>.f299.mp4.part`` yt-dlp writes while downloading is 262::

    ERROR: unable to open for writing: [Errno 36] File name too long: '...'

yt-dlp does not shorten names by default, and ``--trim-filenames`` counts
characters, not bytes. What does count bytes is the ``B`` conversion of an
output template: ``%(title).150B`` keeps the first 150 bytes of the title.
:func:`fit_template` puts that bound on ``%(title)s`` when the name would not
fit, computed from the limit of the folder being written to.

Windows counts characters (and the whole path), so nothing is changed there.

The room left for the name is the limit minus ``RESERVE_BYTES``: what yt-dlp
adds after the name while downloading (``.f251-drc.webm.part-Frag12345``, up to
about 36 bytes), what the partial-recording merge adds
(`` (부분 녹화).mp4.0.tmp``, 28), and some slack because yt-dlp replaces
characters such as ``"``, ``|`` and ``/`` by 3-byte full-width look-alikes
after the title was cut.

Pure logic: no tkinter, no ``print()``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

__all__ = [
    "MIN_TITLE_BYTES",
    "OTHER_FIELD_CAP",
    "RESERVE_BYTES",
    "Fitted",
    "fit_template",
    "name_max_for",
]

# Bytes kept free after the name (see the module docstring).
RESERVE_BYTES = 46
# The title is never cut below this, however little room the rest leaves.
MIN_TITLE_BYTES = 24
# Channel / uploader names are bounded to this many bytes when bounds are applied.
OTHER_FIELD_CAP = 120
DEFAULT_NAME_MAX = 255

# "%%" or "%(field...)<flags><conversion>" in a yt-dlp output template.
_TOKEN = re.compile(r"%%|%\((?P<name>[^)]*)\)(?P<flags>[-#0 +,.\d]*)(?P<conv>[a-zA-Z])")
_PLAIN_FIELD = re.compile(r"[A-Za-z_]\w*")
_PRECISION = re.compile(r"\.(\d+)")
# What a field takes when the video's info is not known (bytes).
_STATIC_BYTES = {"ext": 4, "id": 24, "upload_date": 8, "release_date": 8}
_STATIC_DEFAULT = 30
_CAPPED_FIELDS = ("channel", "uploader")


def name_max_for(directory: str) -> int:
    """The longest file name (bytes) *directory* takes, from the file system;
    255 when that cannot be asked. A folder that does not exist yet is asked
    through its nearest existing parent."""
    path = os.path.abspath(directory or ".")
    while True:
        try:
            return max(1, min(int(os.pathconf(path, "PC_NAME_MAX")), DEFAULT_NAME_MAX))
        except (AttributeError, ValueError):  # no os.pathconf (Windows)
            return DEFAULT_NAME_MAX
        except OSError:
            parent = os.path.dirname(path)
            if parent == path:
                return DEFAULT_NAME_MAX
            path = parent


def _limits_in_bytes() -> bool:
    """File name limits count bytes here (every system but Windows)."""
    return os.name != "nt"


def _char_bytes(char: str) -> int:
    """Bytes *char* takes in a file name. yt-dlp (2026.08.19, checked with
    ``prepare_filename``) turns ``" * : < > ? | / \\`` into full-width
    look-alikes (``＂ ： ？ ｜ ⧸``, 3 bytes each), a newline into a space and
    drops other control characters."""
    if char in '"*:<>?|/\\':
        return 3
    if char == "\n":
        return 1
    if ord(char) < 32 or ord(char) == 127:
        return 0
    return len(char.encode("utf-8"))


def _name_bytes(value: Any) -> int:
    """Bytes *value* takes in a file name once yt-dlp has made it safe."""
    return sum(_char_bytes(char) for char in str(value))


def _raw_bound(title: str, room: int) -> int:
    """The most bytes of the *title* as it is (what ``%(title).NB`` counts)
    whose file-name form is at most *room* bytes. ``B`` cuts before the name
    is made safe, so a title full of ``|`` or ``"`` grows afterwards."""
    raw = made = 0
    for char in title:
        made += _char_bytes(char)
        if made > room:
            break
        raw += len(char.encode("utf-8"))
    return raw


@dataclass(frozen=True)
class Fitted:
    """What :func:`fit_template` made of a template.

    Attributes:
        template: The template to use (the one given when nothing had to change).
        name_max: The file name limit it was fitted to; ``0`` when not checked
            (Windows).
        title_bound: Bytes the title was bounded to; ``None`` when it was not.
        cut: The video's title is longer than that bound, so the name will show
            a shortened title (only known when the video's info was given).
    """

    template: str
    name_max: int = 0
    title_bound: Optional[int] = None
    cut: bool = False


def fit_template(
    template: str,
    info: Optional[Mapping[str, Any]] = None,
    name_max: Optional[int] = None,
) -> Fitted:
    """*template* with ``%(title)s`` bounded (``%(title).150B``) so the file
    name stays within the limit of the folder it is written to.

    With *info* (the video's ``--dump-json`` fields) the real sizes of the
    other parts of the name are used and the template is left alone when the
    name fits. Without it the title is always bounded, assuming typical sizes
    for the other parts. A bound already in the template is kept when it is
    tighter, so fitting twice (after the name got `` (<id>)`` added) is safe.
    *name_max* is the limit in bytes (default: asked from the file system).
    """
    if not _limits_in_bytes():
        return Fitted(template)
    head, tail = os.path.split(template)
    limit = name_max if name_max is not None else name_max_for(head)
    known = bool(info)

    literal = 0
    others = 0
    title: Optional[re.Match] = None
    title_existing: Optional[int] = None
    capped: list[re.Match] = []
    position = 0
    for match in _TOKEN.finditer(tail):
        literal += len(tail[position:match.start()].encode("utf-8"))
        position = match.end()
        if match.group(0) == "%%":
            literal += 1
            continue
        inner, flags, conv = match.group("name"), match.group("flags"), match.group("conv")
        plain = _PLAIN_FIELD.fullmatch(inner) is not None
        precision = _PRECISION.fullmatch(flags)
        bound = int(precision.group(1)) if precision and conv == "B" else None
        if plain and inner == "title" and title is None and conv in "sB" and (
            flags == "" or bound is not None
        ):
            title, title_existing = match, bound
            continue
        if plain and known and info.get(inner) not in (None, ""):
            size = _name_bytes(info[inner])
            if inner == "ext":
                size = max(size, _STATIC_BYTES["ext"])
        else:
            size = _STATIC_BYTES.get(inner, _STATIC_DEFAULT) if plain else _STATIC_DEFAULT
        if plain and inner in _CAPPED_FIELDS and conv == "s" and flags == "":
            capped.append(match)
            size = min(size, OTHER_FIELD_CAP)
        elif bound is not None:
            size = min(size, bound)
        others += size
    literal += len(tail[position:].encode("utf-8"))
    if title is None:
        return Fitted(template, limit)

    room = limit - RESERVE_BYTES - literal - others
    title_bytes = _name_bytes(info.get("title", "")) if known else None
    if title_existing is not None and title_bytes is not None:
        title_bytes = min(title_bytes, title_existing)
    if title_bytes is not None and title_bytes <= room:
        return Fitted(template, limit, title_existing)  # fits as it is

    if known and title_bytes is not None:
        # Exact: the longest prefix whose name form fits (the title that
        # yt-dlp will see, with a bound already in the template applied).
        bound = _raw_bound(str(info.get("title", "")), room)
    else:
        bound = room  # unknown title: the room, as if it had no special characters
    bound = max(MIN_TITLE_BYTES, bound)
    if title_existing is not None:
        bound = min(bound, title_existing)
    edits = [(title, f"%(title).{bound}B")]
    edits += [(m, f"%({m.group('name')}).{OTHER_FIELD_CAP}B") for m in capped]
    for match, text in sorted(edits, key=lambda e: e[0].start(), reverse=True):
        tail = tail[: match.start()] + text + tail[match.end():]
    cut = known and len(str(info.get("title", "")).encode("utf-8")) > bound
    return Fitted(os.path.join(head, tail), limit, bound, cut)
