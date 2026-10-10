"""How well the Russian voice stands out in the final mix, as a function of its level.

Glimpse proportion (after Cooke's glimpsing model of speech in noise): the share of
voice-carrying time-frequency cells, 250-4000 Hz, during Russian speech, where the
voice is at least 3 dB above everything else the listener hears - the ducked
original centre plus the other full-range channels. It is computed on random
1 s blocks of speech and used to choose the voice level: the user's preference
was calibrated by ear as a target glimpse share, which, unlike a fixed dB offset,
carries over between studios with different balances and between dense and
sparse mixes.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from .decode import SAMPLE_RATE

N_FFT, HOP = 1024, 256
GLIMPSE_SNR = 10 ** (3 / 10)   # 3 dB
CARRY_DB = -30                 # cells within 30 dB of the block's loudest voice cell
OTHER_CHANNELS = ("FL", "FR", "SL", "SR", "BL", "BR")


class Audibility:
    """Spectra of sampled speech blocks; evaluates the glimpse share for any voice gain."""

    def __init__(self, work: Path, voice_file: str, orig_files: dict[str, str],
                 speech: np.ndarray, blocks: int = 400, seed: int = 0):
        sr = SAMPLE_RATE
        secs = len(speech) // sr
        per_sec = speech[: secs * sr].reshape(secs, sr).mean(axis=1) > 0.9
        cand = np.flatnonzero(per_sec)
        rng = np.random.default_rng(seed)
        pick = np.sort(rng.choice(cand, size=min(blocks, len(cand)), replace=False)) if len(cand) else cand
        self.blocks = len(pick)

        win = np.hanning(N_FFT).astype(np.float32)
        freqs = np.fft.rfftfreq(N_FFT, 1 / sr)
        band = (freqs >= 250) & (freqs < 4000)
        idx = np.arange(N_FFT)[None] + HOP * np.arange((sr - N_FFT) // HOP + 1)[:, None]

        def spectra(path: Path) -> np.ndarray:
            out = []
            with sf.SoundFile(str(path)) as f:
                for b in pick:
                    f.seek(int(b) * sr)
                    x = f.read(sr, dtype="float32")
                    out.append(np.abs(np.fft.rfft(x[idx] * win, axis=-1)[:, band]) ** 2)
            return np.stack(out) if out else np.zeros((0, idx.shape[0], band.sum()))

        self.V = spectra(work / voice_file)
        self.C = spectra(work / orig_files["FC"])
        others = [c for c in OTHER_CHANNELS if c in orig_files]
        self.O = sum(spectra(work / orig_files[c]) for c in others) if others else np.zeros_like(self.C)
        peak = self.V.max(axis=(1, 2), keepdims=True) if self.blocks else 1.0
        self.carry = self.V > peak * 10 ** (CARRY_DB / 10)

    def measure(self, voice_db: float, duck_db: float) -> dict:
        """Glimpse share, share of cells where the original centre beats the voice,
        and the median voice-over-centre ratio, for the given voice gain and ducking."""
        if not self.blocks or not self.carry.any():
            return {"glimpse": None, "centre_wins": None, "ratio_db": None}
        g2 = 10 ** (voice_db / 10)
        d2 = 10 ** (min(duck_db, 0.0) / 10)
        v = g2 * self.V[self.carry]
        c = d2 * self.C[self.carry]
        o = self.O[self.carry]
        ratio = 10 * np.log10((g2 * self.V.sum(axis=(1, 2))) / np.maximum(d2 * self.C.sum(axis=(1, 2)), 1e-12))
        return {
            "glimpse": float((v > GLIMPSE_SNR * (c + o)).mean()),
            "centre_wins": float((c > v).mean()),
            "ratio_db": float(np.median(ratio)),
        }

    def solve(self, target: float, duck_db: float, lo: float, hi: float, step: float = 0.25) -> float:
        """Smallest voice gain in [lo, hi] reaching the target glimpse share (hi if never reached)."""
        for vdb in np.arange(lo, hi + step / 2, step):
            m = self.measure(float(vdb), duck_db)
            if m["glimpse"] is None:
                return 0.0
            if m["glimpse"] >= target:
                return float(vdb)
        return float(hi)
