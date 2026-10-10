# Troubleshooting

| message or symptom | cause | what to do |
|---|---|---|
| `Missing Python package: ...` | packages not installed in the Python you used | install from `scripts/requirements.txt` into that Python (see setup.md), or call the venv's Python |
| `tool 'ffmpeg' not found` (or ffprobe, mkvmerge) | not installed or not in PATH | install (setup.md) or point to it with a `tools:` override file |
| `[FAIL] ffmpeg E-AC3 encoder` | a minimal ffmpeg build | install a full build (setup.md) |
| `NEEDS A CHOICE: russian: several candidate tracks` | the file has more than one candidate Russian track | ask the user which voice-over, then pass `--russian` (this is expected, not an error) |
| `NEEDS A CHOICE: ... several titles contain 'X'` | the title part matches more than one track | use a longer part of the title or the track number |
| `NEEDS A CHOICE: ... no track title contains 'X'` | this episode has no such track (missing or named differently) | show the user that episode's track list; use `--track-for "<episode>=<number>"` or skip the episode |
| `NEEDS A CHOICE: original: cannot tell which multichannel track is the original` | several multichannel tracks, or the original is tagged Russian | ask the user, pass `--original` |
| `original: unsupported channel layout` | the original is 7.1 or something unusual | not supported yet; tell the user |
| `sync ... FLAGGED` or `no confident measurement` | the Russian track does not match this original (other cut or other speed), or the track is silent | check the track choice; tell the user the result will be poor |
| `leak ... worst` around 0 dB | same as above, or the Russian track is not a voice-over of this video | check the track choice |
| `target audibility not reached within voice_db_max` | very dense mix or a very quiet voice | tell the user; a fragment shows whether it is acceptable; raising `mix.voice_db_max` is possible |
| `FAILED, see the log` in SUMMARY | an error in one file; the others continue | read `rupipe-out/work/<name>/pipeline.log`, report the error |
| Out of memory | long film on a machine with little RAM | close other programs; 16 GB is recommended for long films |
| Run was interrupted | - | run the same command again; finished steps are skipped |
| Result needs to be rebuilt from scratch | - | add `--from analyze` (or delete `rupipe-out/work/<name>`) |
| Garbled Cyrillic in the console on Windows | console code page | cosmetic; set the environment variable `PYTHONIOENCODING=utf-8` |

The exit code of `plan` and `run` is 2 when a track choice is needed, 1 when a file failed, 0 when everything is fine.
