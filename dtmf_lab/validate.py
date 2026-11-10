"""Validate the new DTMF detector on all recordings x channel degradations.

Pass criteria:
  - real files: exactly one '1' detection in EVERY condition
  - fake files: zero detections of '1'/'2' in EVERY condition
"""
import sys
import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio.dtmf import DTMFDetector

ATT = Path(r"C:\Users\konon\.kimi-code\sessions\wd_p1_from_great_maskorad_3eee7114b542\session_61858712-8f14-483c-b7ac-46d5788aba08\attachments")

REAL = [
    ("real", "f_5de7763c-348f-4593-80d6-e666b5ca4037-real.wav"),
    ("real_p1", "f_19d89755-0b91-468a-b979-abd879217d5f-real_p1.wav"),
    ("real_p1_2", "f_a5f0feab-5539-42e5-b711-d06ac457bbd8-real_p1_2.wav"),
    ("real_p1_3", "f_0307dbac-7598-4c2c-b97b-acf5903a03b5-real_p1_3.wav"),
]
FAKE = [
    ("fake_p1_1", "f_17859eec-ad28-4868-b469-d217156dcf57-fake_p1_1.wav"),
    ("fake_p1_2", "f_c87faa38-f18a-4f26-8fd4-6caeac07c848-fake_p1_2.wav"),
    ("fake_p1_3", "f_6644c032-2f2a-4149-b11b-a1c187575aba-fake_p1_3.wav"),
    ("fake_p1_4", "f_6e7c2b79-54de-4a37-834d-e72eb6502819-fake_p1_4.wav"),
    ("fake_p1_5", "f_34d5da3b-8e93-4ab8-b1ec-e6cf34a16f44-fake_p1_5.wav"),
]
SR = 24000


def load(fn):
    sr, d = wavfile.read(ATT / fn)
    d = d.astype(np.float32)
    if np.abs(d).max() > 100:
        d /= 32768.0
    return d[:, 0] if d.ndim > 1 else d


def mulaw_roundtrip(x8):
    mu = 255.0
    y = np.sign(x8) * np.log1p(mu * np.abs(x8)) / np.log1p(mu)
    q = np.round(y * 127.0) / 127.0
    return np.sign(q) * (np.expm1(np.abs(q) * np.log1p(mu)) / mu)


def phone8k(x):
    return resample_poly(resample_poly(x, 1, 3), 3, 1).astype(np.float32)


def phone8k_mulaw(x):
    x8 = resample_poly(x, 1, 3)
    x8 = mulaw_roundtrip(x8)
    return resample_poly(x8, 3, 1).astype(np.float32)


def add_noise(x, snr_db):
    sig = np.sqrt(np.mean(x ** 2))
    noise = np.random.default_rng(42).standard_normal(len(x)).astype(np.float32)
    n = np.sqrt(np.mean(noise ** 2))
    return (x + noise * (sig / n) * 10 ** (-snr_db / 20)).astype(np.float32)


def drift(x, pct):
    # playback clock off by pct: resample by (1+pct)
    g = 1000
    up, down = g, int(round(g / (1 + pct)))
    return resample_poly(x, up, down).astype(np.float32)


CONDITIONS = {
    "clean": lambda x: x,
    "phone8k": phone8k,
    "mulaw8k": phone8k_mulaw,
    "noise20dB": lambda x: add_noise(x, 20),
    "quiet_x0.1": lambda x: (x * 0.1).astype(np.float32),
    "drift+0.8%": lambda x: drift(x, 0.008),
    "drift-0.8%": lambda x: drift(x, -0.008),
    "combo": lambda x: add_noise(phone8k_mulaw(x), 25),
}


def run(x):
    det = DTMFDetector(sample_rate=SR, window_ms=80, step_ms=40,
                       persistence=2, cooldown_ms=300)
    chunk = int(SR * 0.02)
    hits = []
    for start in range(0, len(x), chunk):
        key = det.feed(x[start:start + chunk])
        if key is not None:
            hits.append((start / SR, key))
    return hits


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    conds = {k: v for k, v in CONDITIONS.items() if only is None or k == only}
    total_fail = 0
    for cond_name, fn in conds.items():
        print(f"\n===== condition: {cond_name} =====")
        for name, f in REAL:
            hits = run(fn(load(f)))
            ones = [h for h in hits if h[1] == "1"]
            ok = len(ones) == 1 and len(hits) == 1
            total_fail += 0 if ok else 1
            mark = "OK " if ok else "FAIL"
            print(f"  [{mark}] {name:10s} hits={[(f'{t:.2f}', k) for t, k in hits]}")
        for name, f in FAKE:
            hits = run(fn(load(f)))
            bad = [h for h in hits if h[1] in ("1", "2")]
            ok = len(bad) == 0
            total_fail += 0 if ok else 1
            mark = "OK " if ok else "FAIL"
            extra = f" hits={[(f'{t:.2f}', k) for t, k in hits]}" if hits else ""
            print(f"  [{mark}] {name:10s}{extra}")
    print(f"\n{'ALL PASS' if total_fail == 0 else f'{total_fail} FAILURES'}")
    return total_fail


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
