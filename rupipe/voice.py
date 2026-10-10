"""Step 4 (voice-over): extract the Russian voice by subtracting the original.

Model, per STFT bin f and frame t, for each Russian channel c (L, R):

    RU_c(t, f) = g(t, b) * sum_k H_ck(f) * O_k(t, f) + V_c(t, f) + codec noise

H is the studio's static downmix (complex: also absorbs the sub-sample offset),
g is a slowly varying real gain per band b (the studio's ducking / compression),
V is the Russian voice. H and g are fitted by least squares over the whole file:
the voice is independent of the original, so it does not bias the fit.

Passes over the file (block by block, 60 s plus margins):
  1. accumulate normal equations -> H
  2. per-frame band statistics -> g(t, b), leak level of the residual
  3. residual, spectral suppression of the leak, write the voice; per-frame VAD data
Then in memory: VAD, gating, the studio's ducking depth, scaling to the centre
channel (voice_C.wav keeps the studio's voice/original balance; mix variants
shift it with voice_db).
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import binary_closing, binary_dilation, binary_opening, uniform_filter1d

from .decode import SAMPLE_RATE
from .stft import Stft

log = logging.getLogger(__name__)

NB_EDGES = np.r_[0.0, np.geomspace(100.0, 20000.0, 13), 24001.0]  # 14 bands
VOICE_BAND_HZ = (250.0, 4000.0)


class _Blocks:
    """Reads aligned blocks of several mono files with margins on both sides."""

    def __init__(self, paths: list[Path], block: int, margin: int):
        self.files = [sf.SoundFile(str(p)) for p in paths]
        self.n = min(f.frames for f in self.files)
        self.block = block
        self.margin = margin

    def __iter__(self):
        for s in range(0, self.n, self.block):
            e = min(s + self.block, self.n)
            a, b = s - self.margin, e + self.margin
            data = np.zeros((len(self.files), b - a), dtype=np.float32)
            lo, hi = max(a, 0), min(b, self.n)
            for i, f in enumerate(self.files):
                f.seek(lo)
                data[i, lo - a:hi - a] = f.read(hi - lo, dtype="float32", always_2d=False)
            yield s, e, a, data

    def close(self):
        for f in self.files:
            f.close()


def _smooth(x: np.ndarray, k: int) -> np.ndarray:
    return uniform_filter1d(x, size=k, axis=-1, mode="nearest") if k > 1 else x


def _one_pole(target: np.ndarray, attack: float, release: float) -> np.ndarray:
    """Asymmetric smoothing; values falling use `attack`, rising use `release` (per-step coeffs)."""
    out = np.empty_like(target)
    y = target[0]
    for i, t in enumerate(target):
        a = attack if t < y else release
        y = a * y + (1 - a) * t
        out[i] = y
    return out


def _coef(ms: float, step_s: float) -> float:
    return float(np.exp(-step_s / (ms / 1000.0))) if ms > 0 else 0.0


def extract(cfg: dict, decoded: dict, synced: dict, work: Path) -> dict:
    vc = cfg["voice"]
    st = Stft(vc["n_fft"], vc["n_fft"] // 4)
    H = st.hop
    block = int(vc["block_s"] * SAMPLE_RATE) // H * H
    margin = (vc["n_fft"] + H * (vc["gain_smooth_frames"] + 8)) // H * H

    o = decoded["original"]
    orig_ch = [c for c in o["channels"] if c != "LFE"]
    o_paths = [work / o["files"][c] for c in orig_ch]
    r_paths = [work / synced["files"][c] for c in ("FL", "FR")]
    n = synced["frames"]
    n_frames = n // H + 1
    K = len(orig_ch)
    band = np.digitize(st.freqs, NB_EDGES) - 1
    NB = len(NB_EDGES) - 1

    def blocks():
        return _Blocks(o_paths + r_paths, block, margin)

    def owned(s, e, a, T):
        """Local frame indices whose start lies in [s, e), and their global indices."""
        j0 = a // H
        loc = np.arange(T)
        glob = j0 + loc
        keep = (glob * H >= s) & (glob * H < e)
        return loc[keep], glob[keep]

    # Pass 1: H.
    log.info("voice: pass 1/3 (downmix fit)")
    F = len(st.freqs)
    G = np.zeros((F, K, K), dtype=np.complex128)
    P = np.zeros((F, 2, K), dtype=np.complex128)
    bl = blocks()
    for s, e, a, data in bl:
        S = st.forward(data)  # ch, T, F
        loc, _ = owned(s, e, a, S.shape[1])
        Ao, Ar = S[:K, loc], S[K:, loc]
        G += np.einsum("ktf,ltf->fkl", Ao.conj(), Ao)
        P += np.einsum("ktf,ctf->fck", Ao.conj(), Ar)
    bl.close()
    tr = np.trace(G, axis1=1, axis2=2).real / K
    G += (vc["ridge"] * tr + 1e-12)[:, None, None] * np.eye(K)
    Hm = np.linalg.solve(G[:, None], P[..., None])[..., 0].astype(np.complex64)  # F, c, K

    # Pass 2: band statistics -> g(t, b) and the leak level.
    log.info("voice: pass 2/3 (gain tracking)")
    num = np.zeros((NB, n_frames))
    den = np.zeros((NB, n_frames))
    rup = np.zeros((NB, n_frames))
    bl = blocks()
    for s, e, a, data in bl:
        S = st.forward(data)
        loc, glob = owned(s, e, a, S.shape[1])
        D = np.einsum("fck,ktf->ctf", Hm, S[:K, loc])
        R = S[K:, loc]
        bins_num = np.real(np.conj(D) * R).sum(axis=0)
        bins_den = (np.abs(D) ** 2).sum(axis=0)
        bins_ru = (np.abs(R) ** 2).sum(axis=0)
        for b in range(NB):
            m = band == b
            num[b, glob] = bins_num[:, m].sum(axis=1)
            den[b, glob] = bins_den[:, m].sum(axis=1)
            rup[b, glob] = bins_ru[:, m].sum(axis=1)
    bl.close()
    k = vc["gain_smooth_frames"]
    num_s, den_s = _smooth(num, k), _smooth(den, k)
    g = np.clip(num_s / (den_s + 1e-12), 0.0, 4.0)
    pred = g ** 2 * den                                   # original's power in RU, per band
    resid = np.maximum(rup - 2 * g * num + pred, 0.0)     # residual power, per band
    active = pred > 1e-9 * pred.max(axis=1, keepdims=True).clip(1e-30)
    leak = np.array([
        np.percentile(resid[b, active[b]] / pred[b, active[b]], vc["leak_percentile"])
        if active[b].sum() > 100 else 1.0
        for b in range(NB)
    ])
    log.info("voice: residual leak per band, dB: %s",
             " ".join(f"{10 * np.log10(x + 1e-12):.0f}" for x in leak))

    # Pass 3: residual, leak suppression, voice output.
    log.info("voice: pass 3/3 (voice extraction)")
    beta = vc["oversubtract"]
    floor = 10 ** (vc["mask_floor_db"] / 20)
    vmask = (st.freqs >= VOICE_BAND_HZ[0]) & (st.freqs < VOICE_BAND_HZ[1])
    v_energy = np.zeros(n_frames)
    l_energy = np.zeros(n_frames)
    ms_scale = 4.0 / st.n_fft ** 2
    raw_path = work / "voice_raw.wav"
    bl = blocks()
    with sf.SoundFile(str(raw_path), "w", samplerate=SAMPLE_RATE, channels=1, subtype="FLOAT") as out:
        for s, e, a, data in bl:
            S = st.forward(data)
            T = S.shape[1]
            glob = np.clip(a // H + np.arange(T), 0, n_frames - 1)
            gb = g[:, glob][band].T                       # T, F
            D = np.einsum("fck,ktf->ctf", Hm, S[:K]) * gb[None]
            V = S[K:] - D
            ev = _smooth((np.abs(V) ** 2).sum(axis=0).T, 3).T     # T, F
            el = _smooth((leak[band][None] * (np.abs(D) ** 2).sum(axis=0)).T, 3).T
            M = np.maximum(floor, 1.0 - beta * el / (ev + 1e-12))
            Vm = 0.5 * (V[0] + V[1]) * M                  # mono voice
            loc, gl = owned(s, e, a, T)
            # Scaled so that the value is the mean square of the band-limited signal.
            v_energy[gl] = (np.abs(Vm[loc][:, vmask]) ** 2).sum(axis=1) * ms_scale
            l_energy[gl] = el[loc][:, vmask].sum(axis=1) / 2 * ms_scale
            y = st.inverse(Vm, data.shape[1])
            out.write(y[s - a:e - a])
    bl.close()

    # VAD on the extracted voice.
    hop_s = H / SAMPLE_RATE
    vad = (v_energy > vc["vad_ratio"] * l_energy) & (v_energy > 10 ** (vc["vad_min_db"] / 10))
    vad = binary_opening(vad, np.ones(max(1, int(0.1 / hop_s)), bool))
    vad = binary_closing(vad, np.ones(max(1, int(0.3 / hop_s)), bool))
    vad = binary_dilation(vad, np.ones(2 * max(1, int(vc["hangover_ms"] / 1000 / hop_s)) + 1, bool))

    # Studio ducking depth and the scale to the centre channel.
    gbroad = _smooth(num, 3 * k).sum(axis=0) / (_smooth(den, 3 * k).sum(axis=0) + 1e-12)
    loud = den.sum(axis=0) > 1e-6 * den.sum(axis=0).max()
    g_out = float(np.median(gbroad[~vad & loud])) if (~vad & loud).sum() > 50 else 1.0
    g_in = float(np.median(gbroad[vad & loud])) if (vad & loud).sum() > 50 else g_out
    depth = float(np.clip(g_in / g_out, 10 ** (-30 / 20), 1.0))
    fc = orig_ch.index("FC")
    mid = (st.freqs >= 300) & (st.freqs < 3000)
    h_c = float(np.abs(Hm[mid, :, fc]).mean()) * g_out
    to_c = 1.0 / max(h_c, 1e-3)

    # Gate the voice between phrases and scale it to the centre channel.
    gate_floor = 10 ** (vc["gate_floor_db"] / 20)
    gate = _one_pole(np.where(vad, 1.0, gate_floor),
                     _coef(vc["gate_release_ms"], hop_s), _coef(vc["gate_attack_ms"], hop_s))
    t_frames = np.arange(n_frames) * H + st.n_fft / 2
    np.save(work / "vad.npy", np.stack([t_frames, vad]).astype(np.float32))

    raw, _ = sf.read(str(raw_path), dtype="float32")
    env = np.interp(np.arange(len(raw)), t_frames, gate).astype(np.float32)
    voice_c = raw * env * np.float32(to_c)
    sf.write(str(work / "voice_C.wav"), voice_c, SAMPLE_RATE, subtype="FLOAT")

    speech_s = float(vad.sum() * hop_s)
    result = {
        "files": {"voice_C": "voice_C.wav", "voice_raw": raw_path.name, "vad": "vad.npy"},  # rows: sample position of frame centre, 0/1,
        "downmix_FC_to_LR": round(h_c, 4),
        "leak_db_per_band": [round(float(10 * np.log10(x + 1e-12)), 1) for x in leak],
        "duck_depth_db": round(20 * np.log10(depth), 2),
        "studio_gain_out_of_voice": round(g_out, 3),
        "voice_seconds": round(speech_s, 1),
        "voice_peak_dbfs": round(float(20 * np.log10(np.abs(voice_c).max() + 1e-12)), 2),
    }
    log.info("voice: FC->L/R %.3f, studio ducking %.1f dB, voice %.0f s of %.0f s",
             h_c, result["duck_depth_db"], speech_s, n / SAMPLE_RATE)
    return result
