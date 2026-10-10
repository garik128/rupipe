"""STFT with a sqrt-Hann window at 75% overlap (perfect reconstruction)."""
from __future__ import annotations

import numpy as np


class Stft:
    def __init__(self, n_fft: int, hop: int):
        if n_fft % hop or n_fft // hop != 4:
            raise ValueError("Stft expects hop = n_fft / 4")
        self.n_fft = n_fft
        self.hop = hop
        # Periodic Hann: sum of squared sqrt-Hann windows at 75% overlap is exactly 2.
        self.win = np.sqrt(np.hanning(n_fft + 1)[:-1]).astype(np.float32)
        self.freqs = np.fft.rfftfreq(n_fft, 1.0 / 48000)

    def frames(self, length: int) -> int:
        return (length - self.n_fft) // self.hop + 1

    def forward(self, x: np.ndarray) -> np.ndarray:
        """(..., samples) -> (..., frames, bins), complex64. Frame n starts at n*hop."""
        n = self.frames(x.shape[-1])
        idx = np.arange(self.n_fft)[None, :] + self.hop * np.arange(n)[:, None]
        return np.fft.rfft(x[..., idx] * self.win, axis=-1).astype(np.complex64)

    def inverse(self, X: np.ndarray, length: int) -> np.ndarray:
        """(frames, bins) -> samples. Edges (first/last n_fft - hop samples) are incomplete."""
        frames = np.fft.irfft(X, self.n_fft, axis=-1).astype(np.float32) * self.win
        out = np.zeros(length, dtype=np.float32)
        for n in range(frames.shape[0]):
            a = n * self.hop
            out[a:a + self.n_fft] += frames[n][: max(0, min(self.n_fft, length - a))]
        return out / 2.0
