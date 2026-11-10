"""Precise feature dump at the detector's internal window positions around hits."""
import sys
import numpy as np
from scipy.io import wavfile
from scipy.signal import find_peaks
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio.dtmf import _FFTPlan, _analyze_window, _passes_gates

ATT = Path(r"C:\Users\konon\.kimi-code\sessions\wd_p1_from_great_maskorad_3eee7114b542\session_61858712-8f14-483c-b7ac-46d5788aba08\attachments")

HITS = [
    ("w1_1793823942", "f_38ce33a4-df38-4fb6-a1cf-0fd1c44fe2bf-w1_1793823942_12bf85f6.wav", 36.90),
    ("w4_1793824804", "f_d927bff1-8106-4a2d-b691-054ea71ed607-w4_1793824804_83b8ded1.wav", 36.30),
    ("w7_1793824954", "f_056fe2de-801f-41b5-a051-b910ef0b1357-w7_1793824954_5b5cc3b5.wav", 33.90),
    ("w0_1793823655", "f_2cc2146f-adc6-4a42-9bbc-85bedb2fa4f1-w0_1793823655_276ffd10.wav", 31.74),
]
SR = 24000
plan = _FFTPlan(int(SR * 0.08), SR)


def load(fp):
    sr, d = wavfile.read(fp)
    d = d.astype(np.float32)
    if np.abs(d).max() > 100:
        d /= 32768.0
    return d[:, 0] if d.ndim > 1 else d


def top_peaks(seg, n=6, fmin=200, fmax=3600):
    w = np.hanning(len(seg))
    sp = np.abs(np.fft.rfft(seg * w))
    freqs = np.fft.rfftfreq(len(seg), 1 / SR)
    m = (freqs >= fmin) & (freqs <= fmax)
    pk, _ = find_peaks(sp * m, height=sp.max() * 0.05)
    order = np.argsort(sp[pk])[::-1][:n]
    return sorted([(freqs[pk[i]], sp[pk[i]]) for i in order])


for name, fn, t_hit in HITS:
    x = load(ATT / fn)
    print(f"\n===== {name} around {t_hit}s =====")
    # scan 10ms-step windows in [t-0.25, t+0.05], print every passing window
    for t0 in np.arange(t_hit - 0.25, t_hit + 0.05, 0.01):
        i = int(t0 * SR)
        w = x[i:i + int(0.08 * SR)]
        if len(w) < int(0.08 * SR):
            continue
        f = _analyze_window(w, plan)
        if f and _passes_gates(f, 0.0):
            print(f"  win@{t0:6.2f}s key={f['key']} row={f['row_fest']:7.1f} col={f['col_fest']:7.1f} "
                  f"share=({f['row_share']:.2f},{f['col_share']:.2f}) "
                  f"contrast=({f['row_contrast']:5.1f},{f['col_contrast']:5.1f}) "
                  f"sel=({f['row_sel']:.1f},{f['col_sel']:.1f}) twist={f['twist_db']:+.1f} "
                  f"harm=({f['row_harm']:.2f},{f['col_harm']:.2f}) rms={f['rms']:.3f}")
    # spectral peaks at the strongest window
    i = int((t_hit - 0.04) * SR)
    seg = x[i:i + int(0.1 * SR)]
    print("  top spectral peaks @hit:", ", ".join(f"{fr:.0f}Hz" for fr, _ in top_peaks(seg)))
