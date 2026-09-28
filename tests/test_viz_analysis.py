"""The analysis every visualizer reads: bands where the sound is, silence
as silence, beats on beats."""
import numpy as np

from twiddle.viz import signals
from twiddle.viz.analysis import FFT_N, N_BANDS, RATE, Analyser, band_edges
from twiddle.viz.cli import SIGNAL_NAMES


def _tone(freq, n=FFT_N, amp=0.5):
    t = np.arange(n) / RATE
    s = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.column_stack([s, s])


def test_band_edges_rise_and_every_band_has_a_bin():
    e = band_edges()
    assert len(e) == N_BANDS + 1
    assert (np.diff(e) >= 1).all()


def test_a_tone_lights_the_band_it_is_in():
    for freq in (100, 1000, 5000):
        an = Analyser()
        for _ in range(5):
            f = an.frame(_tone(freq), 1 / 30)
        loudest = int(np.argmax(f.raw_bands))
        centres = np.geomspace(45, 10_000, N_BANDS + 1)
        assert centres[loudest] <= freq * 1.25 and centres[loudest + 1] >= freq / 1.25, freq


def test_silence_is_silent_and_draws_nothing():
    an = Analyser()
    f = an.frame(np.zeros((FFT_N, 2), np.float32), 1 / 30)
    assert f.silent and not f.beat
    assert f.bands.max() == 0 and f.energy == 0


def test_bars_fall_with_gravity_rather_than_vanishing():
    an = Analyser()
    for _ in range(5):
        loud = an.frame(_tone(200), 1 / 30).bands.max()
    after = an.frame(np.zeros((FFT_N, 2), np.float32), 1 / 30).bands.max()
    assert 0 < after < loud


def test_a_kick_drum_gives_beats_and_steady_noise_does_not():
    def beats(sig):
        an = Analyser()
        gen = signals.SIGNALS[sig]()
        return sum(an.frame(gen.window(FFT_N), 1 / 30).beat for _ in range(150))
    assert beats("music") >= 5          # 5 s at 120 bpm is 10 kicks
    assert beats("sine") == 0


def test_garbage_in_is_not_a_crash():
    an = Analyser()
    for bad in (np.zeros((0, 2)), np.zeros(10), np.full((FFT_N, 2), np.nan)):
        f = an.frame(bad, 1 / 30)
        assert np.isfinite(f.bands).all()


def test_the_cli_spells_the_signals_it_offers_correctly():
    assert sorted(signals.SIGNALS) == SIGNAL_NAMES
