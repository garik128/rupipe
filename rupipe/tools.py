"""Thin wrappers around ffmpeg / ffprobe / mkvmerge."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class ToolError(RuntimeError):
    pass


def run(cmd: list[str], capture: bool = True, ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    """Run a tool; mkvmerge returns 1 for warnings, so callers can widen ok_codes."""
    log.debug("run: %s", subprocess.list2cmdline([str(c) for c in cmd]))
    proc = subprocess.run([str(c) for c in cmd], capture_output=capture)
    if proc.returncode == 1 and 1 in ok_codes and proc.stdout:
        log.warning("%s: %s", Path(str(cmd[0])).name, proc.stdout.decode("utf-8", "replace").strip()[-2000:])
    if proc.returncode not in ok_codes:
        err = proc.stderr.decode("utf-8", "replace") if capture else ""
        raise ToolError(f"{Path(str(cmd[0])).name} failed ({proc.returncode}):\n{err[-4000:]}")
    return proc


INSTALL_HINT = {
    "ffmpeg": "ffmpeg (with ffprobe): https://ffmpeg.org/download.html",
    "ffprobe": "ffmpeg (with ffprobe): https://ffmpeg.org/download.html",
    "mkvmerge": "MKVToolNix: https://mkvtoolnix.download/downloads.html",
}


def resolve_tool(value: str) -> str | None:
    """A configured tool: an existing file path, or a program name looked up in PATH."""
    if Path(value).is_file():
        return str(value)
    return shutil.which(value)


def check_tools(cfg: dict) -> None:
    """Replace configured tool names with full paths; fail with an install hint if missing."""
    for name, value in cfg["tools"].items():
        found = resolve_tool(str(value))
        if not found:
            raise ToolError(f"tool '{name}' not found ('{value}' is neither a file nor in PATH). "
                            f"Install {INSTALL_HINT.get(name, name)} or set tools.{name} in config.yaml")
        cfg["tools"][name] = found


def ffprobe_streams(cfg: dict, path: Path) -> dict:
    proc = run([
        cfg["tools"]["ffprobe"], "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ])
    return json.loads(proc.stdout.decode("utf-8"))


def first_audio_frames(cfg: dict, path: Path, stream_index: int, fmt: str, frames: int = 4) -> bytes:
    """Raw bitstream of the first frames of a stream, without decoding."""
    proc = run([
        cfg["tools"]["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error",
        "-i", path, "-map", f"0:{stream_index}", "-c", "copy",
        "-frames:a", str(frames), "-f", fmt, "-",
    ])
    return proc.stdout
