"""Make separate video files for each audio track of comparison previews.

Copy only; originals remain untouched. Output directory must be new.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rupipe import tools
from rupipe.config import load_config


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path)
    ap.add_argument("--pattern", default="*_context.mkv")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    if args.out.exists():
        ap.error("Output directory must be new")
    sources = sorted(args.source.glob(args.pattern)) if args.source.is_dir() else [args.source]
    if not sources:
        ap.error("No previews found")
    cfg = load_config()
    args.out.mkdir(parents=True)
    for source in sources:
        tracks = json.loads(tools.run([cfg["tools"]["mkvmerge"], "-J", source]).stdout.decode("utf-8-sig"))["tracks"]
        audio = [t for t in tracks if t["type"] == "audio"]
        videos = [t for t in tracks if t["type"] == "video"]
        assert len(videos) == 1 and len(audio) >= 1
        folder = args.out / source.stem.removesuffix("_tiger_context")
        folder.mkdir()
        original = tools.ffprobe_streams(cfg, source)
        notes = []
        for i, track in enumerate(audio, 1):
            title = track["properties"].get("track_name", f"Audio {track['id']}")
            safe = re.sub(r'[<>:"/\\|?*]', "_", title).strip(" .")
            dst = folder / f"{i:02d}_{safe}.mkv"
            tools.run([cfg["tools"]["mkvmerge"], "-q", "-o", dst, "--audio-tracks", str(track["id"]),
                       "--default-track-flag", f"{track['id']}:yes", "--no-subtitles", "--no-attachments",
                       "--no-chapters", source], ok_codes=(0, 1))
            result = tools.ffprobe_streams(cfg, dst)
            result_audio = [s for s in result["streams"] if s["codec_type"] == "audio"]
            assert len(result_audio) == 1 and len(result["streams"]) == 2
            assert result_audio[0]["disposition"]["default"] == 1
            expected_audio = [s for s in original["streams"] if s["codec_type"] == "audio"][i - 1]
            assert result_audio[0]["codec_name"] == expected_audio["codec_name"]
            assert result_audio[0]["channels"] == expected_audio["channels"]
            assert abs(float(result["format"]["duration"]) - float(original["format"]["duration"])) < 0.06
            notes.append(f"{dst.name}: {title}")
            print(f"Ready: {folder.name}/{dst.name}", flush=True)
        (folder / "README.txt").write_text("Запускайте файлы по очереди. В каждом одна звуковая дорожка.\n\n" + "\n".join(notes), encoding="utf-8")


if __name__ == "__main__":
    main()
