"""`pipeline doctor`: check that everything the pipeline needs is present."""
from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import tools

GB = 1024 ** 3


def _line(ok: bool, what: str, detail: str) -> bool:
    print(f"[{'OK' if ok else 'FAIL'}] {what}: {detail}")
    return ok


def _free_space(path: Path) -> int:
    p = path.resolve()
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free


def run(cfg: dict) -> int:
    ok = True
    v = sys.version_info
    ok &= _line(v >= (3, 10), "python", f"{v.major}.{v.minor}.{v.micro} (need 3.10+)")

    for mod, pip_name in (("numpy", "numpy"), ("scipy", "scipy"), ("soundfile", "soundfile"), ("yaml", "pyyaml")):
        try:
            m = importlib.import_module(mod)
            ok &= _line(True, f"python package {pip_name}", getattr(m, "__version__", "installed"))
        except ImportError:
            ok &= _line(False, f"python package {pip_name}", "missing: pip install -r requirements.txt")

    for name, value in cfg["tools"].items():
        found = tools.resolve_tool(str(value))
        if not found:
            ok &= _line(False, name, f"not found ('{value}'). Install {tools.INSTALL_HINT.get(name, name)} "
                                     f"or set tools.{name} in config.yaml")
            continue
        try:
            out = subprocess.run([found, "-version" if name != "mkvmerge" else "--version"],
                                 capture_output=True, timeout=30).stdout.decode("utf-8", "replace")
            ok &= _line(True, name, f"{found} - {out.strip().splitlines()[0][:80] if out.strip() else 'runs'}")
        except Exception as e:  # noqa: BLE001 - report any failure to start the tool
            ok &= _line(False, name, f"{found} does not run: {e}")
            continue
        if name == "ffmpeg":
            enc = subprocess.run([found, "-hide_banner", "-encoders"], capture_output=True,
                                 timeout=30).stdout.decode("utf-8", "replace")
            ok &= _line(" eac3 " in enc, "ffmpeg E-AC3 encoder", "present" if " eac3 " in enc else
                        "missing: use a full ffmpeg build")

    for key in ("work_dir", "output_dir"):
        free = _free_space(Path(cfg[key]))
        ok &= _line(free > 20 * GB, f"free space for {key}", f"{free / GB:.0f} GB at {Path(cfg[key]).resolve()} "
                    "(about 8 GB per hour of 5.1 audio in work_dir; a copy of each video in output_dir)")

    print("\nAll checks passed." if ok else "\nFix the FAIL lines above, then run doctor again.")
    return 0 if ok else 1
