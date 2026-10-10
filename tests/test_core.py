import numpy as np

from rupipe.probe import parse_dialnorm
from rupipe.sync import frac_shift, measure_map

SR = 48000
SYNC_CFG = {"window_s": 8, "step_s": 4, "max_lag_s": 0.5, "min_confidence": 3.0,
            "run_tolerance_samples": 1.0, "refine_window_s": 2, "refine_step_s": 0.25,
            "max_segments": 12}


def _bits_to_bytes(bits: str) -> bytes:
    bits += "0" * (-len(bits) % 8) + "0" * 128
    return bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))


def test_dialnorm_ac3():
    # sync, crc1, fscod+frmsizecod, bsid=8, bsmod=0, acmod=2, dsurmod, lfeon=0, dialnorm=27
    bits = "0000101101110111" + "0" * 16 + "00010100" + "01000" + "000" + "010" + "00" + "0" + "11011"
    assert parse_dialnorm(_bits_to_bytes(bits)) == -27


def test_dialnorm_eac3():
    # sync, strmtyp, substreamid, frmsiz, fscod, numblkscod, acmod=7, lfeon=1, bsid=16, dialnorm=31
    bits = "0000101101110111" + "00" + "000" + "0" * 11 + "00" + "11" + "111" + "1" + "10000" + "11111"
    assert parse_dialnorm(_bits_to_bytes(bits)) == -31


def test_dialnorm_reserved_zero_means_minus_31():
    bits = "0000101101110111" + "0" * 16 + "00010100" + "01000" + "000" + "010" + "00" + "0" + "00000"
    assert parse_dialnorm(_bits_to_bytes(bits)) == -31


def _reader(sig):
    def read(start, length):
        out = np.zeros(length, dtype=np.float32)
        a, b = max(start, 0), min(start + length, len(sig))
        if b > a:
            out[a - start:b - start] = sig[a:b]
        return out
    return read


def _case(pieces: list[tuple[float, int]], lag0: int, seconds: int = 120):
    """pieces: (start second in the original, lag) for each piece of the Russian track."""
    rng = np.random.default_rng(1)
    n = SR * seconds
    orig = rng.standard_normal(n).astype(np.float32)
    # Russian track: the original shifted (orig[i] lands at ru[i - L]), quieter,
    # plus an unrelated "voice" and noise.
    ru = 0.5 * rng.standard_normal(n).astype(np.float32)
    edges = [int(p[0] * SR) for p in pieces] + [n]
    for (s0, lag), e in zip(pieces, edges[1:]):
        s = int(s0 * SR)
        a, b = max(s - lag, 0), min(e - lag, n)
        ru[a:b] += 0.5 * orig[a + lag:b + lag]
    return measure_map(_reader(orig), _reader(ru), n, lag0, SYNC_CFG, SR)


def test_sync_finds_lag_from_zero_guess():
    res = _case([(0, 1234)], lag0=0)
    assert len(res["segments"]) == 1
    assert abs(res["segments"][0]["lag"] - 1234) < 0.1
    assert not res["flagged"]


def test_sync_refines_container_guess():
    res = _case([(0, -3001)], lag0=-2950)
    assert len(res["segments"]) == 1
    assert abs(res["segments"][0]["lag"] + 3001) < 0.1


def test_sync_finds_a_jump_in_the_offset():
    res = _case([(0, -1021), (60, -1026)], lag0=0)
    segs = res["segments"]
    assert len(segs) == 2
    assert abs(segs[0]["lag"] + 1021) < 0.1 and abs(segs[1]["lag"] + 1026) < 0.1
    assert abs(segs[1]["start"] / SR - 60) < 0.5


def test_frac_shift_matches_analytic_delay():
    t = np.arange(SR, dtype=np.float64)
    x = np.sin(2 * np.pi * 1000 * t / SR).astype(np.float32)
    lag = 10.37
    y = frac_shift(x, 1000, 2000, lag)
    ref = np.sin(2 * np.pi * 1000 * (np.arange(1000, 3000) - lag) / SR)
    assert np.max(np.abs(y - ref)) < 2e-3


