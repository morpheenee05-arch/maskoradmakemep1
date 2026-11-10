"""Run the production DTMFDetector over each recording's left channel,
streaming-style (20ms chunks), and log every detection with a timestamp."""
import sys
import numpy as np
from scipy.io import wavfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from audio.dtmf import DTMFDetector
except Exception as e:
    print(f"import failed: {e}")
    raise

ATT = Path(r"C:\Users\konon\.kimi-code\sessions\wd_p1_from_great_maskorad_3eee7114b542\session_61858712-8f14-483c-b7ac-46d5788aba08\attachments")

FILES = [
    ("real", "f_5de7763c-348f-4593-80d6-e666b5ca4037-real.wav"),
    ("real_p1", "f_19d89755-0b91-468a-b979-abd879217d5f-real_p1.wav"),
    ("real_p1_2", "f_a5f0feab-5539-42e5-b711-d06ac457bbd8-real_p1_2.wav"),
    ("real_p1_3", "f_0307dbac-7598-4c2c-b97b-acf5903a03b5-real_p1_3.wav"),
    ("fake_p1_1", "f_17859eec-ad28-4868-b469-d217156dcf57-fake_p1_1.wav"),
    ("fake_p1_2", "f_c87faa38-f18a-4f26-8fd4-6caeac07c848-fake_p1_2.wav"),
    ("fake_p1_3", "f_6644c032-2f2a-4149-b11b-a1c187575aba-fake_p1_3.wav"),
    ("fake_p1_4", "f_6e7c2b79-54de-4a37-834d-e72eb6502819-fake_p1_4.wav"),
    ("fake_p1_5", "f_34d5da3b-8e93-4ab8-b1ec-e6cf34a16f44-fake_p1_5.wav"),
]


def load(fp):
    sr, data = wavfile.read(fp)
    data = data.astype(np.float32)
    if np.abs(data).max() > 100:
        data /= 32768.0
    return sr, data


for name, fn in FILES:
    sr, data = load(ATT / fn)
    left = data[:, 0] if data.ndim > 1 else data
    det = DTMFDetector(sample_rate=sr, window_ms=80, step_ms=40,
                       persistence=2, cooldown_ms=300)
    chunk = int(sr * 0.02)  # 20ms streaming chunks
    hits = []
    t = 0.0
    for start in range(0, len(left), chunk):
        key = det.feed(left[start:start + chunk])
        if key is not None:
            hits.append((t, key))
        t = start / sr
    print(f"{name:10s}: {len(hits)} detections -> " +
          (", ".join(f"'{k}'@{t:.2f}s" for t, k in hits) if hits else "none"))
