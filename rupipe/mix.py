"""Step 5: build the 5.1 channels for each mix variant."""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import minimum_filter1d, uniform_filter1d

from .audibility import Audibility

from .decode import SAMPLE_RATE
from .voice import _coef, _one_pole

log = logging.getLogger(__name__)

LIMITER_WINDOW = int(0.010 * SAMPLE_RATE)


def limit(x: np.ndarray, ceiling: float) -> tuple[np.ndarray, int]:
    """Sample-peak limiter that only touches samples near overs.

    Minimum filter of width 2W followed by a moving average of width W never exceeds
    the gain each sample needs, so the result stays under the ceiling.
    """
    over = np.abs(x) > ceiling
    if not over.any():
        return x, 0
    need = np.minimum(1.0, ceiling / np.maximum(np.abs(x), 1e-12)).astype(np.float32)
    env = minimum_filter1d(need, 2 * LIMITER_WINDOW + 1)
    env = uniform_filter1d(env, LIMITER_WINDOW + 1)
    return x * np.minimum(env, 1.0), int(over.sum())


def mix(cfg: dict, decoded: dict, voiced: dict, work: Path) -> dict:
    o = decoded["original"]
    mcfg = cfg["mix"]
    ceiling = 10 ** (mcfg["ceiling_dbfs"] / 20)
    t_frames, vad = np.load(work / voiced["files"]["vad"])
    vad = vad > 0.5
    step_s = float(t_frames[1] - t_frames[0]) / SAMPLE_RATE
    voice, _ = sf.read(str(work / voiced["files"]["voice_C"]), dtype="float32")
    n = len(voice)
    speech = np.interp(np.arange(n), t_frames, vad.astype(np.float32)) > 0.5
    aud = Audibility(work, voiced["files"]["voice_C"], o["files"], speech)
    studio = aud.measure(0.0, 0.0)
    if studio["glimpse"] is None:
        log.warning("mix: too little speech to measure audibility (%d s); voice_db auto -> 0", aud.blocks)
    else:
        log.info("mix: studio balance: voice over original centre %+.1f dB, audible %.0f%% (%d s sampled)",
                 studio["ratio_db"], studio["glimpse"] * 100, aud.blocks)

    variants = {}
    for name, v in mcfg["variants"].items():
        duck_db = voiced["duck_depth_db"] if v["duck_db"] == "auto" else float(v["duck_db"])
        duck_db = min(duck_db, 0.0)
        depth = 10 ** (duck_db / 20)
        duck = _one_pole(np.where(vad, depth, 1.0),
                         _coef(mcfg["duck_attack_ms"], step_s), _coef(mcfg["duck_release_ms"], step_s))
        duck_s = np.interp(np.arange(n), t_frames, duck).astype(np.float32)
        if v["voice_db"] == "auto":
            # Quietest voice that is still audible over the rest of the mix as often as the target says.
            target = float(v.get("target_audible", mcfg["target_audible"]))
            voice_db = aud.solve(target, duck_db, mcfg["voice_db_min"], mcfg["voice_db_max"])
        else:
            voice_db = float(v["voice_db"])
        got = aud.measure(voice_db, duck_db)
        if v["voice_db"] == "auto" and got["glimpse"] is not None and got["glimpse"] < target - 0.005:
            log.warning("mix %s: target audibility %.0f%% not reached within voice_db_max (%.0f%% at %+.1f dB)",
                        name, target * 100, got["glimpse"] * 100, voice_db)
        vgain = np.float32(10 ** (voice_db / 20))
        files, stats = {}, {}
        for ch in o["channels"]:
            if ch not in v["duck_channels"] and ch != "FC":
                files[ch] = o["files"][ch]  # untouched: the decoded original is used as is
                continue
            x, _ = sf.read(str(work / o["files"][ch]), dtype="float32")
            x = x[:n]
            if ch in v["duck_channels"] and depth < 1.0:
                x = x * duck_s
            if ch == "FC":
                x = x + vgain * voice
            x, overs = limit(x, ceiling)
            dst = f"mix_{name}_{ch}.wav"
            sf.write(str(work / dst), x, SAMPLE_RATE, subtype="FLOAT")
            files[ch] = dst
            stats[ch] = {"peak_dbfs": round(float(20 * np.log10(np.abs(x).max() + 1e-12)), 2),
                         "samples_limited": overs}
        variants[name] = {"files": files, "channels": o["channels"], "layout": o["layout"],
                          "voice_db": round(voice_db, 2), "duck_db": round(duck_db, 2),
                          "audible": _round(got["glimpse"], 3),
                          "centre_wins": _round(got["centre_wins"], 3),
                          "voice_over_original_db": _round(got["ratio_db"], 1),
                          "stats": stats}
        log.info("mix %s (voice %+.2f dB, duck %.1f dB, audible %s, voice over centre %s): %s",
                 name, voice_db, duck_db, _pct(got["glimpse"]), _db(got["ratio_db"]), ", ".join(
                     f"{c} peak {s['peak_dbfs']} dBFS, limited {s['samples_limited']}" for c, s in stats.items()))
    return {"variants": variants,
            "studio_audible": _round(studio["glimpse"], 3),
            "studio_voice_over_original_db": _round(studio["ratio_db"], 1)}


def _round(x, nd):
    return None if x is None else round(x, nd)


def _pct(x):
    return "?" if x is None else f"{x * 100:.0f}%"


def _db(x):
    return "?" if x is None else f"{x:+.1f} dB"
