"""Real-time audio feature extraction: frequency bands + beat detection.

Stateful and cheap: each ``push`` of one hop (~20 ms) slides a Hann-windowed
FFT, buckets power into the configured frequency bands with per-band automatic
gain control (so loud and quiet tracks both map to 0..1), and runs a
spectral-flux onset detector with an adaptive threshold for beat flags.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from ..const import (
    ANALYSIS_HOP,
    ANALYSIS_SAMPLE_RATE,
    ANALYSIS_WINDOW,
    BANDS,
)


@dataclass(slots=True)
class AnalysisFrame:
    """One frame of audio features, all band values normalised to 0..1."""

    bands: dict[str, float] = field(default_factory=dict)
    energy: float = 0.0  # broadband RMS energy, normalised
    beat: bool = False
    beat_strength: float = 0.0  # how far flux exceeded threshold, 0..~3
    tempo_bpm: float | None = None


class _AGC:
    """Per-band automatic gain control: tracks a decaying peak as the 1.0 ref."""

    __slots__ = ("_peak", "_decay", "_floor")

    def __init__(self, decay: float = 0.9985, floor: float = 1e-6) -> None:
        self._peak = floor
        self._decay = decay
        self._floor = floor

    def normalise(self, value: float) -> float:
        self._peak = max(value, self._peak * self._decay, self._floor)
        return min(1.0, value / self._peak)


class Analyzer:
    """Turns a stream of audio hops into :class:`AnalysisFrame` features."""

    def __init__(
        self,
        sample_rate: int = ANALYSIS_SAMPLE_RATE,
        window: int = ANALYSIS_WINDOW,
        hop: int = ANALYSIS_HOP,
        beat_sensitivity: float = 1.4,
    ) -> None:
        self._sr = sample_rate
        self._window = window
        self._hop = hop
        self._buf = np.zeros(window, dtype=np.float32)
        self._hann = np.hanning(window).astype(np.float32)

        # Precompute FFT bin index ranges per band.
        freqs = np.fft.rfftfreq(window, 1.0 / sample_rate)
        self._band_bins: dict[str, tuple[int, int]] = {}
        for name, (lo, hi) in BANDS.items():
            lo_i = int(np.searchsorted(freqs, lo, side="left"))
            hi_i = int(np.searchsorted(freqs, hi, side="right"))
            self._band_bins[name] = (lo_i, max(lo_i + 1, hi_i))
        self._agc = {name: _AGC() for name in BANDS}
        self._energy_agc = _AGC()

        # Onset / beat state.
        self._prev_mag: np.ndarray | None = None
        self._flux_hist: deque[float] = deque(maxlen=43)  # ~0.9s at 50 fps
        self._sensitivity = beat_sensitivity
        self._refractory = 6  # min frames between beats (~120 ms)
        self._since_beat = self._refractory
        self._beat_times: deque[float] = deque(maxlen=8)
        self._frame_index = 0

    @property
    def frame_period(self) -> float:
        """Seconds represented by one hop/frame."""
        return self._hop / self._sr

    def push(self, hop: np.ndarray) -> AnalysisFrame:
        """Process one hop of mono float32 samples and return features."""
        if hop.dtype != np.float32:
            hop = hop.astype(np.float32)
        n = hop.shape[0]
        if n >= self._window:
            self._buf = hop[-self._window :].copy()
        else:
            self._buf = np.roll(self._buf, -n)
            self._buf[-n:] = hop

        spectrum = np.fft.rfft(self._buf * self._hann)
        mag = np.abs(spectrum).astype(np.float32)
        power = mag * mag

        bands: dict[str, float] = {}
        for name, (lo_i, hi_i) in self._band_bins.items():
            raw = float(np.mean(power[lo_i:hi_i])) if hi_i > lo_i else 0.0
            bands[name] = self._agc[name].normalise(np.sqrt(raw))

        rms = float(np.sqrt(np.mean(self._buf * self._buf)))
        energy = self._energy_agc.normalise(rms)

        beat, strength = self._detect_beat(mag)
        tempo = self._estimate_tempo()
        self._frame_index += 1

        return AnalysisFrame(
            bands=bands,
            energy=energy,
            beat=beat,
            beat_strength=strength,
            tempo_bpm=tempo,
        )

    def _detect_beat(self, mag: np.ndarray) -> tuple[bool, float]:
        if self._prev_mag is None:
            self._prev_mag = mag
            return False, 0.0
        # Spectral flux: sum of positive magnitude increases.
        diff = mag - self._prev_mag
        flux = float(np.sum(diff[diff > 0]))
        self._prev_mag = mag

        beat = False
        strength = 0.0
        if len(self._flux_hist) >= self._flux_hist.maxlen // 2:
            arr = np.fromiter(self._flux_hist, dtype=np.float32)
            threshold = float(arr.mean() + self._sensitivity * arr.std())
            self._since_beat += 1
            if flux > threshold and threshold > 0 and self._since_beat >= self._refractory:
                beat = True
                strength = min(3.0, flux / threshold)
                self._since_beat = 0
                self._beat_times.append(self._frame_index * self.frame_period)
        else:
            self._since_beat += 1

        self._flux_hist.append(flux)
        return beat, strength

    def _estimate_tempo(self) -> float | None:
        if len(self._beat_times) < 4:
            return None
        intervals = np.diff(np.fromiter(self._beat_times, dtype=np.float64))
        intervals = intervals[(intervals > 0.25) & (intervals < 2.0)]  # 30-240 BPM
        if intervals.size < 2:
            return None
        return float(60.0 / np.median(intervals))

    def reset(self) -> None:
        """Clear transient state, e.g. on a track change."""
        self._buf[:] = 0.0
        self._prev_mag = None
        self._flux_hist.clear()
        self._beat_times.clear()
        self._since_beat = self._refractory
