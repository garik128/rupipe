"""Steps 6-7: encode each mix variant to E-AC3 and add the tracks to a copy of the input."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import tools

log = logging.getLogger(__name__)


def encode(cfg: dict, analysis: dict, mixed: dict, work: Path) -> dict:
    ecfg = cfg["export"]
    by_index = {s["index"]: s for s in analysis["streams"]}
    dialnorm = ecfg["dialnorm"]
    if dialnorm == "auto":
        dialnorm = by_index[analysis["original"]]["dialnorm"] or -31
    out = {}
    for name, v in mixed["variants"].items():
        chans = v["channels"]
        cmd = [cfg["tools"]["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error", "-y"]
        for ch in chans:
            cmd += ["-i", work / v["files"][ch]]
        inputs = "".join(f"[{i}:a]" for i in range(len(chans)))
        mapping = "|".join(f"{i}.0-{ch}" for i, ch in enumerate(chans))
        graph = f"{inputs}join=inputs={len(chans)}:channel_layout={v['layout']}:map={mapping}[a]"
        dst = work / f"ru51_{name}.eac3"
        cmd += ["-filter_complex", graph, "-map", "[a]", "-c:a", "eac3",
                "-b:a", ecfg["bitrate"], "-dialnorm", str(dialnorm), dst]
        log.info("encoding %s -> %s (%s, dialnorm %s)", name, dst.name, ecfg["bitrate"], dialnorm)
        tools.run(cmd)
        out[name] = dst.name
    return {"files": out, "dialnorm": dialnorm, "bitrate": ecfg["bitrate"]}


def mux(cfg: dict, src: Path, analysis: dict, encoded: dict, work: Path) -> dict:
    """Copy the input with the new tracks added first among the audio tracks.

    Output order: the source video tracks, the new tracks, then every other source
    track in its original order. The input file is never modified. The new tracks
    become default; the source audio tracks lose the default flag but are otherwise
    copied as they are.
    """
    ecfg = cfg["export"]
    out_dir = Path(cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{src.stem}.rus51{src.suffix}"
    by_index = {s["index"]: s for s in analysis["streams"]}
    delay_ms = round(by_index[analysis["original"]]["start_time"] * 1000)

    src_tracks = _tracks(cfg, src)
    videos = [t["id"] for t in src_tracks if t["type"] == "video"]
    others = [t["id"] for t in src_tracks if t["type"] != "video"]
    n_new = len(encoded["files"])

    cmd = [cfg["tools"]["mkvmerge"], "-q", "-o", dst]
    for s in analysis["streams"]:
        cmd += ["--default-track-flag", f"{s['index']}:no"]
    cmd += [src]
    studio = ""
    if ecfg.get("studio_in_name", True) and analysis.get("russian") is not None:
        studio = (by_index[analysis["russian"]].get("title") or "").strip()
    for name, f in encoded["files"].items():
        default = "yes" if name == ecfg["default_variant"] else "no"
        title = ecfg["track_names"].get(name, name)
        if studio:
            title = f"{title} - {studio}"
        cmd += ["--language", "0:rus", "--track-name", f"0:{title}",
                "--default-track-flag", f"0:{default}"]
        if delay_ms:
            cmd += ["--sync", f"0:{delay_ms}"]
        cmd += [work / f]
    order = [f"0:{v}" for v in videos] + [f"{i + 1}:0" for i in range(n_new)] + [f"0:{t}" for t in others]
    cmd += ["--track-order", ",".join(order)]
    log.info("muxing -> %s", dst)
    tools.run(cmd, ok_codes=(0, 1))
    expected = videos + [None] * n_new + others
    return {"file": str(dst), **_track_map(cfg, src_tracks, dst, expected)}


def _tracks(cfg: dict, path: Path) -> list[dict]:
    info = json.loads(tools.run([cfg["tools"]["mkvmerge"], "-J", path]).stdout.decode("utf-8-sig"))
    return info["tracks"]


def _track_map(cfg: dict, src_tracks: list[dict], dst: Path, expected: list[int | None]) -> dict:
    """Where each source track ended up in the output, by position, not by tags.

    Language tags are unreliable (ffprobe and mkvmerge can disagree on the same
    track), so later steps find tracks through this map. `expected` is the output
    order: source track ids, None for each new track.
    """
    d = _tracks(cfg, dst)
    if len(expected) != len(d):
        raise RuntimeError(f"unexpected track count in {dst.name}: {len(d)} instead of {len(expected)}")
    by_id = {t["id"]: t for t in src_tracks}
    track_map, new_tracks = {}, []
    for pos, (src_id, out) in enumerate(zip(expected, d)):
        if src_id is None:
            if out["type"] != "audio" or out["codec"] != "E-AC-3":
                raise RuntimeError(f"track order check failed in {dst.name}: position {pos} is not a new track")
            new_tracks.append(out["id"])
            continue
        if out["codec"] != by_id[src_id]["codec"]:
            raise RuntimeError(f"track order check failed in {dst.name} at position {pos}")
        track_map[str(src_id)] = out["id"]
    return {"track_map": track_map, "new_tracks": new_tracks}
