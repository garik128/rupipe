"""Check what would go into the public repository (or a skill copy) before it leaves the machine.

Fails on: personal files, media files, big files, control characters, and text that
ties the project to one machine or to real releases and voice-over studios.

    uv run python tools/check_public.py          # files git would commit
    (tools/build_skill.py runs the same text checks on the skill copy)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

# Words that must not be published (names, paths, network of a machine; names of real
# releases and voice-over studios) are kept in a local, unpublished file - listing them
# here would publish them. One word per line, '#' starts a comment.
WORDS_FILE = ROOT / "tools" / "forbidden-words.local.txt"
# Files that may contain them: LICENSE names the repository owner.
EXEMPT = {"LICENSE"}
PERSONAL_FILES = {"config.local.yaml", "AGENTS.local.md", WORDS_FILE.name}


def _words() -> list[str]:
    if not WORDS_FILE.is_file():
        return []
    lines = (ln.split("#", 1)[0].strip() for ln in WORDS_FILE.read_text(encoding="utf-8").splitlines())
    return [ln.lower() for ln in lines if ln]


WORDS = _words()


def _word_pattern(w: str) -> str:
    """Whole-word match, but only on the sides where the word ends in a letter or digit,
    so a prefix like "10.0." still matches inside an address."""
    left = r"(?<![^\W_])" if w[0].isalnum() else ""
    right = r"(?![^\W_])" if w[-1].isalnum() else ""
    return left + re.escape(w) + right
MEDIA = {".mkv", ".mka", ".mp4", ".wav", ".flac", ".ac3", ".eac3", ".dts", ".thd", ".mp3", ".aac", ".m4a"}
TEXT = {".md", ".py", ".yaml", ".yml", ".toml", ".txt", ".json", ""}
MAX_BYTES = 1_000_000


def text_problems(path: Path, rel: str) -> list[str]:
    if path.resolve() == SELF or path.suffix.lower() not in TEXT:
        return []
    try:
        body = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [f"{rel}: not UTF-8 text"]
    low = body.lower()
    out = []
    if path.name not in EXEMPT:
        # Whole words only: a forbidden "art" does not match inside "party".
        out += [f"{rel}: contains a forbidden word (see {WORDS_FILE.name})" for w in WORDS
                if re.search(_word_pattern(w), low)]
    for email in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", body):
        if not email.lower().endswith(("@users.noreply.github.com", "@anthropic.com", "@example.com")):
            out.append(f"{rel}: e-mail address {email!r}")
    for ip in re.findall(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])", body):
        out.append(f"{rel}: IP address {ip!r}")
    # Real paths name a real user; placeholders like C:\Users\<имя> are fine.
    for pattern, what in ((r"[A-Za-z]:[\\/]+Users[\\/]+\w", "Windows user folder"),
                          (r"/home/[a-z_][\w-]*", "Linux home folder"),
                          (r"/Users/\w", "macOS home folder"),
                          (r"AppData[\\/]|scratchpad[\\/]", "temporary/session folder")):
        if re.search(pattern, body):
            out.append(f"{rel}: {what} path")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", body):
        out.append(f"{rel}: control characters")
    return out


def file_problems(path: Path, rel: str) -> list[str]:
    out = []
    if path.name in PERSONAL_FILES:
        out.append(f"{rel}: personal file")
    if path.suffix.lower() in MEDIA:
        out.append(f"{rel}: media file")
    if path.stat().st_size > MAX_BYTES:
        out.append(f"{rel}: larger than {MAX_BYTES // 1000} KB")
    return out + text_problems(path, rel)


def git_files() -> list[str]:
    cmd = ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        sys.exit("not a git repository: run `git init` first")
    return [f for f in proc.stdout.splitlines() if f and (ROOT / f).is_file()]


def main() -> int:
    files = git_files()
    problems = [p for rel in files for p in file_problems(ROOT / rel, rel)]
    for f in PERSONAL_FILES:
        if (ROOT / f).exists() and f in files:
            problems.append(f"{f}: would be committed; it must be in .gitignore")
    if problems:
        print("NOT ready to publish:\n  " + "\n  ".join(problems))
        return 1
    print(f"OK: {len(files)} files would be published, nothing private found:")
    for f in files:
        print("  " + f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
