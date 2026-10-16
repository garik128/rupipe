# Reading the results and adjusting the sound

## SUMMARY line

Example:

```
Show.S01E03.mkv: sync 2 segment(s) (2 distinct offset(s)) [-1021, -1528 samples]; leak 240-5400 Hz worst -12 dB; studio ducking -3.2 dB; voice 15 min; voice audible 89% (studio 82%), voice +2.5 dB to studio, over centre +12.1 dB; limited 51651 samples (0.04% of the changed channels)
```

| field | meaning | what to do |
|---|---|---|
| `sync N segment(s) (K distinct offset(s)) [...]` | The Russian track was aligned to the original in N pieces, each with its own offset (in samples, 48000 per second); K counts the offsets that really differ (by more than 2 samples). Voice-overs are often assembled from pieces, so 2-5 segments are normal. A long file can also split into many segments whose offsets differ by about one sample: then K is 1 and it is not drift. | With several different offsets, offer the user a fragment across a join: the log line `sync: 0:13:37.500 - ...` gives the times; use `fragments --at` about 40 s before a join. |
| `FLAGGED` | Too many different offsets (the Russian track probably runs at a slightly different speed) or no reliable measurement. | Tell the user; the result may have echo or bad voice extraction. Check the track choice. |
| `leak 240-5400 Hz worst X dB` | How much of the original was left in the extracted Russian voice (lower is cleaner). | About -10 dB and lower is fine. -5 dB or higher: probably the wrong Russian track (not made from this original) or sync trouble. |
| `studio ducking X dB` | How much the voice-over studio itself lowered the original under the voice. The new track repeats this in the centre. | Informational. |
| `voice audible N% (studio M%)` | Share of the voice's sound that stands above everything else in the final mix, and the same for the studio's own balance. | Should be close to `target_audible` (0.89). |
| `voice +X dB to studio` | How the voice level differs from the studio's balance. | Informational; studios differ a lot. |
| `limited N samples (X% of the changed channels)` | Peaks cut by the limiter in the changed channels of the main track, and their share of all samples of those channels. | Up to tens of thousands per episode (hundredths of a percent) was not audible in tests. Hundreds of thousands, or tenths of a percent and more: offer a fragment of a loud scene. |

The full log of each file is `rupipe-out/work/<name>/pipeline.log`; the numbers of every step are in `rupipe-out/work/<name>/manifest.json`.

## Voice level

The voice level is chosen automatically for each file: the tool picks the quietest level at which the Russian voice stands out over the rest of the mix in `mix.target_audible` of its sound (default 0.89). A fixed offset does not work because studios mix the voice very differently and some films have a much denser mix than others.

The default 0.89 was calibrated by ear on a soundbar: 0.88-0.90 sounded right, 0.79-0.86 "English drowns the Russian", 0.92-0.95 "the voice is too loud". Taste and speakers differ, so:

- If the user says the voice is too quiet or the English is too loud: raise `target_audible` (for example to 0.91).
- If the voice is too loud: lower it (for example to 0.87).

Put it in an override file and rebuild only the mix:

```yaml
# voice.yaml
mix:
  target_audible: 0.91
```

```
pipeline --config voice.yaml run "<file or folder>" --russian "<choice>" --from mix
```

### A/B comparison in one file

To let the user compare several levels at once, add variants. Each variant becomes an extra Russian track in the same file and in the fragments:

```yaml
# ab.yaml
mix:
  variants:
    louder:
      voice_db: auto
      target_audible: 0.92
      duck_db: auto
      duck_channels: [FC]
    quieter:
      voice_db: auto
      target_audible: 0.85
      duck_db: auto
      duck_channels: [FC]
export:
  track_names:
    louder: "Русский 5.1 (голос громче)"
    quieter: "Русский 5.1 (голос тише)"
```

```
pipeline --config ab.yaml run "<episode>.mkv" --russian "<choice>" --from mix
pipeline --config ab.yaml fragments "<episode>.mkv" --count 3
```

Before asking the user which variant they like, write down your own expectation from the `voice audible` numbers; then compare. After the user decides, build the season without the extra variants (no `ab.yaml`) and with the chosen `target_audible`.

A variant can also use a fixed level: `voice_db: 0` keeps the studio's own balance.

## Other settings

All settings are in `scripts/config.yaml` with comments. Do not edit that file for one job; use an override file with `--config`. Useful keys:

| key | default | meaning |
|---|---|---|
| `mix.target_audible` | 0.89 | voice level target (see above) |
| `mix.voice_db_min`, `mix.voice_db_max` | -10, 6 | limits for the automatic level, relative to the studio balance |
| `mix.variants.<name>.duck_db` | auto | ducking of the centre under the voice: `auto` copies the studio, or a number like -6 |
| `export.track_names.<variant>` | "Русский 5.1 (закадр)" | name of the new track |
| `export.studio_in_name` | true | append the title of the chosen Russian source track (the studio) to the new track name: "Русский 5.1 (закадр) - <title of the source track>" |
| `export.bitrate` | 1024k | E-AC3 bitrate. Higher than the source on purpose: this is a second encode with ffmpeg's simpler encoder; at the source bitrate the added distortion at 8-14 kHz is only ~11 dB below the signal, at 1024k ~28 dB. 1024k is also the safe maximum for most players and receivers. If the user asks why the track is "bigger than the original", explain this. |
| `sync.flag_tolerance_samples` | 2 | segment offsets this close count as one when deciding whether to set `FLAGGED` |
| `fragments.length_s` | 90 | fragment length |
| `tracks.files` | - | per-file track choice, like `--track-for`: `{"E05": "3"}` or `{"E05": {"russian": "3"}}` |