def test_stft_perfect_reconstruction():
    from rupipe.stft import Stft
    st = Stft(1024, 256)
    x = np.random.default_rng(2).standard_normal(48000).astype(np.float32)
    y = st.inverse(st.forward(x), len(x))
    inner = slice(1024, len(x) - 1024)
    assert np.max(np.abs(y[inner] - x[inner])) < 1e-4


def test_limiter_keeps_ceiling_and_leaves_quiet_parts():
    from rupipe.mix import limit
    x = np.full(48000, 0.1, dtype=np.float32)
    x[20000:20100] = 1.5
    y, overs = limit(x, 0.89)
    assert overs == 100
    assert np.abs(y).max() <= 0.89 + 1e-6
    assert np.allclose(y[:10000], 0.1) and np.allclose(y[-10000:], 0.1)


def _streams():
    mk = lambda i, ch, lang, title: {"index": i, "codec": "x", "layout": None, "bit_rate": None,
                                     "channels": ch, "language": lang, "title": title}
    # A Russian voice-over tagged eng, as happens in real releases.
    return [mk(1, 2, "rus", "MVO [Studio A]"), mk(3, 6, "rus", "MVO [Studio C 5.1]"),
            mk(6, 2, "eng", "DVO [Студия Б]"), mk(7, 6, "eng", None)]


def test_track_choice_is_never_guessed():
    import pytest
    from rupipe.probe import TrackChoiceNeeded, pick_original, pick_russian
    st = _streams()
    orig = pick_original(st, "auto")
    assert orig["index"] == 7                      # the only non-Russian 6ch track
    with pytest.raises(TrackChoiceNeeded):
        pick_russian(st, "auto", orig)             # several candidates: ask
    assert pick_russian(st, "студия б", orig)["index"] == 6   # case-insensitive, Cyrillic
    assert pick_russian(st, "6", orig)["index"] == 6
    with pytest.raises(TrackChoiceNeeded):
        pick_russian(st, "MVO", orig)              # ambiguous title part: ask
    assert pick_russian([st[2], st[3]], "auto", orig)["index"] == 6   # single other track


def test_track_choice_per_file():
    from pathlib import Path
    from rupipe.cli import tracks_for
    cfg = {"tracks": {"original": "auto", "russian": "Studio B", "files": {"E05": "2"}}}
    assert tracks_for(cfg, Path("Show.S01E01.mkv"), [])["tracks"]["russian"] == "Studio B"
    assert tracks_for(cfg, Path("Show.S01E05.mkv"), [])["tracks"]["russian"] == "2"
    t = tracks_for(cfg, Path("Show.S01E03.mkv"), ["E03=4", "e03=original:7"])["tracks"]
    assert t["russian"] == "4" and t["original"] == "7"
    assert cfg["tracks"]["russian"] == "Studio B"   # the shared config is not modified


def test_audibility_solve_reaches_target(tmp_path):
    import soundfile as sf
    from rupipe.audibility import Audibility
    rng = np.random.default_rng(3)
    n = SR * 30
    files = {}
    for ch in ("FL", "FR", "FC", "SL", "SR"):
        files[ch] = f"orig_{ch}.wav"
        sf.write(tmp_path / files[ch], (0.05 * rng.standard_normal(n)).astype(np.float32), SR)
    # "voice": noise bursts with a 4 Hz syllable envelope
    env = (np.sin(2 * np.pi * 4 * np.arange(n) / SR) > 0).astype(np.float32)
    sf.write(tmp_path / "voice.wav", (0.05 * rng.standard_normal(n) * env).astype(np.float32), SR)
    aud = Audibility(tmp_path, "voice.wav", files, np.ones(n, bool), blocks=20)
    lo = aud.measure(-10, 0)["glimpse"]
    hi = aud.measure(10, 0)["glimpse"]
    assert lo < hi
    target = aud.measure(0.0, 0.0)["glimpse"]          # reachable by construction
    vdb = aud.solve(target, 0.0, -10, 15)
    assert vdb <= 0.0
    assert aud.measure(vdb, 0.0)["glimpse"] >= target
    assert vdb == -10 or aud.measure(vdb - 0.25, 0.0)["glimpse"] < target
    assert aud.solve(1.01, 0.0, -10, 15) == 15          # unreachable: the upper limit
