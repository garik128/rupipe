# How it works

Read this only to debug or to explain the method. You do not need it for normal use.

The tool runs these steps for each file; each step writes its result to `rupipe-out/work/<name>/` and records it in `manifest.json`, so a rerun continues where it stopped.

1. **analyze** - lists the audio tracks with ffprobe, reads dialnorm from the first AC3/E-AC3 frame (ffprobe does not show it) and resolves which track is the original and which is the Russian voice-over. It never guesses when there is a choice.
2. **decode** - decodes both tracks to 32-bit float WAV, 48 kHz, one file per channel. Dynamic range compression of the AC3/E-AC3 decoder is switched off (`-drc_scale 0`), otherwise the original's dynamics change and the subtraction below fails. The Russian track is always taken as stereo.
3. **sync** - the container offset is usually wrong by a few to tens of milliseconds, and Russian tracks are often assembled from pieces with different offsets. GCC-PHAT cross-correlation between a downmix of the original and the Russian track is measured every 4 seconds over the whole file; windows that agree form segments with one offset each; the boundary between segments is searched with short windows. Each segment is shifted by its own fractional offset (windowed-sinc interpolation) with short crossfades at the joins. Sample accuracy matters: an error of a few samples ruins the subtraction at high frequencies.
4. **voice** - the Russian stereo track is modelled as `g(t, band) * H(f) * original + voice`. `H` is the studio's downmix of the original channels into L/R (complex, per frequency, least squares over the whole file); `g` is the studio's slowly changing gain in 14 bands (its level automation and ducking). The voice is independent of the original, so it does not bias the fit. What remains after subtracting is the voice plus codec noise; the noise ("leak") is estimated per band and suppressed with a spectral mask. Then voice activity detection, a gate between phrases, the measured depth of the studio's ducking, and scaling of the voice to the centre channel's level (dividing by how strongly the original centre went into L/R), which keeps the studio's voice-to-original balance. No neural separator is used.
5. **mix** - samples up to 400 seconds of Russian speech and computes how audible the voice is: the share of time-frequency cells (250-4000 Hz) where the voice is at least 3 dB above everything else the listener hears (ducked centre plus front and surround channels). The quietest voice level reaching `target_audible` is used. Then `FC = duck(t) * FC + gain * voice`; the other channels are the decoded original, untouched. A peak limiter acts only on samples above -1 dBFS.
6. **encode** - ffmpeg E-AC3, 1024 kbit/s, same channel layout and dialnorm as the original.
7. **mux** - mkvmerge copies the whole input with the new track(s) inserted first among audio tracks and set as default; all source tracks are kept, only their default flag is cleared. It records where every source track ended up, which `fragments` uses (track language tags are unreliable, so tracks are found by position).

`fragments` picks scenes by loudness, LFE, peaks, share of speech and front/surround-to-centre balance, and cuts them with mkvmerge from the finished file.

## Limits

- Atmos objects are lost: ffmpeg decodes E-AC3 JOC as its 5.1 core. The result is plain 5.1.
- 7.1 originals are not supported yet (ffmpeg's E-AC3 encoder has no 7.1).
- Dubbing (Russian replaces English completely) is not supported; that needs removing English speech from the centre, which this tool does not do.
- The Russian voice cannot sound better than the source voice-over recording.
