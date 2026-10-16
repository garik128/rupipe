"""Make short stereo WAVs and E-AC3 5.1 comparison MKAs after step mix.

Reads full-length pipeline audio without copying the video. Excerpts use the
same positions for all variants. Stereo WAVs share one gain per scene to avoid
clipping without disguising voice-level differences. Temporary encoding inputs
are existing mono files; no large multichannel WAV is written.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rupipe import scenes, tools
from rupipe.config import load_config


def read_excerpt(path, start, frames):
    with sf.SoundFile(path) as f:
        if f.samplerate != 48000 or f.channels != 1:
            raise ValueError(f"Expected mono 48 kHz: {path}")
        f.seek(start)
        data = f.read(frames, dtype="float32")
    if len(data) != frames or not np.isfinite(data).all():
        raise ValueError(f"Incomplete or invalid excerpt: {path}")
    return data


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("work", type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--config", type=Path)
    ap.add_argument("--length", type=int, default=60)
    ap.add_argument("--count", type=int, default=4)
    args = ap.parse_args()
    if args.out.exists():
        ap.error("Choose a new output directory; previous previews are preserved")
    if args.length < 1 or args.count not in range(1, 5):
        ap.error("Positive length and count 1..4 required")
    cfg = load_config(args.config)
    m = json.loads((args.work / "manifest.json").read_text(encoding="utf-8"))["steps"]
    decoded, voiced, mixed = [m[s]["result"] for s in ("decode", "voice", "mix")]
    orig = decoded["original"]
    channels = orig["channels"]
    analysis = m["analyze"]["result"]
    original_stream = next(s for s in analysis["streams"] if s["index"] == analysis["original"])
    dialnorm = original_stream["dialnorm"] or -31
    if channels != ["FL", "FR", "FC", "LFE", "SL", "SR"]:
        ap.error("Currently requires 5.1(side) input")
    n = decoded["original"]["frames"]
    for variant in mixed["variants"].values():
        for ch, file in variant["files"].items():
            with sf.SoundFile(args.work / file) as f:
                if f.frames != n:
                    raise ValueError(f"Full-length channel mismatch: {file}")
            if ch != "FC" and file != orig["files"][ch]:
                raise ValueError("Preview expects only centre processing")
    st = scenes.stats(args.work, decoded, voiced)
    chosen = scenes.pick(st, args.length, args.count)
    # Add excerpts around synchronization boundaries when present.
    for segment in m["sync"]["result"]["segments"][1:]:
        start = max(0, int(segment["start"] / 48000) - args.length // 2)
        if all(abs(start - c["start"]) >= args.length for c in chosen):
            chosen.append(scenes.manual(st, str(start), args.length))
    args.out.mkdir(parents=True)
    report = {"mix": mixed, "sync": m["sync"]["result"], "voice": voiced,
              "decoded_duration_s": n / 48000, "scenes": [],
              "stereo_downmix": "L=FL+0.7071*FC+0.7071*SL; R=FR+0.7071*FC+0.7071*SR; LFE omitted; shared attenuation per scene"}
    for i, scene in enumerate(chosen, 1):
        start, length = scene["start"], scene["length"]
        frames = length * 48000
        base = f"{i:02d}_{scene['kind']}_{start // 3600:02d}-{start % 3600 // 60:02d}-{start % 60:02d}"
        signals = {}
        sources = {**{k: v["files"] for k, v in mixed["variants"].items()}, "original": orig["files"]}
        encoded = []
        for name, files in sources.items():
            data = {ch: read_excerpt(args.work / files[ch], start * 48000, frames) for ch in channels}
            signals[name] = np.column_stack([data["FL"] + 0.7071 * (data["FC"] + data["SL"]),
                                            data["FR"] + 0.7071 * (data["FC"] + data["SR"])])
            dst = args.out / f"{base}_{name}.eac3"
            cmd = [cfg["tools"]["ffmpeg"], "-hide_banner", "-loglevel", "error", "-n"]
            for ch in channels:
                cmd += ["-ss", str(start), "-t", str(length), "-i", str(args.work / files[ch])]
            mapping = "|".join(f"{j}.0-{ch}" for j, ch in enumerate(channels))
            cmd += ["-filter_complex", f"join=inputs=6:channel_layout=5.1(side):map={mapping}",
                    "-t", str(length), "-c:a", "eac3", "-b:a", cfg["export"]["bitrate"],
                    "-dialnorm", str(dialnorm), str(dst)]
            tools.run(cmd)
            probe = tools.run([cfg["tools"]["ffprobe"], "-v", "error", "-show_streams", "-show_format", "-of", "json", dst])
            info = json.loads(probe.stdout)
            stream = info["streams"][0]
            assert stream["codec_name"] == "eac3" and stream["channels"] == 6
            assert abs(float(info["format"]["duration"]) - length) < 0.05
            encoded.append((name, dst))
        signals["source_russian"] = np.column_stack([read_excerpt(args.work / m["sync"]["result"]["files"][ch], start * 48000, frames) for ch in ["FL", "FR"]])
        peak = max(float(np.abs(x).max()) for x in signals.values())
        gain = min(1.0, 10 ** (-2 / 20) / max(peak, 1e-12))
        for name, data in signals.items():
            path = args.out / f"{base}_{name}_stereo.wav"
            sf.write(path, data * gain, 48000, subtype="PCM_16")
            with sf.SoundFile(path) as f:
                assert f.frames == frames and f.samplerate == 48000 and f.channels == 2
        container = args.out / f"{base}_comparison.mka"
        cmd = [cfg["tools"]["mkvmerge"], "-q", "-o", container]
        for name, path in encoded:
            title = f"{name} - E-AC3 5.1" if name != "original" else "Original 5.1 (re-encoded, no Atmos)"
            cmd += ["--track-name", f"0:{title}", "--language", f"0:{'eng' if name == 'original' else 'rus'}",
                    "--default-track-flag", f"0:{'yes' if name == 'main' else 'no'}", path]
        tools.run(cmd, ok_codes=(0, 1))
        info = tools.ffprobe_streams(cfg, container)
        assert len(info["streams"]) == len(encoded)
        assert all(s["codec_name"] == "eac3" and s["channels"] == 6 for s in info["streams"])
        assert abs(float(info["format"]["duration"]) - length) < 0.05
        report["scenes"].append({**scene, "base": base, "stereo_gain_db": round(float(20 * np.log10(gain)), 2)})
        print(f"Ready: {base}", flush=True)
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(scenes.describe(chosen))


if __name__ == "__main__":
    main()
