"""Compare cleaned voices in video scenes with both unprocessed source tracks.

Uses existing cleaned excerpts, performs no neural inference, and leaves the
source, pipeline manifest and existing previews unchanged. Encoding runs on CPU.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rupipe import tools
from rupipe.config import load_config
from rupipe.export import _tracks
from rupipe.mix import limit
from rupipe.voice import _coef, _one_pole
from video_previews import packets


def read(path, start, frames):
    with sf.SoundFile(path) as f:
        assert f.samplerate == 48000 and f.channels == 1
        f.seek(start)
        x = f.read(frames, dtype="float32")
    assert len(x) == frames and np.isfinite(x).all()
    return x


def timestamp(s):
    return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("work", type=Path)
    ap.add_argument("--clean", required=True, type=Path)
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--config", type=Path)
    ap.add_argument("--variant", default="moderate")
    args = ap.parse_args()
    if args.out.exists():
        ap.error("Output directory must be new")
    cfg = load_config(args.config)
    manifest = json.loads((args.work / "manifest.json").read_text(encoding="utf-8"))
    assert args.source.resolve() == Path(manifest["source"]["input"]).resolve()
    assert args.source.stat().st_size == manifest["source"]["size"]
    assert int(args.source.stat().st_mtime) == manifest["source"]["mtime"]
    m = manifest["steps"]
    analysis = m["analyze"]["result"]
    orig = m["decode"]["result"]["original"]
    variant = m["mix"]["result"]["variants"][args.variant]
    for channel, file in variant["files"].items():
        if channel != "FC":
            assert file == orig["files"][channel]
    assert orig["channels"] == ["FL", "FR", "FC", "LFE", "SL", "SR"]
    assert cfg["mix"]["variants"][args.variant]["duck_channels"] == ["FC"]
    streams = tools.ffprobe_streams(cfg, args.source)["streams"]
    source_audio = [s for s in streams if s["codec_type"] == "audio"]
    ids = [t["id"] for t in _tracks(cfg, args.source) if t["type"] == "audio"]
    id_map = {s["index"]: track for s, track in zip(source_audio, ids)}
    ru_id, en_id = [id_map[analysis[k]] for k in ["russian", "original"]]
    original_stream = next(s for s in analysis["streams"] if s["index"] == analysis["original"])
    assert abs(original_stream["start_time"]) < 0.001
    times, vad = np.load(args.work / "vad.npy")
    step = float(times[1] - times[0]) / 48000
    duck = _one_pole(np.where(vad > 0.5, 10**(variant["duck_db"] / 20), 1.0),
                     _coef(cfg["mix"]["duck_attack_ms"], step), _coef(cfg["mix"]["duck_release_ms"], step))
    trials = json.loads((args.clean / "report.json").read_text(encoding="utf-8"))
    args.out.mkdir(parents=True)
    reports = []
    for row in trials["excerpts"]:
        begin, end = row["source_start_s"], row["source_start_s"] + row["duration_s"]
        keys = [p for p in packets(cfg, args.source, f"{begin}%+{row['duration_s'] + 5}")
                if "K" in p["flags"] and begin <= float(p["pts_time"]) < end - 0.05]
        assert len(keys) >= 2
        full_voice = read(args.work / "voice_C.wav", round(begin * 48000), round(row["duration_s"] * 48000))
        cells = full_voice[:len(full_voice) // 4800 * 4800].reshape(-1, 4800)
        power = np.mean(cells.astype(np.float64)**2, axis=1)
        audible = np.flatnonzero(power > max(10**(-45 / 10), power.max() * 0.001))
        onset = begin + (int(audible[0]) * 0.1 if len(audible) else 0)
        possible = [k for k in keys[:-1] if float(k["pts_time"]) <= onset]
        key = possible[-1] if possible else keys[0]
        start = float(key["pts_time"])
        last = float(keys[-1]["pts_time"])
        base = row["base"] + "_tiger_context"
        context = args.out / f"{base}_source.mkv"
        tools.run([cfg["tools"]["mkvmerge"], "-q", "-o", context,
                   "--audio-tracks", f"{ru_id},{en_id}", "--no-subtitles", "--no-attachments", "--no-chapters",
                   "--split", f"parts:{timestamp(start - 0.001)}-{timestamp(last - 0.001)}", args.source], ok_codes=(0, 1))
        first = packets(cfg, context, "%+1")[0]
        assert first["data_hash"] == key["data_hash"] and abs(float(first["pts_time"])) < 0.002
        length = float(tools.ffprobe_streams(cfg, context)["format"]["duration"])
        assert start + length <= end + 0.002
        frames = round(length * 48000)
        clean = read(args.clean / f"{row['base']}_clean_float.wav", round((start - begin) * 48000), frames)
        fc = read(args.work / orig["files"]["FC"], round(start * 48000), frames)
        envelope = np.interp(np.arange(frames) + start * 48000, times, duck).astype(np.float32)
        after, overs = limit(fc * envelope + clean * np.float32(10**(variant["voice_db"] / 20)),
                             10**(cfg["mix"]["ceiling_dbfs"] / 20))
        centre = args.out / f"{base}_clean_FC.wav"
        sf.write(centre, after, 48000, subtype="FLOAT")
        audios = []
        for name in ["before", "tiger"]:
            cmd = [cfg["tools"]["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error", "-n", "-threads", "2"]
            for ch in orig["channels"]:
                if name == "tiger" and ch == "FC":
                    cmd += ["-i", centre]
                else:
                    cmd += ["-ss", str(start), "-t", str(length), "-i", args.work / variant["files"][ch]]
            mapping = "|".join(f"{i}.0-{ch}" for i, ch in enumerate(orig["channels"]))
            dst = args.out / f"{base}_{name}.eac3"
            cmd += ["-filter_complex_threads", "2", "-filter_complex", f"join=inputs=6:channel_layout=5.1(side):map={mapping}",
                    "-t", str(length), "-c:a", "eac3", "-b:a", cfg["export"]["bitrate"],
                    "-dialnorm", str(original_stream["dialnorm"] or -31), dst]
            tools.run(cmd)
            audios.append(dst)
        tracks = _tracks(cfg, context)
        v_id = next(t["id"] for t in tracks if t["type"] == "video")
        clip_audios = [t for t in tracks if t["type"] == "audio"]
        assert len(clip_audios) == 2
        selected = sorted([ru_id, en_id])
        ru_clip = clip_audios[selected.index(ru_id)]["id"]
        en_clip = clip_audios[selected.index(en_id)]["id"]
        final = args.out / f"{base}.mkv"
        cmd = [cfg["tools"]["mkvmerge"], "-q", "-o", final,
               "--track-name", f"{ru_clip}:Исходная русская — стерео", "--default-track-flag", f"{ru_clip}:yes",
               "--track-name", f"{en_clip}:Английский оригинал", "--default-track-flag", f"{en_clip}:no", context]
        level = f"{variant['voice_db']:+g} дБ"
        for title, path in zip([f"Наш микс {level}", f"Наш микс {level} + TIGER"], audios):
            cmd += ["--track-name", f"0:{title}", "--language", "0:rus", "--default-track-flag", "0:no", path]
        cmd += ["--track-order", f"0:{v_id},0:{ru_clip},1:0,2:0,0:{en_clip}"]
        tools.run(cmd, ok_codes=(0, 1))
        final_info = tools.ffprobe_streams(cfg, final)
        assert len(final_info["streams"]) == 5
        assert abs(float(final_info["format"]["duration"]) - length) < 0.06
        final_key = packets(cfg, final, "%+1")[0]
        assert final_key["data_hash"] == key["data_hash"] and abs(float(final_key["pts_time"])) < 0.002
        reports.append({"file": final.name, "source_start_s": start, "duration_s": length,
                        "measured_voice_onset_s": round(onset - start, 3), "tiger_limited_samples": overs})
        (args.out / "report.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Ready: {final.name} ({length:.3f}s)", flush=True)


if __name__ == "__main__":
    main()
