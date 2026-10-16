"""Select speech-rich scenes with loud surroundings and residual pause audio.

Scores identify listening candidates, not verified noise or dialogue labels.
Exclude the final 10% to reduce selections from end credits and songs.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rupipe import scenes


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("work", type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--length", type=int, default=30)
    args = ap.parse_args()
    if args.out.exists():
        ap.error("Choose a new report path")
    m = json.loads((args.work / "manifest.json").read_text(encoding="utf-8"))["steps"]
    st = scenes.stats(args.work, m["decode"]["result"], m["voice"]["result"])
    raw, _ = scenes._per_second_power(args.work / "voice_raw.wav")
    candidates = []
    for start in range(120, int(len(st["total"]) * 0.9) - args.length, 5):
        sl = slice(start, start + args.length)
        speech = float(st["speech"][sl].mean())
        pauses = st["speech"][sl] < 0.1
        if speech < 0.5:
            continue
        candidates.append({"start_s": start, "length_s": args.length, "speech_fraction": round(speech, 3),
                           "surrounding_power_db": round(float(10 * np.log10(st["sides"][sl].mean() + st["lfe"][sl].mean() + 1e-20)), 2),
                           "raw_pause_power_db": round(float(10 * np.log10(raw[sl][pauses].mean() + 1e-20)), 2) if pauses.sum() >= 4 else -200,
                           "pause_seconds": int(pauses.sum())})
    loud = max(candidates, key=lambda c: c["surrounding_power_db"])
    remaining = [c for c in candidates if abs(c["start_s"] - loud["start_s"]) >= args.length]
    residual = max(remaining, key=lambda c: c["raw_pause_power_db"])
    report = {"dense_speech": loud, "residual_candidate": residual,
              "note": "Selections use energy and existing VAD; listen to the source to verify speech, music/effects and actual residuals."}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
