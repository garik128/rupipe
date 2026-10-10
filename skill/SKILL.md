---
name: russian-voiceover-51
description: Builds a Russian E-AC3 5.1 audio track for a movie or TV episode from the multichannel original (English 5.1, E-AC3/Atmos) and a stereo Russian voice-over that are both inside the same MKV, and adds it as the first, default audio track to a copy of the file. Use whenever the user wants Russian audio in 5.1/surround ("русская 5.1", "закадр в 5.1", "сделай русскую дорожку 5.1"), wants to rebuild or upmix a stereo Russian voice-over (закадровый перевод, MVO/DVO/AVO tracks) over the original surround mix, or complains that a release has only a stereo Russian track next to a good 5.1 original, or wants to process a whole season folder of MKV files this way.
compatibility: Python 3.10+, ffmpeg with E-AC3 encoder, MKVToolNix. Windows, Linux or macOS. No GPU, no internet needed at run time.
metadata:
  version: "1.0"
---

# Russian voice-over in 5.1

Web releases often carry a good English 5.1 track and several Russian voice-overs in plain stereo. This skill takes the English 5.1 original and one Russian stereo voice-over from the same MKV, pulls the clean Russian voice out of the stereo track (by subtracting the aligned original), and puts that voice into the centre channel of the original 5.1. Effects, music and surround stay as in the original. The result is a new E-AC3 5.1 track (1024 kbit/s) added first and as default to a copy of the MKV.

It handles voice-over (закадр: Russian voice over audible English). It does not do full dubbing yet, and it needs a 5.1 original (7.1 originals are not supported yet).

All work is done by one command-line tool in `scripts/`. You run it, read its output, talk to the user. Do not reimplement its steps yourself.

## Ground rules

These come from real use; each one prevents a mistake that already happened.

1. **Never guess which Russian track to use - ask the user.** Releases name, number, order and language-tag tracks arbitrarily, and this changes even between episodes of one season (a Russian voice-over tagged as English is common). The tool refuses to guess and stops when there is a choice. Show the user the list and let them pick. Do not pick "the first Russian track" to make it run, and do not call one option "obvious" because of its language tag: the tags are often wrong, the user knows which studio they want.
2. **Plan first, then process.** Show the plan for all files and get the user's OK before running anything long.
3. **Never remove or change source tracks.** The tool only adds tracks to a copy; the input file is never modified. Keep it that way.
4. **The user judges the sound by ear.** You cannot listen. Report the numbers the tool prints and cut short fragments for the user to listen to.
5. Talk to the user in their language (usually Russian).

## Running the tool

The entry point is `scripts/pipeline.py` inside this skill folder. Always call it with Python and the full path, for example:

```
python /path/to/russian-voiceover-51/scripts/pipeline.py doctor
```

Below, `pipeline` means `python /path/to/russian-voiceover-51/scripts/pipeline.py`.

**First time on a machine:** run `pipeline doctor`. It checks Python, Python packages, ffmpeg, ffprobe, mkvmerge and free disk space, and prints `[FAIL]` lines with what to install. To install things, read [references/setup.md](references/setup.md). Run `doctor` again until all lines are `[OK]`.

Results go to a `rupipe-out` folder next to the input (or to `--out <folder>`, given before the command). It holds the finished files, a `work` subfolder with temporary files (about 8 GB per hour of video; safe to delete after the job) and `listen` with fragments.

## Workflow

### 1. Plan

```
pipeline plan "<season folder or file.mkv>"
```

For each file it prints every audio track and which one is used as `ORIGINAL` and `RUSSIAN`. Nothing is processed. Typical output when a choice is needed (simplified, the real lines have more columns):

```
Show.S01E01.mkv
  1            aac    stereo    ?     rus  MVO [Studio A]
  2            ac3    stereo  192k    rus  MVO [Studio B]
  3  ORIGINAL  eac3   5.1(side) 640k  eng
  NEEDS A CHOICE: russian: several candidate tracks. Choose with --russian <number or part of the title>.
```

Show this to the user (shortened if the season is long) and ask which voice-over they want. If episodes differ (other numbering, missing track, other names), point that out.

### 2. Choose the tracks

- `--russian "<part of title>"` for all files, for example `--russian "Studio B"`. Titles usually stay the same across a season while numbers change, so prefer a part of the title.
- `--russian 2` uses track number 2 (the number shown by `plan`).
- `--track-for "E05=3"` overrides the choice for files whose name contains `E05` (repeatable). `--track-for "E05=original:4"` does the same for the original.
- `--original ...` only if the plan picked the wrong original.

Run `plan` again with the choice and check that every file is resolved and the chosen track is the same voice-over everywhere (same title, codec, channels). The last line says `N of N file(s) resolved`.

### 3. Trial episode

For a new series or a new voice-over studio, build one episode first:

```
pipeline run "<first episode>.mkv" --russian "<choice>"
pipeline fragments "<first episode>.mkv" --count 3
```

`fragments` cuts up to four 90-second scenes (dialogue, loud action, speech over music, quiet talk) from the finished file into `rupipe-out/listen`, each with the new track, the source Russian track and the original. Give the user the paths and ask them to listen. `--at HH:MM:SS` cuts a fragment at a given time (useful for joins between sync segments, see below).

If the user says the voice is too loud or too quiet, see "Voice level" in [references/results.md](references/results.md). Rebuild only the mix with `--from mix` (fast).

### 4. Whole season

```
pipeline run "<season folder>" --russian "<choice>" [--track-for ...]
```

It plans first and stops without processing anything if any file is ambiguous (exit code 2). It resumes where it stopped if interrupted: finished steps are skipped. An episode takes a few minutes on a normal CPU.

### 5. Check and report

At the end `run` prints a `SUMMARY` line per file. Go through it and tell the user about anything unusual. Short guide (details in [references/results.md](references/results.md)):

| in SUMMARY | fine | tell the user |
|---|---|---|
| `sync N segment(s)` | 1, or a few | many segments, or `FLAGGED` |
| `leak ... worst` | -10 dB or lower | -5 dB or higher (check the track choice) |
| `voice audible N%` | close to the target (89%) | warning "target audibility not reached" in the log |
| `limited` | up to tens of thousands | hundreds of thousands (offer a fragment to check) |
| `FAILED` | - | always, with the error from the log |

Finished files: `rupipe-out/<name>.rus51.mkv`. Tell the user where they are and that `rupipe-out/work` can be deleted.

## Command summary

```
pipeline doctor                                    check the environment
pipeline plan <folder|file> [choice options]       show tracks and the plan, process nothing
pipeline run  <folder|file> [choice options]       build; prints SUMMARY at the end
pipeline run  <file> --from mix                    redo the mix (after changing voice level settings)
pipeline fragments <file> [--count 3] [--at HH:MM:SS] [--length 90]
choice options: --russian X  --original X  --track-for NAME=X
global options (before the command): --out <folder>  --config <override.yaml>
```

## More information

- [references/setup.md](references/setup.md) - installing Python packages, ffmpeg and MKVToolNix on Windows, Linux, macOS.
- [references/results.md](references/results.md) - reading SUMMARY, voice level and A/B variants, settings in `scripts/config.yaml`.
- [references/troubleshooting.md](references/troubleshooting.md) - error messages and what to do.
- [references/how-it-works.md](references/how-it-works.md) - what each step does inside (read only if you need to debug or explain).
