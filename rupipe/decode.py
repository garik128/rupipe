"""Step 2: decode the selected streams to float32 48 kHz WAV, one file per channel."""
from __future__ import annotations

import logging
from pathlib import Path

import soundfile as sf

from . import tools
from .probe import LAYOUT_CHANNELS

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000


def decode_stream(cfg: dict, src: Path, stream: dict, out_dir: Path, prefix: str,
                  force_stereo: bool = False) -> dict:
    """Decode one stream into <out_dir>/<prefix>_<CH>.wav.

    drc_scale 0: ffmpeg's AC3/E-AC3 decoder applies dynamic range compression by
    default, which would alter the original's dynamics.
    """
    layout = "stereo" if force_stereo else stream["layout"]
    channels = LAYOUT_CHANNELS[layout]
    # Default resampler: works in every ffmpeg build (soxr is not always compiled in);
    # for the usual 48 kHz sources it is a no-op anyway.
    chain = f"aresample={SAMPLE_RATE}"
    if force_stereo and stream["layout"] != "stereo":
        chain += ",aformat=channel_layouts=stereo"
    labels = "".join(f"[{c}]" for c in channels)
    graph = f"[0:{stream['index']}]{chain},channelsplit=channel_layout={layout}{labels}"

    cmd = [cfg["tools"]["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error", "-y",
           "-drc_scale", "0", "-i", src, "-filter_complex", graph]
    files = {}
    for c in channels:
        out = out_dir / f"{prefix}_{c}.wav"
        cmd += ["-map", f"[{c}]", "-c:a", "pcm_f32le", out]
        files[c] = out.name
    log.info("decoding stream %d (%s, %s) -> %s_*.wav", stream["index"], stream["codec"], layout, prefix)
    tools.run(cmd)

    lengths = {c: sf.info(str(out_dir / f)).frames for c, f in files.items()}
    if len(set(lengths.values())) != 1:
        raise RuntimeError(f"channel lengths differ after decode: {lengths}")
    return {
        "stream": stream["index"],
        "layout": layout,
        "channels": channels,
        "files": files,
        "frames": next(iter(lengths.values())),
        "start_time": stream["start_time"],
    }


def decode(cfg: dict, src: Path, analysis: dict, work: Path) -> dict:
    by_index = {s["index"]: s for s in analysis["streams"]}
    return {
        "original": decode_stream(cfg, src, by_index[analysis["original"]], work, "orig"),
        "russian": decode_stream(cfg, src, by_index[analysis["russian"]], work, "ru", force_stereo=True),
    }
