"""Step 3: align the Russian track to the original.

Offset convention: lag L (samples) means Russian sample j plays together with
original sample j + L. The container gives the initial guess
L0 = (ru_start - orig_start) * sr; dense GCC-PHAT windows refine it and find
the places where the offset jumps (Russian tracks are often assembled from
pieces), and every segment is shifted by its own fractional lag.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf

from .decode import SAMPLE_RATE

log = logging.getLogger(__name__)

Reader = Callable[[int, int], np.ndarray]

# Downmix weights for the mono reference; LFE is left out.
DOWNMIX_WEIGHTS = {
    "FL": 1.0, "FR": 1.0, "FC": 0.707,
    "BL": 0.707, "BR": 0.707, "SL": 0.707, "SR": 0.707,
    "FLC": 0.707, "FRC": 0.707,
}
PHAT_BAND_HZ = (100.0, 8000.0)


def _read_padded(f: sf.SoundFile, start: int, length: int) -> np.ndarray:
    """Read [start, start+length) from a mono file, zero-filling outside its bounds."""
    out = np.zeros(length, dtype=np.float32)
    a, b = max(start, 0), min(start + length, f.frames)
    if b > a:
        f.seek(a)
        out[a - start:b - start] = f.read(b - a, dtype="float32", always_2d=False)
    return out


def file_reader(paths_weights: list[tuple[Path, float]]) -> tuple[Reader, int, Callable[[], None]]:
    """Weighted mono sum of several mono WAV files, read on demand."""
    handles = [(sf.SoundFile(str(p)), w) for p, w in paths_weights]
    frames = min(h.frames for h, _ in handles)

    def read(start: int, length: int) -> np.ndarray:
        acc = np.zeros(length, dtype=np.float32)
        for h, w in handles:
            acc += w * _read_padded(h, start, length)
        return acc

    def close() -> None:
        for h, _ in handles:
            h.close()

    return read, frames, close


def gcc_phat(x: np.ndarray, y: np.ndarray, sr: int) -> np.ndarray:
    """GCC-PHAT correlation r[k] ~ sum_t x[t] * y[t + k], k = 0..len(y)-len(x).

    Whitening is restricted to PHAT_BAND_HZ so codec-removed highs and rumble do not add noise.
    """
    n = 1 << int(np.ceil(np.log2(len(x) + len(y))))
    X = np.fft.rfft(x, n)
    Y = np.fft.rfft(y, n)
    R = np.conj(X) * Y
    R /= np.abs(R) + 1e-12
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    R[(freqs < PHAT_BAND_HZ[0]) | (freqs > PHAT_BAND_HZ[1])] = 0
    r = np.fft.irfft(R, n)
    return r[: len(y) - len(x) + 1]


def _peak(r: np.ndarray, exclude: int = 48) -> tuple[int, float, float]:
    """Index of the max, its parabolic sub-sample correction, peak/second-peak ratio."""
    k = int(np.argmax(r))
    frac = 0.0
    if 0 < k < len(r) - 1:
        a, b, c = r[k - 1], r[k], r[k + 1]
        denom = a - 2 * b + c
        if denom != 0:
            frac = float(0.5 * (a - c) / denom)
    rest = r.copy()
    rest[max(0, k - exclude): k + exclude + 1] = -np.inf
    second = float(np.max(rest)) if np.isfinite(rest).any() else 0.0
    conf = float(r[k] / second) if second > 0 else float("inf")
    return k, frac, conf


def _window_lag(orig: Reader, ru: Reader, pos: int, window: int, lag0: int, max_lag: int,
                sr: int) -> tuple[float | None, float]:
    """Lag (fractional) and confidence of one window starting at original sample `pos`."""
    x = orig(pos, window)
    y = ru(pos - lag0 - max_lag, window + 2 * max_lag)
    if np.sqrt(np.mean(x ** 2)) < 1e-5 or np.sqrt(np.mean(y ** 2)) < 1e-5:
        return None, 0.0
    r = gcc_phat(x, y, sr)
    k, frac, conf = _peak(r)
    # A peak on the very edge of the search range is not a measurement: the true offset is
    # outside it or there is nothing to correlate (quiet stretch). Seen on a near-silent
    # montage: three windows "confidently" at exactly +max_lag, which put the Russian voice
    # 2 s off the picture.
    if k <= 1 or k >= len(r) - 2:
        return None, 0.0
    return lag0 + max_lag - (k + frac), conf


def _score_at(orig: Reader, ru: Reader, pos: int, window: int, lag: float, sr: int) -> float:
    """GCC-PHAT value at one given lag (used to place a boundary between two offsets)."""
    li = int(round(lag))
    x = orig(pos, window)
    y = ru(pos - li - 8, window + 16)
    if np.sqrt(np.mean(x ** 2)) < 1e-5:
        return 0.0
    return float(gcc_phat(x, y, sr)[8])


def distinct_offsets(lags: list[float], tol: float) -> int:
    """How many really different offsets a list of segment lags holds.

    Consecutive lags within `tol` samples of the first lag of the current group count as one:
    measurements of the same offset can alternate by about a sample from window to window.
    A real drift walks away from the group's first lag and is counted.
    """
    count, anchor = 0, None
    for lag in lags:
        if anchor is None or abs(lag - anchor) > tol:
            count += 1
            anchor = lag
    return count


def measure_map(orig: Reader, ru: Reader, n_orig: int, lag0: int, scfg: dict,
                sr: int = SAMPLE_RATE) -> dict:
    """Offset map of the whole file.

    Dense GCC-PHAT windows give a lag every `step_s`. Consecutive windows that
    agree within `run_tolerance_samples` form a run with one offset. A lone window
    disagreeing with equal neighbours is treated as a measurement error. Where two
    runs meet, short windows scored at both offsets place the boundary. A Russian
    track assembled from pieces gets one segment per piece; a single segment
    means a plain constant offset.
    """
    max_lag = int(scfg["max_lag_s"] * sr)
    window = int(scfg["window_s"] * sr)
    step = int(scfg["step_s"] * sr)
    tol = float(scfg["run_tolerance_samples"])
    window = min(window, max(2 * sr, n_orig // 4))
    positions = list(range(0, max(1, n_orig - window), step))

    windows = []
    for p in positions:
        lag, conf = _window_lag(orig, ru, p, window, lag0, max_lag, sr)
        windows.append({"pos": p, "lag": lag, "conf": conf,
                        "used": lag is not None and conf >= scfg["min_confidence"]})
    used = [w for w in windows if w["used"]]
    result = {"container_lag": lag0, "windows": len(windows), "windows_used": len(used),
              "window_s": window / sr}
    if not used:
        log.warning("sync: no confident measurement, falling back to the container offset")
        result.update(segments=[{"start": 0, "end": n_orig, "lag": float(lag0), "windows": 0}],
                      flagged=True, reason="no confident measurement")
        return result

    # Runs of agreeing windows.
    runs: list[list[dict]] = []
    for w in used:
        if runs and abs(w["lag"] - np.median([x["lag"] for x in runs[-1]])) <= tol:
            runs[-1].append(w)
        else:
            runs.append([w])
    # Lone outliers between two runs with the same offset.
    changed = True
    while changed and len(runs) > 2:
        changed = False
        for i in range(1, len(runs) - 1):
            a, b, c = runs[i - 1], runs[i], runs[i + 1]
            la, lc = np.median([x["lag"] for x in a]), np.median([x["lag"] for x in c])
            if len(b) == 1 and abs(la - lc) <= tol:
                runs[i - 1:i + 2] = [a + c]
                changed = True
                break
    # Single-window runs at the very edges are not trusted either.
    if len(runs) > 1 and len(runs[0]) == 1:
        runs = runs[1:]
    if len(runs) > 1 and len(runs[-1]) == 1:
        runs = runs[:-1]

    lags = [float(np.median([x["lag"] for x in r])) for r in runs]
    # Boundaries: the first short window, scanning forward, that fits the next offset better.
    fine_w = int(scfg["refine_window_s"] * sr)
    fine_step = int(scfg["refine_step_s"] * sr)
    bounds = []
    for i in range(len(runs) - 1):
        lo = runs[i][-1]["pos"]
        hi = runs[i + 1][0]["pos"] + window - fine_w
        best = (lo + hi + fine_w) // 2
        for p in range(lo, max(lo + 1, hi + 1), fine_step):
            sa = _score_at(orig, ru, p, fine_w, lags[i], sr)
            sb = _score_at(orig, ru, p, fine_w, lags[i + 1], sr)
            if sb > sa and sb > 0:
                best = p + fine_w // 2
                break
        bounds.append(int(best))
    edges = [0] + bounds + [n_orig]
    segments = [{"start": edges[i], "end": edges[i + 1], "lag": round(lags[i], 3),
                 "windows": len(runs[i])} for i in range(len(runs))]
    distinct = distinct_offsets(lags, float(scfg.get("flag_tolerance_samples", 2.0)))
    flagged = distinct > scfg["max_segments"]
    result.update(
        segments=segments,
        distinct_offsets=distinct,
        flagged=flagged,
        reason=("too many offset changes: probably continuous drift, which piecewise alignment "
                "only approximates") if flagged else "",
    )
    return result


def frac_shift(x: np.ndarray, start: int, length: int, lag: float, taps: int = 32) -> np.ndarray:
    """out[j] = x(start + j - lag), with windowed-sinc interpolation for the fraction."""
    li = int(np.floor(lag))
    d = lag - li
    a = start - li - taps
    src = np.zeros(length + 2 * taps, dtype=np.float32)
    lo, hi = max(a, 0), min(a + len(src), len(x))
    if hi > lo:
        src[lo - a:hi - a] = x[lo:hi]
    if d < 1e-6:
        return src[taps:taps + length]
    m = np.arange(-taps, taps + 1)
    h = (np.sinc(m - d) * np.blackman(2 * taps + 1)).astype(np.float32)
    h /= h.sum()
    return np.convolve(src, h, mode="valid")[:length]


def write_aligned(ru_files: dict[str, Path], out_dir: Path, segments: list[dict], n_orig: int,
                  crossfade: int, prefix: str = "ru_aligned") -> dict[str, str]:
    """Shift each segment of the Russian channels by its own lag; crossfade at the joins."""
    out = {}
    half = crossfade // 2
    for ch, path in ru_files.items():
        x, _ = sf.read(str(path), dtype="float32")
        y = np.zeros(n_orig, dtype=np.float32)
        for i, seg in enumerate(segments):
            a = max(0, seg["start"] - (half if i > 0 else 0))
            b = min(n_orig, seg["end"] + (half if i < len(segments) - 1 else 0))
            part = frac_shift(x, a, b - a, seg["lag"])
            w = np.ones(b - a, dtype=np.float32)
            if i > 0 and half:
                w[:2 * half] = np.linspace(0, 1, 2 * half, dtype=np.float32)
            if i < len(segments) - 1 and half:
                w[-2 * half:] = np.linspace(1, 0, 2 * half, dtype=np.float32)
            y[a:b] += part * w
        dst = out_dir / f"{prefix}_{ch}.wav"
        sf.write(str(dst), y, SAMPLE_RATE, subtype="FLOAT")
        out[ch] = dst.name
    return out


def _ts(samples: int) -> str:
    s = samples / SAMPLE_RATE
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}"


def sync(cfg: dict, decoded: dict, work: Path) -> dict:
    o, r = decoded["original"], decoded["russian"]
    orig_parts = [(work / f, DOWNMIX_WEIGHTS[c]) for c, f in o["files"].items() if c in DOWNMIX_WEIGHTS]
    ru_parts = [(work / f, 1.0) for f in r["files"].values()]
    orig_read, n_orig, close_o = file_reader(orig_parts)
    ru_read, _, close_r = file_reader(ru_parts)
    lag0 = int(round((r["start_time"] - o["start_time"]) * SAMPLE_RATE))
    try:
        result = measure_map(orig_read, ru_read, n_orig, lag0, cfg["sync"])
    finally:
        close_o()
        close_r()

    segs = result["segments"]
    log.info("sync: container lag %d, %d segment(s), %d/%d windows confident", lag0, len(segs),
             result["windows_used"], result["windows"])
    for s in segs:
        log.info("sync:   %s - %s  lag %.3f samples (%.3f ms), %d windows", _ts(s["start"]),
                 _ts(s["end"]), s["lag"], s["lag"] / SAMPLE_RATE * 1000, s["windows"])
    if result["flagged"]:
        log.warning("sync flagged: %s", result.get("reason"))

    crossfade = int(cfg["sync"]["crossfade_ms"] / 1000 * SAMPLE_RATE)
    result["files"] = write_aligned({c: work / f for c, f in r["files"].items()}, work,
                                    segs, n_orig, crossfade)
    result["frames"] = n_orig
    return result
