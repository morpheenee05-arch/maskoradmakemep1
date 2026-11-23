"""
Production-Grade DTMF Detector (FFT-based).

One rfft per analysis window, then a cascade of gates designed from measured
recordings of real key presses vs. false triggers (voice, line tones, noise):

  - Adaptive noise-floor gate — near silence, relative thresholds mislead
  - Narrow-band peak search (±2%) around each DTMF frequency with parabolic
    interpolation — tolerates device clock drift and slightly detuned phones
  - Frequency-precision gate — interpolated peak must land within ±1.6%
    of the nominal tone; speech harmonics usually miss
  - Spectral-contrast gate — a real tone is a narrow spike; a voice formant
    is broad, so the peak must tower over its ±4..10% neighborhood
  - Selectivity — best tone must dominate the runner-up inside its group
  - Twist validation (ITU-T Q.24) — high/low group level balance
  - Guard-band dominance — both tones must beat any other voice-band tone
  - 2nd-harmonic rejection — voice has harmonics, pure DTMF tones do not
  - Persistence across consecutive windows + cooldown against retriggering

Works at any sample rate (optimized for 24kHz VoIP).
"""
import math
from collections import deque
from typing import Optional

import numpy as np
from loguru import logger


# Standard DTMF frequency pairs
DTMF_ROWS = np.array([697.0, 770.0, 852.0, 941.0])
DTMF_COLS = np.array([1209.0, 1336.0, 1477.0, 1633.0])
DTMF_MAP = {
    (0, 0): "1", (0, 1): "2", (0, 2): "3", (0, 3): "A",
    (1, 0): "4", (1, 1): "5", (1, 2): "6", (1, 3): "B",
    (2, 0): "7", (2, 1): "8", (2, 2): "9", (2, 3): "C",
    (3, 0): "*", (3, 1): "0", (3, 2): "#", (3, 3): "D",
}

# Guard tones across the voice band: a real press must dominate ALL of these
_GUARD_FREQS = np.arange(620.0, 1701.0, 45.0)

# Peak search half-width around each nominal tone (device drift, detuned phones)
_PEAK_SEARCH_PCT = 0.02
# Interpolated peak must land this close to nominal
_FREQ_TOL_PCT = 0.016
# Spectral contrast: peak amp vs strongest neighbor in ±4..10% around it
_CONTRAST_MIN = 4.0
# Minimum fraction of window power carried by each tone
_POWER_SHARE_MIN = 0.10
# Best tone vs runner-up inside the same group (amplitude ratio)
_SELECTIVITY_MIN = 2.5
# Twist limits, dB (ITU-T Q.24: normal <4, reverse <8; margin for VoIP paths)
_TWIST_MAX_DB = 8.0
_TWIST_MIN_DB = -8.0
# Both tones must beat the strongest guard tone by this factor
_GUARD_DOMINANCE = 2.0
# 2nd harmonic must be weaker than this fraction of the fundamental
_HARMONIC_MAX = 0.35
# Silence gate: absolute floor + adaptive multiple of the noise floor
_RMS_FLOOR = 0.004
_NOISE_MULT = 6.0
_NOISE_GATE_CAP = 0.05


class _FFTPlan:
    """Per-(window-size, sample-rate) cached spectrum geometry."""

    def __init__(self, n: int, sr: int):
        self.n = n
        self.sr = sr
        self.hann = np.hanning(n).astype(np.float32)
        self.freqs = np.fft.rfftfreq(n, d=1.0 / sr)
        self.df = float(self.freqs[1]) if len(self.freqs) > 1 else 1.0

        def band(f: float, pct: float) -> slice:
            lo = int(np.searchsorted(self.freqs, f * (1.0 - pct)))
            hi = int(np.searchsorted(self.freqs, f * (1.0 + pct)))
            lo = max(lo, 1)
            return slice(lo, max(hi, lo + 2))

        self.row_bands = [band(f, _PEAK_SEARCH_PCT) for f in DTMF_ROWS]
        self.col_bands = [band(f, _PEAK_SEARCH_PCT) for f in DTMF_COLS]
        self.harm_bands = {
            float(f): band(2.0 * f, 0.015)
            for f in np.concatenate([DTMF_ROWS, DTMF_COLS])
        }
        self.guard_bands = [band(f, 0.01) for f in _GUARD_FREQS]

    def contrast_neighbors(self, f: float) -> np.ndarray:
        """Bin indices in ±4..10% around f (outside the tone's mainlobe)."""
        fr = self.freqs
        outer = (fr >= f * 0.90) & (fr <= f * 1.10)
        inner = (fr >= f * 0.96) & (fr <= f * 1.04)
        return np.nonzero(outer & ~inner)[0]


