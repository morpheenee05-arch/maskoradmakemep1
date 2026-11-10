"""Deep spectral analysis of real press vs fake segments + right-channel health."""
import numpy as np
from scipy.io import wavfile
from scipy.signal import find_peaks
from pathlib import Path

ATT = Path(r"C:\Users\konon\.kimi-code\sessions\wd_p1_from_great_maskorad_3eee7114b542\session_61858712-8f14-483c-b7ac-46d5788aba08\attachments")
F = {
    "real": "f_5de7763c-348f-4593-80d6-e666b5ca4037-real.wav",
    "real_p1": "f_19d89755-0b91-468a-b979-abd879217d5f-real_p1.wav",
    "real_p1_2": "f_a5f0feab-5539-42e5-b711-d06ac457bbd8-real_p1_2.wav",
    "real_p1_3": "f_0307dbac-7598-4c2c-b97b-acf5903a03b5-real_p1_3.wav",
    "fake_p1_1": "f_17859eec-ad28-4868-b469-d217156dcf57-fake_p1_1.wav",
    "fake_p1_2": "f_c87faa38-f18a-4f26-8fd4-6caeac07c848-fake_p1_2.wav",
    "fake_p1_3": "f_6644c032-2f2a-4149-b11b-a1c187575aba-fake_p1_3.wav",
    "fake_p1_4": "f_6e7c2b79-54de-4a37-834d-e72eb6502819-fake_p1_4.wav",
    "fake_p1_5": "f_34d5da3b-8e93-4ab8-b1ec-e6cf34a16f44-fake_p1_5.wav",
}

def load(name, chan=0):
    sr, d = wavfile.read(ATT / F[name])
    d = d.astype(np.float32)
    if np.abs(d).max() > 100:
        d /= 32768.0
    return sr, (d[:, chan] if d.ndim > 1 else d)

def top_peaks(seg, sr, n=6, fmin=200, fmax=3600):
    w = np.hanning(len(seg))
    sp = np.abs(np.fft.rfft(seg * w))
    freqs = np.fft.rfftfreq(len(seg), 1 / sr)
    m = (freqs >= fmin) & (freqs <= fmax)
    pk, _ = find_peaks(sp * m, height=sp.max() * 0.05)
    order = np.argsort(sp[pk])[::-1][:n]
    return sorted([(freqs[pk[i]], sp[pk[i]]) for i in order])

def show(name, t0, t1, chan=0):
    sr, x = load(name, chan)
    seg = x[int(t0 * sr):int(t1 * sr)]
    rms = np.sqrt(np.mean(seg ** 2))
    print(f"\n### {name} ch{'L' if chan==0 else 'R'} {t0}-{t1}s rms={rms:.4f} ###")
    for f, m in top_peaks(seg, sr):
        print(f"   {f:7.1f} Hz  mag={m:9.1f}")

# Real press core segments
show("real_p1", 35.05, 35.15)
show("real_p1_2", 35.95, 36.05)
# Fake segments
show("fake_p1_2", 36.0, 36.5)     # strong 697-only tone
show("fake_p1_5", 13.9, 14.1)     # strong 697-ish, weak col
show("fake_p1_1", 1.1, 1.3)       # voice-like
show("fake_p1_3", 11.7, 11.9)     # quiet noise that fired 'A'
show("fake_p1_4", 10.2, 10.4)

# Right channel health: look for dropouts/glitches in bot speech channel
print("\n===== RIGHT CHANNEL HEALTH =====")
for name in F:
    sr, r = load(name, chan=1)
    # frame rms at 10ms
    fl = int(sr * 0.01)
    nfr = len(r) // fl
    fr = np.sqrt(np.mean(r[:nfr * fl].reshape(nfr, fl) ** 2, axis=1))
    active = fr > 0.01
    # dropouts: near-zero frames inside active speech regions
    drop = 0
    i = 0
    while i < nfr:
        if active[i]:
            j = i
            while j < nfr and (active[j] or (j + 1 < nfr and active[j + 1])):
                j += 1
            region = fr[i:j]
            if len(region) > 5:
                drop += int(np.sum(region[2:-2] < 0.0005))
            i = j
        else:
            i += 1
    # sample discontinuities (clicks): abs diff outliers
    d1 = np.abs(np.diff(r))
    thr = max(0.3, np.percentile(d1, 99.9) * 3)
    clicks = int(np.sum(d1 > thr))
    peak = np.abs(r).max()
    clipped = float(np.mean(np.abs(r) > 0.98) * 100)
    print(f"{name:10s}: peak={peak:.3f} clipped%={clipped:.2f} mid-dropout-frames={drop} clicks={clicks}")
