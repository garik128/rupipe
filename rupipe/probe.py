"""Step 1: list audio streams, read dialnorm, pick the original and the Russian track."""
from __future__ import annotations

import logging
from pathlib import Path

from . import tools

log = logging.getLogger(__name__)

RUS_TAGS = {"rus", "ru", "russian"}

# Channel order as ffmpeg names them in channelsplit, per layout.
LAYOUT_CHANNELS = {
    "mono": ["FC"],
    "stereo": ["FL", "FR"],
    "5.1": ["FL", "FR", "FC", "LFE", "BL", "BR"],
    "5.1(side)": ["FL", "FR", "FC", "LFE", "SL", "SR"],
    "7.1": ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"],
    "7.1(wide)": ["FL", "FR", "FC", "LFE", "BL", "BR", "FLC", "FRC"],
    "7.1(wide-side)": ["FL", "FR", "FC", "LFE", "FLC", "FRC", "SL", "SR"],
}


class _Bits:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read(self, n: int) -> int:
        v = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]
            v = (v << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v


def parse_dialnorm(frame: bytes) -> int | None:
    """Dialnorm (dB, -1..-31) from the BSI of the first AC3 or E-AC3 frame.

    ffprobe does not expose this field, so it is read from the bitstream directly.
    """
    if len(frame) < 16 or frame[0] != 0x0B or frame[1] != 0x77:
        return None
    bsid = frame[5] >> 3
    b = _Bits(frame[2:])
    if bsid <= 10:  # AC3
        b.read(16)            # crc1
        b.read(8)             # fscod, frmsizecod
        b.read(5)             # bsid
        b.read(3)             # bsmod
        acmod = b.read(3)
        if (acmod & 1) and acmod != 1:
            b.read(2)         # cmixlev
        if acmod & 4:
            b.read(2)         # surmixlev
        if acmod == 2:
            b.read(2)         # dsurmod
        b.read(1)             # lfeon
        value = b.read(5)
    else:  # E-AC3
        b.read(2)             # strmtyp
        b.read(3)             # substreamid
        b.read(11)            # frmsiz
        b.read(2)             # fscod
        b.read(2)             # fscod2 / numblkscod
        b.read(3)             # acmod
        b.read(1)             # lfeon
        b.read(5)             # bsid
        value = b.read(5)
    return -31 if value == 0 else -value


def _stream_info(s: dict) -> dict:
    tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
    return {
        "index": s["index"],
        "codec": s.get("codec_name"),
        "profile": s.get("profile"),
        "channels": s.get("channels"),
        "layout": s.get("channel_layout"),
        "sample_rate": int(s.get("sample_rate", 0)),
        "bit_rate": int(s["bit_rate"]) if s.get("bit_rate") else None,
        "start_time": float(s.get("start_time", 0.0)),
        "language": tags.get("language"),
        "title": tags.get("title"),
        "default": bool((s.get("disposition") or {}).get("default")),
    }


class TrackChoiceNeeded(ValueError):
    """The track cannot be chosen without the user: several candidates or none."""


def _describe(s: dict) -> str:
    br = f"{s['bit_rate'] // 1000}k" if s["bit_rate"] else "?"
    return (f"#{s['index']} {s['codec']} {s['layout'] or '?'} {br} "
            f"{s['language'] or '-'} \"{s['title'] or ''}\"")


def _ask(kind: str, option: str, candidates: list[dict], why: str) -> TrackChoiceNeeded:
    listing = "\n".join("    " + _describe(c) for c in candidates)
    return TrackChoiceNeeded(
        f"{kind}: {why}. Choose with {option} <number or part of the title>.\n"
        f"  Candidates:\n{listing}")


def _match(streams: list[dict], choice, kind: str, option: str) -> dict:
    """Explicit choice: a stream index or a unique substring of the track title."""
    text = str(choice).strip()
    if text.isdigit():
        for s in streams:
            if s["index"] == int(text):
                return s
        raise _ask(kind, option, streams, f"there is no audio track #{text}")
    hits = [s for s in streams if s["title"] and text.lower() in s["title"].lower()]
    if len(hits) == 1:
        return hits[0]
    exact = [s for s in hits if s["title"].strip().lower() == text.lower()]
    if len(exact) == 1:
        return exact[0]
    why = f"no track title contains '{text}'" if not hits else f"several titles contain '{text}'"
    raise _ask(kind, option, hits or streams, why)


def pick_original(streams: list[dict], choice) -> dict:
    """auto: the track with the most channels, if exactly one non-Russian track has that many."""
    if choice != "auto":
        return _match(streams, choice, "original", "--original")
    top = max(s["channels"] or 0 for s in streams)
    widest = [s for s in streams if (s["channels"] or 0) == top]
    candidates = [s for s in widest if (s["language"] or "").lower() not in RUS_TAGS]
    if len(candidates) == 1:
        return candidates[0]
    raise _ask("original", "--original", widest, "cannot tell which multichannel track is the original")


def pick_russian(streams: list[dict], choice, original: dict) -> dict:
    """auto only when there is a single other track; otherwise the user has to say which.

    Titles, numbers and even language tags differ between releases (a Russian
    voice-over can be tagged eng), so nothing is guessed.
    """
    others = [s for s in streams if s["index"] != original["index"]]
    if choice != "auto":
        return _match(others, choice, "russian", "--russian")
    if len(others) == 1:
        return others[0]
    raise _ask("russian", "--russian", others,
               "several candidate tracks" if others else "no track besides the original")


def analyze(cfg: dict, path: Path, strict: bool = True) -> dict:
    """List audio streams and resolve the two tracks.

    strict=False (for `probe`) records an unresolved choice in `problems`
    instead of raising, so the table can still be shown.
    """
    info = tools.ffprobe_streams(cfg, path)
    streams = [_stream_info(s) for s in info["streams"] if s.get("codec_type") == "audio"]
    if not streams:
        raise ValueError(f"{path}: no audio streams")
    for s in streams:
        if s["codec"] in ("ac3", "eac3"):
            raw = tools.first_audio_frames(cfg, path, s["index"], s["codec"])
            s["dialnorm"] = parse_dialnorm(raw)
        else:
            s["dialnorm"] = None

    result = {
        "duration": float(info["format"]["duration"]),
        "streams": streams,
        "original": None,
        "russian": None,
        "requested": {"original": cfg["tracks"]["original"], "russian": cfg["tracks"]["russian"]},
        "problems": [],
    }
    try:
        original = pick_original(streams, cfg["tracks"]["original"])
        result["original"] = original["index"]
        if original["layout"] not in LAYOUT_CHANNELS:
            raise ValueError(f"original: unsupported channel layout {original['layout']}")
        result["russian"] = pick_russian(streams, cfg["tracks"]["russian"], original)["index"]
    except ValueError as e:
        if strict:
            raise
        result["problems"].append(str(e))
    return result


def format_table(result: dict) -> str:
    rows = []
    for s in result["streams"]:
        role = ""
        if s["index"] == result["original"]:
            role = "ORIGINAL"
        elif s["index"] == result["russian"]:
            role = "RUSSIAN"
        br = f"{s['bit_rate'] // 1000}k" if s["bit_rate"] else "?"
        dn = str(s["dialnorm"]) if s["dialnorm"] is not None else "-"
        rows.append(
            f"{s['index']:>3}  {role:<9} {s['codec']:<6} {s['layout'] or '?':<11} {br:>6}  "
            f"dialnorm {dn:>4}  start {s['start_time']:.3f}s  "
            f"{s['language'] or '-'}  {s['title'] or ''}  {s['profile'] or ''}"
        )
    text = f"duration {result['duration']:.2f}s\n" + "\n".join(rows)
    for p in result.get("problems", []):
        text += f"\n  NEEDS A CHOICE: {p}"
    return text
