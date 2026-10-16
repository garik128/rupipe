"""Compare pipeline and Silero gates on one existing voice-step result.

Optional dependencies: onnxruntime and the model shipped in silero-vad.
No pipeline settings or existing audio are changed. Speech accuracy requires
listening or manual labels; detector agreement is not an accuracy score.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import binary_closing, binary_dilation, binary_opening
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rupipe.config import load_config
from rupipe.voice import _coef, _one_pole


def probabilities(audio, model):
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = opts.inter_op_num_threads = 1
    session = ort.InferenceSession(str(model), opts, providers=["CPUExecutionProvider"])
    state = np.zeros((2, 1, 128), np.float32)
    context = np.zeros((1, 64), np.float32)
    values = []
    for start in range(0, len(audio), 512):
        chunk = np.zeros((1, 512), np.float32)
        part = audio[start:start + 512]
        chunk[0, :len(part)] = part
        inp = np.concatenate([context, chunk], axis=1)
        out, state = session.run(None, {"input": inp, "state": state,
                                       "sr": np.array(16000, np.int64)})
        values.append(float(out.flat[0]))
        context = inp[:, -64:]
    return np.asarray(values)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("work", type=Path, help="Pipeline folder containing voice_raw.wav and vad.npy")
    ap.add_argument("--model", required=True, type=Path)
    ap.add_argument("--deps", type=Path, help="Optional isolated dependency directory")
    ap.add_argument("--out", required=True, type=Path, help="New output directory; must not exist")
    ap.add_argument("--config", type=Path, help="Same configuration used for voice extraction")
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--clips-from", type=Path, help="Reuse excerpt positions from a previous report.json")
    args = ap.parse_args()
    if args.out.exists():
        ap.error("Output already exists; choose a new folder to preserve the previous comparison")
    if not 0.15 < args.threshold < 1:
        ap.error("Threshold must be between 0.15 and 1")
    if args.deps:
        sys.path.insert(0, str(args.deps.resolve()))
    cfg = load_config(args.config)["voice"]
    raw, sr = sf.read(args.work / "voice_raw.wav", dtype="float32")
    baseline, baseline_sr = sf.read(args.work / "voice_C.wav", dtype="float32")
    times, existing = np.load(args.work / "vad.npy")
    assert sr == baseline_sr == 48000 and raw.ndim == 1 and len(raw) == len(baseline)
    assert np.isfinite(raw).all() and np.isfinite(baseline).all()
    hop_s = float(times[1] - times[0]) / sr
    base_mask = existing.astype(bool)

    # A common input gain avoids testing a neural VAD on abnormally quiet audio.
    # This gain affects detection only. Both listening variants use one shared gain.
    rms = np.sqrt(np.mean(raw.astype(np.float64) ** 2))
    detection_gain = min(10 ** (-20 / 20) / max(rms, 1e-12),
                         0.89 / max(float(np.abs(raw).max()), 1e-12))
    audio16 = resample_poly(raw * detection_gain, 1, 3).astype(np.float32)
    t0 = time.perf_counter()
    probs = probabilities(audio16, args.model)
    inference_s = time.perf_counter() - t0
    # Hysteresis, followed by the same phrase smoothing used by rupipe.
    active = False
    decisions = []
    for p in probs:
        if p >= args.threshold:
            active = True
        elif p < args.threshold - 0.15:
            active = False
        decisions.append(active)
    indices = np.minimum((times / sr / 0.032).astype(int), len(probs) - 1)
    silero = np.asarray(decisions)[indices]
    silero = binary_opening(silero, np.ones(max(1, int(0.1 / hop_s)), bool))
    silero = binary_closing(silero, np.ones(max(1, int(0.3 / hop_s)), bool))
    silero = binary_dilation(silero, np.ones(2 * max(1, int(cfg["hangover_ms"] / 1000 / hop_s)) + 1, bool))

    def envelope(mask):
        gate = _one_pole(np.where(mask, 1.0, 10 ** (cfg["gate_floor_db"] / 20)),
                         _coef(cfg["gate_release_ms"], hop_s),
                         _coef(cfg["gate_attack_ms"], hop_s))
        return np.interp(np.arange(len(raw)), times, gate).astype(np.float32)

    base_env = envelope(base_mask)
    # Recover exact original centre scaling; manifest rounds its coefficient.
    select = (np.abs(raw) > 1e-4) & (base_env > 0.9)
    scale = float(np.median(baseline[select] / (raw[select] * base_env[select])))
    rebuilt = raw * base_env * np.float32(scale)
    assert np.max(np.abs(rebuilt - baseline)) < 1e-5, "Config differs from the voice-step config"
    candidate = raw * envelope(silero) * np.float32(scale)
    reference = raw * np.float32(scale)
    common_gain = min(10 ** (-20 / 20) / max(float(np.sqrt(np.mean(baseline.astype(np.float64) ** 2))), 1e-12),
                      10 ** (-2 / 20) / max(float(np.abs(reference).max()), 1e-12))
    signals = {"A_pipeline": baseline * common_gain,
               "B_silero": candidate * common_gain,
               "reference_ungated": reference * common_gain}
    args.out.mkdir(parents=True)
    for name, data in signals.items():
        sf.write(args.out / f"{name}_full.flac", data, sr, subtype="PCM_24")
        check, check_sr = sf.read(args.out / f"{name}_full.flac", dtype="float32")
        assert check_sr == sr and len(check) == len(raw)
        assert np.max(np.abs(check - data)) < 2e-7 and np.abs(check).max() < 1

    # Select short excerpts with the most output difference in each direction.
    chosen = []
    difference = (signals["A_pipeline"] - signals["B_silero"]) ** 2
    frame_energy = np.asarray([np.mean(difference[i:i + sr]) for i in range(0, len(raw), sr)])
    frame_t = times / sr
    fixed = json.loads(args.clips_from.read_text(encoding="utf-8"))["clips"] if args.clips_from else None
    for title, mask in [("pipeline_only", base_mask & ~silero),
                        ("silero_only", silero & ~base_mask),
                        ("agreement_speech", base_mask & silero)]:
        scores = []
        starts = [int(c["start_s"]) for c in fixed if c["category"] == title] if fixed else range(0, int(len(raw) / sr) - 29)
        for start in starts:
            if start < 0 or start + 30 > len(raw) / sr:
                raise ValueError("Excerpt lies outside the current audio")
            if any(abs(start - prev["start_s"]) < 30 for prev in chosen):
                continue
            inside = (frame_t >= start) & (frame_t < start + 30)
            seconds = float(mask[inside].sum() * hop_s)
            energy = float(frame_energy[start:start + 30].sum())
            scores.append((seconds * (energy + 1e-12) if title != "agreement_speech" else seconds, start, seconds))
        if not scores:
            continue
        _, start, seconds = max(scores)
        chosen.append({"category": title, "start_s": start, "duration_s": 30,
                       "category_seconds": round(seconds, 2)})
        for name, data in signals.items():
            clip = data[start * sr:(start + 30) * sr]
            clip_path = args.out / f"{len(chosen):02d}_{title}_{name}.wav"
            sf.write(clip_path, clip, sr, subtype="PCM_16")
            check, check_sr = sf.read(clip_path, dtype="float32")
            assert len(check) == 30 * sr and check_sr == sr
            assert np.max(np.abs(check - clip)) < 3.1e-5

    report = {
        "duration_s": len(raw) / sr,
        "pipeline_speech_s": round(float(base_mask.sum() * hop_s), 2),
        "silero_speech_s": round(float(silero.sum() * hop_s), 2),
        "pipeline_only_s": round(float((base_mask & ~silero).sum() * hop_s), 2),
        "silero_only_s": round(float((silero & ~base_mask).sum() * hop_s), 2),
        "probability_quantiles_pipeline_only": np.quantile(probs[indices][base_mask & ~silero], [0, 0.25, 0.5, 0.75, 1]).round(4).tolist() if (base_mask & ~silero).any() else [],
        "inference_s": round(inference_s, 3), "threshold": args.threshold,
        "model_sha256": hashlib.sha256(args.model.read_bytes()).hexdigest(),
        "raw_sha256": hashlib.sha256((args.work / "voice_raw.wav").read_bytes()).hexdigest(),
        "versions": {name: importlib.metadata.version(name) for name in ["silero-vad", "onnxruntime", "numpy", "scipy"]},
        "detection_gain_db": round(float(20 * np.log10(detection_gain)), 2),
        "listening_gain_db": round(float(20 * np.log10(common_gain)), 2),
        "gate_settings": {k: cfg[k] for k in ["gate_floor_db", "gate_attack_ms", "gate_release_ms", "hangover_ms"]},
        "clips": chosen,
        "interpretation": "Agreement is not accuracy. Listen for missed words and residual background. Same extraction, centre scale and listening gain; only VAD mask differs."
    }
    (args.out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    np.savez(args.out / "masks.npz", sample_positions=times, pipeline=base_mask, silero=silero, probabilities=probs)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
