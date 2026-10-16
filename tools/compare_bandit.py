"""Compare an extracted mono voice before/after BandIt Plus or TIGER-DnR.

Uses an external, separately installed model implementation. Does not change
pipeline audio, manifests, mixes, or NAS files. Listening gain is shared.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, correlate, resample_poly, sosfilt


def separate(model, torch, audio, chunk_s, overlap_s, device="cpu", engine="bandit"):
    size, overlap = int(chunk_s * 44100), int(overlap_s * 44100)
    hop = size - overlap
    if hop < 1 or overlap < 1:
        raise ValueError("Require 0 < overlap < chunk")
    output = np.zeros(len(audio), np.float64)
    weights = np.zeros(len(audio), np.float64)
    for start in range(0, len(audio), hop):
        part = audio[start:start + size]
        padded = np.pad(part, (0, size - len(part)))
        tensor = torch.from_numpy(padded.copy())[None, None].to(device)
        with torch.inference_mode():
            if engine == "tiger":
                estimate = model.dialog(tensor)[0, 2, :len(part)].cpu().numpy()
            else:
                _, result = model({"audio": {"mixture": tensor}})
                estimate = result["audio"]["speech"][0, 0, :len(part)].cpu().numpy()
        window = np.ones(len(part))
        if start:
            fade = min(overlap, len(part))
            window[:fade] *= np.linspace(0, 1, fade)
        if start + size < len(audio):
            window[-overlap:] *= np.linspace(1, 0, overlap)
        output[start:start + len(part)] += estimate * window
        weights[start:start + len(part)] += window
        print(f"{engine}: {min(start + size, len(audio)) / 44100:.1f}/{len(audio) / 44100:.1f}s", flush=True)
    assert np.all(weights > 0)
    return (output / weights).astype(np.float32)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("work", type=Path)
    ap.add_argument("--upstream", required=True, type=Path)
    ap.add_argument("--checkpoint", required=True, type=Path)
    ap.add_argument("--sha256", required=True, help="Expected checkpoint hash")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--at", required=True, action="append", type=float)
    ap.add_argument("--length", type=float, default=30)
    ap.add_argument("--chunk", type=float, default=6)
    ap.add_argument("--overlap", type=float, default=2)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--engine", choices=["bandit", "tiger"], default="bandit")
    ap.add_argument("--model-config", type=Path, help="TIGER config.json")
    ap.add_argument("--device", choices=["cpu", "xpu"], default="cpu")
    args = ap.parse_args()
    if args.out.exists():
        ap.error("Output must be a new directory")
    if args.length <= 0 or any(t < 0 for t in args.at) or args.threads < 1:
        ap.error("Invalid duration, start or thread count")
    sha = hashlib.sha256(args.checkpoint.read_bytes()).hexdigest()
    if sha.lower() != args.sha256.lower():
        ap.error("Checkpoint hash mismatch")
    sys.path.insert(0, str(args.upstream.resolve()))
    import torch

    torch.set_num_threads(args.threads)
    if args.device == "xpu" and not torch.xpu.is_available():
        ap.error("Intel XPU unavailable; use --device cpu explicitly")
    if args.engine == "tiger":
        if not args.model_config:
            ap.error("TIGER requires --model-config")
        from look2hear.models.tiger_dnr import TIGERDNR
        from safetensors.torch import load_file
        config = json.loads(args.model_config.read_text(encoding="utf-8"))
        model = TIGERDNR(**config)
        model.load_state_dict(load_file(str(args.checkpoint)), strict=True)
    else:
        from bandit_inference import clean_state_dict, create_model, detect_model_config
        checkpoint = torch.load(args.checkpoint, map_location="cpu", weights_only=True)
        state = checkpoint.get("state_dict", checkpoint)
        config = detect_model_config(state)
        model = create_model(config, fs=44100)
        model.load_state_dict(clean_state_dict(state, config), strict=True)
    model.to(args.device).eval()
    times, vad = np.load(args.work / "vad.npy")
    args.out.mkdir(parents=True)
    report = {"engine": args.engine, "device": args.device, "checkpoint_sha256": sha, "model_config": config, "model_sample_rate": 44100,
              "upstream_commit": subprocess.check_output(["git", "-C", str(args.upstream), "rev-parse", "HEAD"], text=True).strip(),
              "chunk_s": args.chunk, "overlap_s": args.overlap,
              "versions": {n: importlib.metadata.version(n) for n in ["torch", "torchaudio", "numpy", "scipy"]},
              "excerpts": []}
    for i, start_s in enumerate(args.at, 1):
        start, frames = round(start_s * 48000), round(args.length * 48000)
        with sf.SoundFile(args.work / "voice_C.wav") as f:
            assert f.samplerate == 48000 and f.channels == 1
            if start + frames > f.frames:
                raise ValueError("Excerpt exceeds audio")
            lo, hi = max(0, start - 96000), min(f.frames, start + frames + 96000)
            f.seek(lo)
            context = f.read(hi - lo, dtype="float32")
        before = context[start - lo:start - lo + frames]
        input_gain = min(1.0, 0.8 / max(float(np.abs(context).max()), 1e-12))
        inp = resample_poly(context * input_gain, 147, 160).astype(np.float32)
        t0 = time.perf_counter()
        if args.device == "xpu":
            torch.xpu.reset_peak_memory_stats()
        clean = separate(model, torch, inp, args.chunk, args.overlap, args.device, args.engine)
        elapsed = time.perf_counter() - t0
        restored = resample_poly(clean, 160, 147).astype(np.float32) / input_gain
        after = restored[start - lo:start - lo + frames]
        assert len(after) == frames and np.isfinite(after).all()
        removed = before - after
        gain = min(1.0, 10 ** (-2 / 20) / max(float(np.abs(x).max()) for x in [before, after, removed]))
        base = f"{i:02d}_{int(start_s) // 3600:02d}-{int(start_s) % 3600 // 60:02d}-{int(start_s) % 60:02d}"
        for name, data in [("A_before", before), (f"B_{args.engine}", after), ("removed", removed)]:
            sf.write(args.out / f"{base}_{name}.wav", data * gain, 48000, subtype="PCM_24")
            saved, sr = sf.read(args.out / f"{base}_{name}.wav", dtype="float32")
            assert len(saved) == frames and sr == 48000 and np.abs(saved).max() < 1
            assert np.max(np.abs(saved - data * gain)) < 2e-7
        # Keep unscaled float audio for a later mix comparison after listening.
        sf.write(args.out / f"{base}_clean_float.wav", after, 48000, subtype="FLOAT")
        sos = butter(4, [250, 4000], btype="bandpass", fs=48000, output="sos")
        a, b = [sosfilt(sos, x.astype(np.float64)) for x in [before, after]]
        block = 4800
        count = frames // block
        ea = np.mean(a[:count * block].reshape(count, block) ** 2, axis=1)
        eb = np.mean(b[:count * block].reshape(count, block) ** 2, axis=1)
        centres = start + np.arange(count) * block + block / 2
        speech = (np.interp(centres, times, vad) > 0.5) & (ea > 1e-6)
        ratios = 10 * np.log10((eb + 1e-20) / (ea + 1e-20))
        lost = speech & (ratios < -12)
        intervals = []
        transitions = np.diff(np.r_[False, lost, False].astype(int))
        for first, last in zip(np.flatnonzero(transitions == 1), np.flatnonzero(transitions == -1)):
            intervals.append({"from_clip_s": round(first * 0.1, 2), "to_clip_s": round(last * 0.1, 2)})
        # This is a signal check, not a speech-accuracy score.
        cc = correlate(b, a, mode="full", method="fft")
        mid = len(a) - 1
        lag = int(np.argmax(cc[mid - 2400:mid + 2401]) - 2400)
        row = {"base": base, "source_start_s": start_s, "duration_s": frames / 48000,
               "gpu_peak_allocated_gib": round(torch.xpu.max_memory_allocated() / 2**30, 3) if args.device == "xpu" else None,
               "inference_s": round(elapsed, 2), "input_gain_db": round(float(20 * np.log10(input_gain)), 2),
               "shared_listening_gain_db": round(float(20 * np.log10(gain)), 2),
               "voice_band_speech_median_change_db": round(float(np.median(ratios[speech])), 2) if speech.any() else None,
               "voice_band_drop_over_12db_s": round(float(lost.sum() * 0.1), 2),
               "candidate_drop_intervals": intervals, "waveform_lag_ms": round(lag / 48, 3)}
        report["excerpts"].append(row)
        # Context reference at the exact same source position, not a quality score.
        reference = []
        for channel in ["FL", "FR"]:
            with sf.SoundFile(args.work / f"ru_aligned_{channel}.wav") as f:
                f.seek(start)
                reference.append(f.read(frames, dtype="float32"))
        source_stereo = np.column_stack(reference)
        source_gain = min(1.0, 10**(-2 / 20) / max(float(np.abs(source_stereo).max()), 1e-12))
        sf.write(args.out / f"{base}_source_russian.wav", source_stereo * source_gain, 48000, subtype="PCM_24")
        print(json.dumps(row), flush=True)
        (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