def _interp_peak(mag: np.ndarray, sl: slice, freqs: np.ndarray, df: float):
    """Strongest bin in slice + parabolic interpolation -> (freq, amplitude)."""
    seg = mag[sl]
    j = int(np.argmax(seg)) + sl.start
    delta = 0.0
    if 0 < j < len(mag) - 1:
        y0, y1, y2 = float(mag[j - 1]), float(mag[j]), float(mag[j + 1])
        denom = y0 - 2.0 * y1 + y2
        if denom != 0.0:
            delta = max(-1.0, min(1.0, 0.5 * (y0 - y2) / denom))
    return float(freqs[j] + delta * df), float(mag[j])


def _analyze_window(samples: np.ndarray, plan: _FFTPlan) -> Optional[dict]:
    """Extract DTMF features from one window. None when gated out early."""
    n = len(samples)
    rms = float(np.sqrt(np.mean(samples ** 2)))
    if rms < _RMS_FLOOR:
        return None

    mag = np.abs(np.fft.rfft(samples * plan.hann))
    freqs = plan.freqs

    # Amplitude estimate of a pure tone: single-sided, Hann coherent gain 0.5
    amp_scale = 2.0 / (n * 0.5)

    rows = []
    for f, sl in zip(DTMF_ROWS, plan.row_bands):
        f_est, a = _interp_peak(mag, sl, freqs, plan.df)
        rows.append((f, f_est, a * amp_scale))
    cols = []
    for f, sl in zip(DTMF_COLS, plan.col_bands):
        f_est, a = _interp_peak(mag, sl, freqs, plan.df)
        cols.append((f, f_est, a * amp_scale))

    br = max(range(4), key=lambda i: rows[i][2])
    bc = max(range(4), key=lambda i: cols[i][2])
    row_f, row_fest, row_a = rows[br]
    col_f, col_fest, col_a = cols[bc]

    # Power share of each tone in the window
    total_pow = rms * rms + 1e-12
    row_share = 0.5 * row_a * row_a / total_pow
    col_share = 0.5 * col_a * col_a / total_pow

    # Selectivity inside each group
    row_second = max(rows[i][2] for i in range(4) if i != br)
    col_second = max(cols[i][2] for i in range(4) if i != bc)

    # Spectral contrast: peak vs strongest neighbor outside the mainlobe
    row_nei = plan.contrast_neighbors(row_f)
    col_nei = plan.contrast_neighbors(col_f)
    row_contrast = row_a / ((float(np.max(mag[row_nei])) * amp_scale) + 1e-12) if len(row_nei) else 99.0
    col_contrast = col_a / ((float(np.max(mag[col_nei])) * amp_scale) + 1e-12) if len(col_nei) else 99.0

    # 2nd-harmonic levels (voice rejection)
    row_h = _interp_peak(mag, plan.harm_bands[row_f], freqs, plan.df)[1] * amp_scale
    col_h = _interp_peak(mag, plan.harm_bands[col_f], freqs, plan.df)[1] * amp_scale

    # Strongest guard-band tone away from the pair. Exclusion is measured
    # from the *estimated* peaks so a detuned tone's own spectral leakage
    # cannot fail the dominance check.
    guard_max = 0.0
    for f, sl in zip(_GUARD_FREQS, plan.guard_bands):
        if abs(float(f) - row_fest) > 55.0 and abs(float(f) - col_fest) > 55.0:
            guard_max = max(guard_max, _interp_peak(mag, sl, freqs, plan.df)[1] * amp_scale)

    twist_db = 20.0 * math.log10((col_a + 1e-12) / (row_a + 1e-12))

    return dict(
        rms=rms,
        key=DTMF_MAP[(br, bc)],
        row_f=row_f, row_fest=row_fest, row_a=row_a,
        col_f=col_f, col_fest=col_fest, col_a=col_a,
        row_share=row_share, col_share=col_share,
        row_sel=row_a / (row_second + 1e-12),
        col_sel=col_a / (col_second + 1e-12),
        row_contrast=row_contrast, col_contrast=col_contrast,
        row_harm=row_h / (row_a + 1e-12), col_harm=col_h / (col_a + 1e-12),
        guard_max=guard_max, twist_db=twist_db,
    )


