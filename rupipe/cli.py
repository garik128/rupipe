"""Command line entry point: `pipeline probe|run ...`."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import decode, doctor, export, mix, probe, scenes, sync, tools, voice
from .config import load_config
from .manifest import Manifest

log = logging.getLogger("rupipe")

MEDIA_EXT = {".mkv", ".mka"}
STEPS = ["analyze", "decode", "sync", "voice", "mix", "encode", "mux"]


def _setup_logging(logfile: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in root.handlers):
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(fmt)
        root.addHandler(h)
    for h in [h for h in root.handlers if isinstance(h, logging.FileHandler)]:
        root.removeHandler(h)
        h.close()
    if logfile:
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


def _inputs(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(p for p in path.iterdir() if p.suffix.lower() in MEDIA_EXT)
    return [path]


def run_file(cfg: dict, src: Path, start: str | None, until: str | None, plan: dict) -> None:
    """Process one file; `plan` is its analysis with both tracks already resolved."""
    work = Path(cfg["work_dir"]) / src.stem
    work.mkdir(parents=True, exist_ok=True)
    _setup_logging(work / "pipeline.log")
    log.info("=== %s", src)
    m = Manifest(work, src)
    if m.done("analyze"):
        old = m.result("analyze")
        if (old.get("original"), old.get("russian")) != (plan["original"], plan["russian"]):
            log.info("track choice changed, starting over")
            m.invalidate_from(STEPS, "analyze")
    if start:
        m.invalidate_from(STEPS, start)

    last = STEPS.index(until) if until else len(STEPS) - 1
    for step in STEPS[: last + 1]:
        if m.done(step):
            log.info("[%s] already done, skipping", step)
            continue
        log.info("[%s] start", step)
        t0 = time.time()
        if step == "analyze":
            result = plan
            log.info("\n%s", probe.format_table(result))
        elif step == "decode":
            result = decode.decode(cfg, src, m.result("analyze"), work)
        elif step == "sync":
            result = sync.sync(cfg, m.result("decode"), work)
        elif step == "voice":
            result = voice.extract(cfg, m.result("decode"), m.result("sync"), work)
        elif step == "mix":
            result = mix.mix(cfg, m.result("decode"), m.result("voice"), work)
        elif step == "encode":
            result = export.encode(cfg, m.result("analyze"), m.result("mix"), work)
        elif step == "mux":
            result = export.mux(cfg, src, m.result("analyze"), m.result("encode"), work)
        m.mark(step, result, time.time() - t0)
        log.info("[%s] done in %.1fs", step, time.time() - t0)


def fragments_file(cfg: dict, src: Path, length: int | None, count: int, at: list[str] | None) -> None:
    work = Path(cfg["work_dir"]) / src.stem
    m = Manifest(work, src)
    if not m.done("mux"):
        raise RuntimeError(f"{src.name}: run the pipeline first (step mux is not done)")
    length = length or cfg["fragments"]["length_s"]
    st = scenes.stats(work, m.result("decode"), m.result("voice"))
    if at:
        chosen = [scenes.manual(st, t, length) for t in at]
    else:
        chosen = scenes.pick(st, length, count)
    text = scenes.describe(chosen)
    print(f"{src.name}\n{text}\n")
    out_dir = Path(cfg["fragments"]["dir"])
    files = scenes.cut(cfg, m.result("mux"), m.result("analyze"), chosen, out_dir)
    note = out_dir / f"{src.stem}.fragments.txt"
    with open(note, "a", encoding="utf-8") as fh:  # appended: --at runs add to earlier picks
        fh.write(text + "\n\n" + "\n".join(f.name for f in files) + "\n\n")


def resolve_dirs(cfg: dict, out: str | None, input_path: Path | None) -> None:
    """Fill output_dir, work_dir and fragments.dir.

    --out wins. Otherwise empty values in the config mean "rupipe-out" next to the
    input (the folder itself for a folder, the file's folder for a file), or in the
    current folder when there is no input (doctor).
    """
    if out:
        base = Path(out)
        cfg["output_dir"], cfg["work_dir"] = str(base), str(base / "work")
        cfg["fragments"]["dir"] = str(base / "listen")
        return
    if input_path is None:
        root = Path.cwd()
    else:
        root = input_path if input_path.is_dir() else input_path.parent
    base = root / "rupipe-out"
    if not cfg.get("output_dir"):
        cfg["output_dir"] = str(base)
    if not cfg.get("work_dir"):
        cfg["work_dir"] = str(base / "work")
    if not cfg["fragments"].get("dir"):
        cfg["fragments"]["dir"] = str(base / "listen")


def tracks_for(cfg: dict, f: Path, per_file: list[str]) -> dict:
    """Config with the track choice for one file.

    Order of precedence: --track-for rules (last match wins), then tracks.files in
    the config (keys are parts of file names), then tracks.original/russian.
    A rule looks like "E03=russian:Studio" or "E03=3" (shorthand for the Russian track).
    """
    tracks = {"original": cfg["tracks"]["original"], "russian": cfg["tracks"]["russian"]}
    rules = [(k, v) for k, v in (cfg["tracks"].get("files") or {}).items()]
    for rule in per_file:
        if "=" not in rule:
            raise ValueError(f"--track-for expects PART_OF_NAME=CHOICE, got '{rule}'")
        k, v = rule.split("=", 1)
        if ":" in v and v.split(":", 1)[0] in ("russian", "original"):
            role, choice = v.split(":", 1)
            v = {role: choice}
        rules.append((k, v))
    for key, value in rules:
        if key.lower() in f.name.lower():
            tracks.update(value if isinstance(value, dict) else {"russian": value})
    out = dict(cfg)
    out["tracks"] = {**cfg["tracks"], **tracks}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pipeline")
    ap.add_argument("--config", help="YAML file overriding config.yaml")
    ap.add_argument("--out", help="folder for results: <out> (finished files), <out>/work (temporary), "
                                  "<out>/listen (fragments); overrides output_dir, work_dir, fragments.dir")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check Python packages, ffmpeg/ffprobe/mkvmerge and free disk space")
    pl = sub.add_parser("plan", help="show every audio track of every file and what would be used; "
                                     "processes nothing")
    p = sub.add_parser("probe", help="same as plan")
    r = sub.add_parser("run", help="process a file or every .mkv/.mka in a folder")
    for sp in (pl, p, r):
        sp.add_argument("path", type=Path)
        sp.add_argument("--russian", help="Russian track: number or part of the title")
        sp.add_argument("--original", help="original track: number or part of the title")
        sp.add_argument("--track-for", action="append", default=[], metavar="NAME=CHOICE",
                        help="choice for files whose name contains NAME, e.g. E03=4 or "
                             "E03=russian:Studio or E03=original:2 (repeatable)")
    r.add_argument("--from", dest="start", choices=STEPS, help="redo this step and everything after it")
    r.add_argument("--until", choices=STEPS, help="stop after this step")

    fr = sub.add_parser("fragments", help="pick contrasting scenes and cut them from the finished file")
    fr.add_argument("path", type=Path)
    fr.add_argument("--length", type=int, help="fragment length, seconds (default: fragments.length_s)")
    fr.add_argument("--count", type=int, default=4, help="number of fragments, 1-4 (default 4)")
    fr.add_argument("--at", action="append", help="cut from this time instead of picking (HH:MM:SS, repeatable)")

    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = load_config(args.config)
    for key in ("russian", "original"):
        if getattr(args, key, None):
            cfg["tracks"][key] = getattr(args, key)
    resolve_dirs(cfg, args.out, getattr(args, "path", None))
    _setup_logging()
    if args.cmd == "doctor":
        return doctor.run(cfg)
    try:
        tools.check_tools(cfg)
    except tools.ToolError as e:
        log.error("%s\nRun `pipeline doctor` for a full check.", e)
        return 1

    files = _inputs(args.path)
    if not files:
        log.error("no .mkv/.mka files in %s", args.path)
        return 1

    if args.cmd in ("plan", "probe", "run"):
        # Plan first: resolve the tracks of every file before touching any of them.
        cfgs = {f: tracks_for(cfg, f, args.track_for) for f in files}
        plans = {f: probe.analyze(cfgs[f], f, strict=False) for f in files}
        unresolved = [f for f, pl_ in plans.items() if pl_["problems"]]
        if args.cmd in ("plan", "probe"):
            for f in files:
                print(f"{f.name}\n{probe.format_table(plans[f])}\n")
            print(f"{len(files) - len(unresolved)} of {len(files)} file(s) resolved"
                  + (f"; needs a choice: {', '.join(f.name for f in unresolved)}" if unresolved else ""))
            return 2 if unresolved else 0
        print(plan_text(plans))
        if unresolved:
            print("\nNothing was processed: choose the tracks (--russian / --original / --track-for) "
                  "and run again.")
            return 2

    failed = []
    for f in files:
        try:
            if args.cmd == "fragments":
                fragments_file(cfg, f, args.length, args.count, args.at)
            else:
                run_file(cfgs[f], f, args.start, args.until, plans[f])
        except Exception:
            log.exception("%s failed", f.name)
            failed.append(f)
    if args.cmd == "run":
        print("\n" + summary_text(cfg, files, failed))
    return 1 if failed else 0


def plan_text(plans: dict) -> str:
    lines = ["PLAN"]
    for f, p in plans.items():
        by_index = {s["index"]: s for s in p["streams"]}
        lines.append(f"  {f.name}")
        for role in ("original", "russian"):
            idx = p[role]
            lines.append(f"    {role:<8} " + (probe._describe(by_index[idx]) if idx is not None else "?"))
        for problem in p["problems"]:
            lines.append("    NEEDS A CHOICE: " + problem.replace("\n", "\n    "))
    return "\n".join(lines)


def summary_text(cfg: dict, files: list[Path], failed: list[Path]) -> str:
    """Per-file check list after a run: what to look at before trusting the result."""
    lines = ["SUMMARY"]
    for f in files:
        if f in failed:
            lines.append(f"  {f.name}: FAILED, see the log")
            continue
        m = Manifest(Path(cfg["work_dir"]) / f.stem, f)
        if not m.done("sync"):
            lines.append(f"  {f.name}: not finished")
            continue
        sy = m.result("sync")
        segs = sy["segments"]
        line = f"  {f.name}: sync {len(segs)} segment(s)"
        if len(segs) > 1:
            line += " [" + ", ".join(f"{s['lag']:.0f}" for s in segs) + " samples]"
        if sy.get("flagged"):
            line += " FLAGGED: " + sy.get("reason", "")
        if m.done("voice"):
            v = m.result("voice")
            mid = v["leak_db_per_band"][3:10]  # bands 240 Hz - 5.4 kHz
            line += (f"; leak 240-5400 Hz worst {max(mid):.0f} dB; studio ducking {v['duck_depth_db']:.1f} dB; "
                     f"voice {v['voice_seconds'] / 60:.0f} min")
        if m.done("mix"):
            mx = m.result("mix")
            lim = sum(st["samples_limited"] for var in mx["variants"].values()
                      for st in var["stats"].values())
            main = mx["variants"].get(cfg["export"]["default_variant"]) or next(iter(mx["variants"].values()))
            if main.get("audible") is not None and mx.get("studio_audible") is not None:
                line += (f"; voice audible {main['audible'] * 100:.0f}% (studio {mx['studio_audible'] * 100:.0f}%), "
                         f"voice {main['voice_db']:+.1f} dB to studio, "
                         f"over centre {main['voice_over_original_db']:+.1f} dB")
            line += f"; limited {lim} samples"
        lines.append(line)
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
