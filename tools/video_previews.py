"""Copy short video excerpts and add already mixed E-AC3 5.1 variants.

No full movie copy or video re-encoding. Each excerpt starts at a source
keyframe; audio is cut at that same source timestamp. Output must be new.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rupipe import tools
from rupipe.config import load_config


def packets(cfg, path, interval):
    return json.loads(tools.run([cfg["tools"]["ffprobe"], "-v", "error", "-read_intervals", interval,
        "-select_streams", "v:0", "-show_packets", "-show_data_hash", "sha256",
        "-show_entries", "packet=pts_time,flags,data_hash", "-of", "json", path]).stdout)["packets"]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("work", type=Path)
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--config", type=Path)
    ap.add_argument("--at", required=True, action="append", type=float, help="Source time in seconds; repeatable")
    ap.add_argument("--length", type=int, default=90)
    ap.add_argument("--default", default="main", help="Default mix variant")
    args = ap.parse_args()
    if args.out.exists():
        ap.error("Choose a new output directory")
    if args.length < 1 or any(t < 0 for t in args.at):
        ap.error("Positive length and nonnegative start times required")
    cfg = load_config(args.config)
    manifest = json.loads((args.work / "manifest.json").read_text(encoding="utf-8"))
    source_stat = args.source.stat()
    recorded = manifest["source"]
    if source_stat.st_size != recorded["size"] or int(source_stat.st_mtime) != recorded["mtime"]:
        ap.error("Source differs from the processed file")
    if args.source.resolve() != Path(recorded["input"]).resolve():
        ap.error("Source path differs from the processed file")
    m = manifest["steps"]
    analysis = m["analyze"]["result"]
    variants = m["mix"]["result"]["variants"]
    if args.default not in variants:
        ap.error("Default variant is absent")
    orig = next(s for s in analysis["streams"] if s["index"] == analysis["original"])
    if abs(orig["start_time"]) > 0.001:
        ap.error("This preview tool currently requires original audio to start at zero")
    video_info = next(s for s in tools.ffprobe_streams(cfg, args.source)["streams"] if s["codec_type"] == "video")
    args.out.mkdir(parents=True)
    report = []
    for i, requested in enumerate(args.at, 1):
        keys = [p for p in packets(cfg, args.source, f"{requested}%+30")
                if "K" in p["flags"] and float(p["pts_time"]) >= requested]
        if not keys:
            raise ValueError("No keyframe found in 30 seconds after requested time")
        key = keys[0]
        start = float(key["pts_time"])
        if start + args.length > m["decode"]["result"]["original"]["frames"] / 48000:
            raise ValueError("Excerpt exceeds decoded audio duration")
        base = f"{i:02d}_{int(start) // 3600:02d}-{int(start) % 3600 // 60:02d}-{int(start) % 60:02d}"
        video = args.out / f"{base}_video.mkv"
        def timestamp(seconds):
            return f"{int(seconds // 3600):02d}:{int(seconds % 3600 // 60):02d}:{seconds % 60:06.3f}"
        tools.run([cfg["tools"]["mkvmerge"], "-q", "-o", video,
                   "--no-audio", "--no-subtitles", "--no-attachments", "--no-chapters",
                   "--split", f"parts:{timestamp(requested)}-{timestamp(start + args.length)}",
                   args.source], ok_codes=(0, 1))
        first = packets(cfg, video, "%+1")[0]
        if first["data_hash"] != key["data_hash"] or abs(float(first["pts_time"])) > 0.002:
            raise ValueError("Video keyframe/timestamp validation failed")
        length = float(tools.ffprobe_streams(cfg, video)["format"]["duration"])
        if abs(length - args.length) > 30 or start + length > m["decode"]["result"]["original"]["frames"] / 48000:
            raise ValueError("Unexpected video length or insufficient decoded audio")
        encoded = []
        for name, variant in variants.items():
            channels = variant["channels"]
            dst = args.out / f"{base}_{name}.eac3"
            cmd = [cfg["tools"]["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error", "-n"]
            for ch in channels:
                cmd += ["-ss", str(start), "-t", str(length), "-i", args.work / variant["files"][ch]]
            mapping = "|".join(f"{j}.0-{ch}" for j, ch in enumerate(channels))
            cmd += ["-filter_complex", f"join=inputs={len(channels)}:channel_layout={variant['layout']}:map={mapping}",
                    "-t", str(length), "-c:a", "eac3", "-b:a", cfg["export"]["bitrate"],
                    "-dialnorm", str(orig["dialnorm"] or -31), dst]
            tools.run(cmd)
            encoded.append((name, variant, dst))
        output = args.out / f"{base}_comparison.mkv"
        cmd = [cfg["tools"]["mkvmerge"], "-q", "-o", output, video]
        # Preferred variant first; explicit default flags avoid player ambiguity.
        encoded.sort(key=lambda item: item[0] != args.default)
        for name, variant, dst in encoded:
            title = f"Русский 5.1 — {variant['voice_db']:+g} дБ к студии"
            cmd += ["--language", "0:rus", "--track-name", f"0:{title}",
                    "--default-track-flag", f"0:{'yes' if name == args.default else 'no'}", dst]
        tools.run(cmd, ok_codes=(0, 1))
        info = tools.ffprobe_streams(cfg, output)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        for field in ["codec_name", "width", "height", "pix_fmt", "color_space", "color_transfer", "color_primaries"]:
            assert v.get(field) == video_info.get(field), field
        audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
        assert len(audio) == len(variants)
        assert all(s["codec_name"] == "eac3" and s["channels"] == 6 and abs(float(s["start_time"])) < 0.002 for s in audio)
        assert abs(float(info["format"]["duration"]) - length) < 0.05
        final_first = packets(cfg, output, "%+1")[0]
        assert final_first["data_hash"] == key["data_hash"] and abs(float(final_first["pts_time"])) < 0.002
        report.append({"file": output.name, "requested_s": requested, "actual_source_start_s": start,
                       "duration_s": float(info["format"]["duration"]), "video_keyframe_sha256": key["data_hash"],
                       "default_variant": args.default, "voice_db": {n: v["voice_db"] for n, v, _ in encoded}})
        print(f"Ready: {output.name} (source {start:.3f}s)", flush=True)
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