def _passes_gates(f: dict, noise_floor: float) -> bool:
    """All per-window acceptance gates in one place."""
    if f["rms"] < max(_RMS_FLOOR, min(_NOISE_GATE_CAP, _NOISE_MULT * noise_floor)):
        return False
    if f["row_share"] < _POWER_SHARE_MIN or f["col_share"] < _POWER_SHARE_MIN:
        return False
    if abs(f["row_fest"] - f["row_f"]) / f["row_f"] > _FREQ_TOL_PCT:
        return False
    if abs(f["col_fest"] - f["col_f"]) / f["col_f"] > _FREQ_TOL_PCT:
        return False
    if f["row_contrast"] < _CONTRAST_MIN or f["col_contrast"] < _CONTRAST_MIN:
        return False
    if f["row_sel"] < _SELECTIVITY_MIN or f["col_sel"] < _SELECTIVITY_MIN:
        return False
    if not (_TWIST_MIN_DB <= f["twist_db"] <= _TWIST_MAX_DB):
        return False
    if f["row_a"] < f["guard_max"] * _GUARD_DOMINANCE:
        return False
    if f["col_a"] < f["guard_max"] * _GUARD_DOMINANCE:
        return False
    if f["row_harm"] > _HARMONIC_MAX or f["col_harm"] > _HARMONIC_MAX:
        return False
    return True


def _tone_like(f: dict, noise_floor: float) -> bool:
    """Loud, tone-dominated window — worth diagnosing when later gates reject it."""
    if f["rms"] < max(_RMS_FLOOR, min(_NOISE_GATE_CAP, _NOISE_MULT * noise_floor)):
        return False
    return f["row_share"] >= _POWER_SHARE_MIN and f["col_share"] >= _POWER_SHARE_MIN


def _first_rejection(f: dict) -> Optional[str]:
    """Why a tone-like window was rejected — the first failing gate, with numbers."""
    if abs(f["row_fest"] - f["row_f"]) / f["row_f"] > _FREQ_TOL_PCT:
        return (f"freq off: row {f['row_fest']:.0f}Hz vs {f['row_f']:.0f}Hz "
                f"(tol {_FREQ_TOL_PCT * 100:.1f}%)")
    if abs(f["col_fest"] - f["col_f"]) / f["col_f"] > _FREQ_TOL_PCT:
        return (f"freq off: col {f['col_fest']:.0f}Hz vs {f['col_f']:.0f}Hz "
                f"(tol {_FREQ_TOL_PCT * 100:.1f}%)")
    if f["row_contrast"] < _CONTRAST_MIN or f["col_contrast"] < _CONTRAST_MIN:
        return (f"contrast row={f['row_contrast']:.1f} col={f['col_contrast']:.1f} "
                f"< {_CONTRAST_MIN}")
    if f["row_sel"] < _SELECTIVITY_MIN or f["col_sel"] < _SELECTIVITY_MIN:
        return (f"selectivity row={f['row_sel']:.1f} col={f['col_sel']:.1f} "
                f"< {_SELECTIVITY_MIN}")
    if not (_TWIST_MIN_DB <= f["twist_db"] <= _TWIST_MAX_DB):
        return f"twist {f['twist_db']:.1f}dB outside {_TWIST_MIN_DB}..{_TWIST_MAX_DB}"
    if f["row_a"] < f["guard_max"] * _GUARD_DOMINANCE:
        return (f"guard tone louder: row {f['row_a']:.3f} vs guard "
                f"{f['guard_max']:.3f}×{_GUARD_DOMINANCE}")
    if f["col_a"] < f["guard_max"] * _GUARD_DOMINANCE:
        return (f"guard tone louder: col {f['col_a']:.3f} vs guard "
                f"{f['guard_max']:.3f}×{_GUARD_DOMINANCE}")
    if f["row_harm"] > _HARMONIC_MAX or f["col_harm"] > _HARMONIC_MAX:
        return (f"2nd harmonic row={f['row_harm']:.2f} col={f['col_harm']:.2f} "
                f"> {_HARMONIC_MAX}")
    return None


