"""Assemble the portable Agent Skill from this project: dist/<name>/ and dist/<name>.zip.

Sources: skill/ (SKILL.md, references, entry script, README for people), rupipe/ (code),
config.yaml (already portable; machine settings live in config.local.yaml, not copied).
Checks the Agent Skills spec rules and runs the same checks as check_public.py.

    uv run python tools/build_skill.py
"""
from __future__ import annotations

import re
import shutil
import sys
import zipfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_public import file_problems  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NAME = "russian-voiceover-51"
DIST = ROOT / "dist"
OUT = DIST / NAME


def check_config(text: str) -> None:
    cfg = yaml.safe_load(text)
    if cfg["tools"] != {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "mkvmerge": "mkvmerge"}:
        raise SystemExit(f"config.yaml is not portable: tools = {cfg['tools']}")
    if cfg["work_dir"] or cfg["output_dir"] or cfg["fragments"]["dir"]:
        raise SystemExit("config.yaml is not portable: work_dir/output_dir/fragments.dir must be empty")


def check_skill(out: Path) -> list[str]:
    problems = []
    text = (out / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return ["SKILL.md has no YAML frontmatter"]
    fm = yaml.safe_load(m.group(1))
    name = fm.get("name", "")
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) or len(name) > 64:
        problems.append(f"bad name: {name!r}")
    if name != out.name:
        problems.append(f"name {name!r} differs from folder {out.name!r}")
    if not 1 <= len(fm.get("description", "")) <= 1024:
        problems.append(f"description length {len(fm.get('description', ''))} (1-1024)")
    if "compatibility" in fm and not 1 <= len(fm["compatibility"]) <= 500:
        problems.append("compatibility length (1-500)")
    if any(not isinstance(v, str) for v in (fm.get("metadata") or {}).values()):
        problems.append("metadata values must be strings")
    if len(text.splitlines()) > 500:
        problems.append("SKILL.md longer than 500 lines")
    for md in out.rglob("*.md"):
        for link in re.findall(r"\]\(([^)#]+)\)", md.read_text(encoding="utf-8")):
            if "://" not in link and not (md.parent / link).exists() and not (out / link).exists():
                problems.append(f"{md.relative_to(out)}: broken link {link}")
    for f in sorted(out.rglob("*")):
        if f.is_file():
            problems += file_problems(f, f.relative_to(out).as_posix())
    return problems


def main() -> int:
    config_text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    check_config(config_text)
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(ROOT / "skill", OUT)
    pkg = OUT / "scripts" / "rupipe"
    pkg.mkdir(parents=True)
    for py in sorted((ROOT / "rupipe").glob("*.py")):
        shutil.copy2(py, pkg / py.name)
    (OUT / "scripts" / "config.yaml").write_text(config_text, encoding="utf-8")
    shutil.copy2(ROOT / "LICENSE", OUT / "LICENSE")

    for py in OUT.rglob("*.py"):
        compile(py.read_text(encoding="utf-8"), str(py), "exec")
    problems = check_skill(OUT)
    if problems:
        print("Skill check FAILED:\n  " + "\n  ".join(problems))
        return 1

    archive = DIST / f"{NAME}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(OUT.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, f.relative_to(DIST).as_posix())
    files = [f for f in OUT.rglob("*") if f.is_file()]
    print(f"OK: {OUT} ({len(files)} files), archive {archive} ({archive.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
