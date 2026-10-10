# Setup

Run `pipeline doctor` (see SKILL.md) before and after installing anything; it tells you exactly what is missing.

## What is needed

| what | why | check |
|---|---|---|
| Python 3.10 or newer | runs the tool | `python --version` |
| Python packages numpy, scipy, soundfile, pyyaml | audio math and file I/O | `pipeline doctor` |
| ffmpeg and ffprobe, with the E-AC3 encoder | decoding and encoding audio | `ffmpeg -hide_banner -encoders` lists `eac3` |
| MKVToolNix (mkvmerge) | reading and writing MKV | `mkvmerge --version` |
| Disk space | temporary files: about 8 GB per hour of video; plus a copy of each video in the output | `pipeline doctor` |
| RAM | 8 GB is enough for episodes; 16 GB recommended for films over 1.5 hours | - |

No GPU and no internet connection are needed to run it (only to install).

## Python packages

Use a virtual environment so nothing is installed system-wide. Create it anywhere outside the video folders, for example in the user's home folder:

```
python -m venv rupipe-venv
```

Activate it:

- Windows (PowerShell): `rupipe-venv\Scripts\Activate.ps1` (or call `rupipe-venv\Scripts\python.exe` directly)
- Linux / macOS: `source rupipe-venv/bin/activate` (or call `rupipe-venv/bin/python` directly)

Install the packages from this skill:

```
python -m pip install -r /path/to/russian-voiceover-51/scripts/requirements.txt
```

Then always run the tool with that Python: `rupipe-venv/bin/python /path/to/russian-voiceover-51/scripts/pipeline.py ...` (on Windows `rupipe-venv\Scripts\python.exe ...`).

If the user has `uv`, this also works without a venv: `uv run --with numpy --with scipy --with soundfile --with pyyaml python /path/to/.../scripts/pipeline.py doctor`.

## ffmpeg and MKVToolNix

- **Windows:** `winget install Gyan.FFmpeg` and `winget install MoritzBunkus.MKVToolNix`, then open a new terminal. Or download the "full" build from https://www.gyan.dev/ffmpeg/builds/ and MKVToolNix from https://mkvtoolnix.download/ .
- **macOS:** `brew install ffmpeg mkvtoolnix`
- **Debian / Ubuntu:** `sudo apt install ffmpeg mkvtoolnix`
- **Fedora:** `sudo dnf install ffmpeg mkvtoolnix` (ffmpeg from RPM Fusion)

Installing software changes the user's system: ask the user before running these commands.

## If the tools are installed but not found

The tool looks for `ffmpeg`, `ffprobe` and `mkvmerge` in PATH. If they live somewhere else (common on Windows: MKVToolNix in `C:\Program Files\MKVToolNix`), do not edit PATH; create an override file and pass it with `--config`:

```yaml
# my-tools.yaml
tools:
  ffmpeg: C:/ffmpeg/bin/ffmpeg.exe
  ffprobe: C:/ffmpeg/bin/ffprobe.exe
  mkvmerge: C:/Program Files/MKVToolNix/mkvmerge.exe
```

```
pipeline --config my-tools.yaml doctor
```

Use forward slashes in paths inside YAML. An override file only needs the keys it changes; `--config` goes before the command and must be repeated on every call.