class DTMFDetector:
    """
    Sliding-window DTMF detector with persistence checking.

    Feed audio continuously via `feed()`. When a key is confirmed,
    `feed()` returns the key character. Otherwise returns None.

    Architecture:
      - Internal buffer accumulates raw samples
      - Every `step_size` new samples, one FFT window is analyzed
      - A key must pass all gates in `persistence` consecutive windows
      - After confirmation, a cooldown prevents duplicate detections
      - A rolling noise floor adapts the silence gate to the line
    """

    def __init__(self, sample_rate: int = 24000, window_ms: int = 80,
                 step_ms: int = 40, persistence: int = 2, cooldown_ms: int = 300):
        """
        Args:
            sample_rate:  Audio sample rate (Hz)
            window_ms:    Analysis window size (ms) — 80ms is optimal for DTMF
            step_ms:      Step between windows (ms) — 40ms = 50% overlap
            persistence:  Consecutive windows required to confirm a key
            cooldown_ms:  Minimum time between detections (ms)
        """
        self.sample_rate = sample_rate
        self.window_size = int(sample_rate * window_ms / 1000)
        self.step_size = int(sample_rate * step_ms / 1000)
        self.persistence = persistence
        self.cooldown_samples = int(sample_rate * cooldown_ms / 1000)

        self._plan = _FFTPlan(self.window_size, sample_rate)

        # Internal state
        self._buffer = np.zeros(0, dtype=np.float32)
        self._pending_key: Optional[str] = None
        self._pending_count: int = 0
        self._samples_since_last: int = self.cooldown_samples  # Allow immediate first detection
        self._total_fed: int = 0
        # Confirmed key that is still sounding — suppress retriggering on a
        # single long press while still allowing fast repeated presses
        self._latched_key: Optional[str] = None
        # Rolling window-RMS history for the adaptive noise floor (~10s)
        self._rms_hist: deque = deque(maxlen=250)
        # Near-miss diagnostics throttle (seconds of fed audio at last log)
        self._last_near_miss_log = -10.0
        # Per-call diagnostics: did any tone-like energy show up, and which
        # gate rejected it (Counter of reason strings)
        self._diag_windows = 0
        self._diag_tone_like = 0
        self._diag_rejections: dict[str, int] = {}
        self._diag_best: Optional[dict] = None
        self._diag_loudest: Optional[dict] = None

    def _noise_floor(self) -> float:
        if len(self._rms_hist) < 10:
            return 0.0
        return float(np.percentile(np.asarray(self._rms_hist), 10))

    def feed(self, samples: np.ndarray) -> Optional[str]:
        """
        Feed new audio samples. Returns detected key or None.

        Args:
            samples: float32 array of audio samples normalized to [-1, 1]

        Returns:
            Detected key ("0"-"9", "*", "#", "A"-"D") or None
        """
        if len(samples) == 0:
            return None

        # Append to internal buffer
        self._buffer = np.concatenate([self._buffer, samples.astype(np.float32)])
        self._samples_since_last += len(samples)
        self._total_fed += len(samples)

        detected_key = None

        # Process all available windows
        while len(self._buffer) >= self.window_size:
            window = self._buffer[:self.window_size]
            feat = _analyze_window(window, self._plan)
            self._rms_hist.append(float(np.sqrt(np.mean(window ** 2))))
            self._diag_windows += 1

            key = None
            if feat is not None:
                nf = self._noise_floor()
                gate_rms = max(_RMS_FLOOR, min(_NOISE_GATE_CAP, _NOISE_MULT * nf))
                if feat["rms"] >= gate_rms and (
                    self._diag_loudest is None or feat["rms"] > self._diag_loudest["rms"]
                ):
                    self._diag_loudest = feat
                if _passes_gates(feat, nf):
                    key = feat["key"]
                elif _tone_like(feat, nf):
                    self._diag_tone_like += 1
                    if self._diag_best is None or feat["rms"] > self._diag_best["rms"]:
                        self._diag_best = feat
                    # Loud, tone-dominated window that still failed a gate —
                    # a real press being rejected looks exactly like this.
                    reason = _first_rejection(feat)
                    if reason:
                        self._diag_rejections[reason.split(":")[0]] = (
                            self._diag_rejections.get(reason.split(":")[0], 0) + 1
                        )
                    fed_s = self._total_fed / self.sample_rate
                    if reason and fed_s - self._last_near_miss_log >= 1.5:
                        self._last_near_miss_log = fed_s
                        logger.info(
                            f"dtmf near-miss '{feat['key']}' at {fed_s:.1f}s: "
                            f"{reason} (rms={feat['rms']:.3f})"
                        )

            if key is not None and key == self._latched_key:
                # Previously confirmed press still sounding — no retrigger
                key = None
            elif self._latched_key is not None:
                # Tone ended or changed — the next press of that key is new
                self._latched_key = None

            if key is not None:
                if key == self._pending_key:
                    self._pending_count += 1
                else:
                    self._pending_key = key
                    self._pending_count = 1

                # Persistence check: confirmed!
                if self._pending_count >= self.persistence:
                    if self._samples_since_last >= self.cooldown_samples:
                        detected_key = self._pending_key
                        self._latched_key = self._pending_key
                        self._samples_since_last = 0
                        self._pending_count = 0
                        self._pending_key = None
            else:
                # No tone in this window — reset pending
                self._pending_key = None
                self._pending_count = 0

            # Slide forward by step_size
            self._buffer = self._buffer[self.step_size:]

        # Keep buffer from growing unbounded (retain at most 2x window)
        max_keep = self.window_size * 2
        if len(self._buffer) > max_keep:
            self._buffer = self._buffer[-max_keep:]

        return detected_key

    def diag_summary(self) -> str:
        """One-line per-call digest: tone-like energy seen + rejecting gates."""
        fed_s = self._total_fed / float(self.sample_rate or 1)
        parts = [f"fed={fed_s:.1f}s windows={self._diag_windows} tone_like={self._diag_tone_like}"]
        if self._diag_rejections:
            rej = ", ".join(f"{k}×{v}" for k, v in
                            sorted(self._diag_rejections.items(), key=lambda kv: -kv[1]))
            parts.append(f"rejections: {rej}")
        if self._diag_best is not None:
            b = self._diag_best
            parts.append(
                f"loudest tone-like '{b['key']}': row {b['row_fest']:.0f}Hz a={b['row_a']:.3f} "
                f"col {b['col_fest']:.0f}Hz a={b['col_a']:.3f} twist={b['twist_db']:.1f}dB "
                f"contrast={b['row_contrast']:.1f}/{b['col_contrast']:.1f} "
                f"sel={b['row_sel']:.1f}/{b['col_sel']:.1f} "
                f"harm={b['row_harm']:.2f}/{b['col_harm']:.2f} guard={b['guard_max']:.3f} "
                f"rms={b['rms']:.3f}"
            )
        elif self._diag_loudest is not None:
            b = self._diag_loudest
            parts.append(
                f"loudest window '{b['key']}': row {b['row_fest']:.0f}Hz a={b['row_a']:.3f} "
                f"col {b['col_fest']:.0f}Hz a={b['col_a']:.3f} twist={b['twist_db']:.1f}dB "
                f"shares={b['row_share']:.2f}/{b['col_share']:.2f} "
                f"contrast={b['row_contrast']:.1f}/{b['col_contrast']:.1f} "
                f"sel={b['row_sel']:.1f}/{b['col_sel']:.1f} "
                f"harm={b['row_harm']:.2f}/{b['col_harm']:.2f} guard={b['guard_max']:.3f} "
                f"rms={b['rms']:.3f}"
            )
        return " | ".join(parts)

    def reset(self):
        """Clear all state for a new call."""
        self._buffer = np.zeros(0, dtype=np.float32)
        self._pending_key = None
        self._pending_count = 0
        self._samples_since_last = self.cooldown_samples
        self._total_fed = 0
        self._latched_key = None
        self._rms_hist.clear()
        self._last_near_miss_log = -10.0
        self._diag_windows = 0
        self._diag_tone_like = 0
        self._diag_rejections = {}
        self._diag_best = None
        self._diag_loudest = None
