"""Scan no-detection files for near-miss dual-tone regions (keys 1 and 2)."""
import sys
import numpy as np
from scipy.io import wavfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio.dtmf import _FFTPlan, _analyze_window

ATT = Path(r"C:\Users\konon\.kimi-code\sessions\wd_p1_from_great_maskorad_3eee7114b542\session_61858712-8f14-483c-b7ac-46d5788aba08\attachments")

FILES = [
    ("w2_1793823810", "f_baaa18c4-78ec-4be6-bb18-a8379b7798b9-w2_1793823810_4d7ebab3.wav"),
    ("w2_1793825006", "f_641ad771-c330-4c99-b2ff-9e27e9642fae-w2_1793825006_826f7193.wav"),
    ("w4_1793823908", "f_d86d5cd9-5c69-4660-bd15-24e4344d9c59-w4_1793823908_ded46602.wav"),
    ("w7_1793825134", "f_c2b10d01-87bb-423e-b832-846a0ff03575-w7_1793825134_c2d5f963.wav"),
    ("w0_1793824010", "f_cce83d9a-69dd-4e48-a8b6-0f89a45131ff-w0_1793824010_f7ac21d7.wav"),
    ("w0_1793824575", "f_70359e9c-da33-4d39-88e2-79a19d603277-w0_1793824575_10376589.wav"),
]
SR = 24000
plan = _FFTPlan(int(SR * 0.08), SR)

for name, fn in FILES:
    sr, d = wavfile.read(ATT / fn)
    d = d.astype(np.float32)
    if np.abs(d).max() > 100:
        d /= 32768.0
    x = d[:, 0] if d.ndim > 1 else d
    best = []
    win = int(0.08 * SR)
    for i in range(0, len(x) - win, int(0.04 * SR)):
        f = _analyze_window(x[i:i + win], plan)
        if f and f["key"] in ("1", "2"):
            score = min(f["row_share"], f["col_share"])
            best.append((score, i / SR, f))
    best.sort(key=lambda b: -b[0])
    print(f"\n=== {name}: top-3 '1'/'2'-like windows ===")
    for score, t, f in best[:3]:
        print(f"  t={t:6.2f}s key={f['key']} row={f['row_fest']:6.1f} col={f['col_fest']:6.1f} "
              f"share=({f['row_share']:.2f},{f['col_share']:.2f}) "
              f"contrast=({f['row_contrast']:.1f},{f['col_contrast']:.1f}) "
              f"twist={f['twist_db']:+.1f} harm=({f['row_harm']:.2f},{f['col_harm']:.2f}) "
              f"rms={f['rms']:.3f}")
