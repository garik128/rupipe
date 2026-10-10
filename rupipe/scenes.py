"""Pick contrasting scenes for listening tests and cut them from the finished file."""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import soundfile as sf

from . import tools
from .decode import SAMPLE_RATE

log = logging.getLogger(__name__)

# label, description shown to the user; with --count N the first N are cut
KINDS = [
    ("talk", "обычный диалог"),
    ("boom", "громко, много LFE (взрывы, удары)"),
    ("music_talk", "речь поверх музыки или плотного фона"),
    ("quiet_talk", "тихий разговор"),
]


def _per_second_power(path: Path) -> tuple[np.ndarray, np.ndarray]:
    with sf.SoundFile(str(path)) as f:
        secs = f.frames // SAMPLE_RATE
        power = np.zeros(secs)
        peak = np.zeros(secs)
        for i in range(secs):
            x = f.read(SAMPLE_RATE, dtype="float32")
            power[i] = np.mean(x.astype(np.float64) ** 2)
            peak[i] = np.abs(x).max()
    return power, peak


def stats(work: Path, decoded: dict, voiced: dict) -> dict:
    o = decoded["original"]
    pw, pk = {}, {}
    for ch in o["channels"]:
        pw[ch], pk[ch] = _per_second_power(work / o["files"][ch])
    secs = min(len(v) for v in pw.values())
    t_frames, vad = np.load(work / voiced["files"]["vad"])
    sec_idx = (t_frames / SAMPLE_RATE).astype(int)
    speech = np.bincount(sec_idx[sec_idx < secs], weights=vad[sec_idx < secs], minlength=secs)
    speech /= np.maximum(np.bincount(sec_idx[sec_idx < secs], minlength=secs), 1)
    return {
        "total": sum(v[:secs] for v in pw.values()),
        "lfe": pw.get("LFE", np.zeros(secs))[:secs],
        "centre": pw["FC"][:secs],
        "sides": sum(v[:secs] for c, v in pw.items() if c not in ("FC", "LFE")),
        "peak": np.max([v[:secs] for v in pk.values()], axis=0),
        "speech": speech,
    }


def pick(st: dict, length_s: int, count: int = len(KINDS), step_s: int = 5,
         skip_edges_s: int = 30) -> list[dict]:
    secs = len(st["total"])
    db = lambda x: 10 * np.log10(np.asarray(x) + 1e-12)
    starts = np.arange(skip_edges_s, max(skip_edges_s + 1, secs - length_s - skip_edges_s), step_s)
    rows = []
    for s in starts:
        sl = slice(s, s + length_s)
        rows.append({
            "start": int(s),
            "loud_db": float(db(st["total"][sl].mean())),
            "lfe_db": float(db(st["lfe"][sl].mean())),
            "lfe_peak_db": float(db(np.percentile(st["lfe"][sl], 95))),
            "peak_db": float(20 * np.log10(st["peak"][sl].max() + 1e-12)),
            "sides_vs_centre_db": float(db(st["sides"][sl].mean()) - db(st["centre"][sl].mean())),
            "speech": float(st["speech"][sl].mean()),
        })
    med_loud = float(np.median([r["loud_db"] for r in rows]))
    scores = {
        "boom": lambda r: r["lfe_peak_db"] + 0.5 * r["loud_db"],
        "quiet_talk": lambda r: -r["loud_db"] if r["speech"] >= 0.5 else -1e9,
        "music_talk": lambda r: r["sides_vs_centre_db"] + 0.5 * (r["loud_db"] - med_loud)
                                if r["speech"] >= 0.4 else -1e9,
        "talk": lambda r: r["speech"] - abs(r["loud_db"] - med_loud) / 20,
    }
    chosen = []
    for kind, desc in KINDS[:count]:
        free = [r for r in rows
                if all(abs(r["start"] - c["start"]) >= length_s for c in chosen)]
        if not free:
            break
        best = max(free, key=scores[kind])
        chosen.append({**best, "kind": kind, "description": desc, "length": length_s})
    return sorted(chosen, key=lambda c: c["start"])


def manual(st: dict, at: str, length_s: int) -> dict:
    """A fragment at a given time (HH:MM:SS or seconds), with the same statistics as picked ones."""
    parts = [float(x) for x in at.split(":")]
    start = int(sum(v * m for v, m in zip(reversed(parts), (1, 60, 3600))))
    sl = slice(start, start + length_s)
    db = lambda x: 10 * np.log10(np.asarray(x) + 1e-12)
    return {
        "start": start, "length": length_s, "kind": "manual", "description": "выбрано вручную",
        "loud_db": float(db(st["total"][sl].mean())),
        "lfe_db": float(db(st["lfe"][sl].mean())),
        "peak_db": float(20 * np.log10(st["peak"][sl].max() + 1e-12)),
        "sides_vs_centre_db": float(db(st["sides"][sl].mean()) - db(st["centre"][sl].mean())),
        "speech": float(st["speech"][sl].mean()),
    }


def _ts(s: float) -> str:
    return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}"


def cut(cfg: dict, muxed: dict, analysis: dict, scenes: list[dict], out_dir: Path) -> list[Path]:
    """Cut each scene from the finished file, keeping video, the new tracks, the
    original and the source Russian track, and subtitles."""
    out_dir.mkdir(parents=True, exist_ok=True)
    finished = Path(muxed["file"])
    if "track_map" not in muxed:
        raise RuntimeError("the finished file has no track map; redo the last step: run --from mux")
    # By position recorded at mux time: language tags are not reliable.
    keep = [str(i) for i in muxed["new_tracks"]] + [
        str(muxed["track_map"][str(analysis[role])]) for role in ("original", "russian")]

    outputs = []
    for i, sc in enumerate(scenes, 1):
        base = out_dir / f"{finished.stem}.{i:02d}_{sc['kind']}_{_ts(sc['start']).replace(':', '-')[:8]}"
        tmp = base.with_name(base.name + "-part.mkv")
        tools.run([cfg["tools"]["mkvmerge"], "-q", "-o", tmp, "--audio-tracks", ",".join(keep), finished,
                   "--split", f"parts:{_ts(sc['start'])}-{_ts(sc['start'] + sc['length'])}"],
                  ok_codes=(0, 1))
        produced = sorted(out_dir.glob(base.name + "-part*.mkv"))
        if not produced:
            raise RuntimeError(f"mkvmerge produced nothing for {base.name}")
        dst = base.with_name(base.name + finished.suffix)
        if dst.exists():
            dst.unlink()
        produced[0].replace(dst)
        outputs.append(dst)
        log.info("fragment %s: %s", sc["kind"], dst.name)
    return outputs


def describe(scenes: list[dict]) -> str:
    lines = []
    for i, s in enumerate(scenes, 1):
        lines.append(
            f"{i}. {_ts(s['start'])[:8]} +{s['length']}s  {s['kind']:<11} ({s['description']}): "
            f"громкость {s['loud_db']:.1f} dB, LFE {s['lfe_db']:.1f} dB, пик {s['peak_db']:.1f} dBFS, "
            f"речь {s['speech'] * 100:.0f}%, фронт/тылы к центру {s['sides_vs_centre_db']:+.1f} dB")
    return "\n".join(lines)
