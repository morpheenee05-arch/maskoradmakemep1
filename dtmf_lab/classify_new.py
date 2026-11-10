"""Classify new recordings: run the new detector + dump features at hits."""
import sys
import numpy as np
from scipy.io import wavfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio.dtmf import DTMFDetector, _FFTPlan, _analyze_window, _passes_gates

ATT = Path(r"C:\Users\konon\.kimi-code\sessions\wd_p1_from_great_maskorad_3eee7114b542\session_61858712-8f14-483c-b7ac-46d5788aba08\attachments")

FILES = [
    "f_38ce33a4-df38-4fb6-a1cf-0fd1c44fe2bf-w1_1793823942_12bf85f6.wav",
    "f_baaa18c4-78ec-4be6-bb18-a8379b7798b9-w2_1793823810_4d7ebab3.wav",
    "f_641ad771-c330-4c99-b2ff-9e27e9642fae-w2_1793825006_826f7193.wav",
    "f_d86d5cd9-5c69-4660-bd15-24e4344d9c59-w4_1793823908_ded46602.wav",
    "f_d927bff1-8106-4a2d-b691-054ea71ed607-w4_1793824804_83b8ded1.wav",
    "f_056fe2de-801f-41b5-a051-b910ef0b1357-w7_1793824954_5b5cc3b5.wav",
    "f_c2b10d01-87bb-423e-b832-846a0ff03575-w7_1793825134_c2d5f963.wav",
    "f_2cc2146f-adc6-4a42-9bbc-85bedb2fa4f1-w0_1793823655_276ffd10.wav",
    "f_cce83d9a-69dd-4e48-a8b6-0f89a45131ff-w0_1793824010_f7ac21d7.wav",
    "f_70359e9c-da33-4d39-88e2-79a19d603277-w0_1793824575_10376589.wav",
]
SR = 24000


def load(fp):
    sr, d = wavfile.read(fp)
    d = d.astype(np.float32)
    if np.abs(d).max() > 100:
        d /= 32768.0
    return sr, (d[:, 0] if d.ndim > 1 else d)


plan = _FFTPlan(int(SR * 0.08), SR)

for fn in FILES:
    name = fn.split("-")[-1].replace(".wav", "")
    sr, x = load(ATT / fn)
    det = DTMFDetector(sample_rate=sr, window_ms=80, step_ms=40,
                       persistence=2, cooldown_ms=300)
    chunk = int(sr * 0.02)
    hits = []
    for s in range(0, len(x), chunk):
        k = det.feed(x[s:s + chunk])
        if k is not None:
            hits.append((s / sr, k))
    print(f"\n=== {name} (dur={len(x)/sr:.0f}s) -> {[(f'{t:.2f}', k) for t, k in hits] or 'no detections'}")
    # dump features around each hit
    for t, k in hits:
        i = int(t * sr)
        seg = x[max(0, i - int(0.04 * sr)): i + int(0.16 * sr)]
        # analyze a clean window at the hit
        w = x[i:i + int(0.08 * sr)]
        if len(w) < int(0.08 * sr):
            continue
        f = _analyze_window(w, plan)
        if f is None:
            print(f"   hit '{k}' @{t:.2f}s: window gated (silence)")
            continue
        print(f"   hit '{k}' @{t:.2f}s: row={f['row_fest']:.1f}Hz(nom {f['row_f']}) "
              f"col={f['col_fest']:.1f}Hz(nom {f['col_f']}) "
              f"share=({f['row_share']:.2f},{f['col_share']:.2f}) "
              f"contrast=({f['row_contrast']:.1f},{f['col_contrast']:.1f}) "
              f"twist={f['twist_db']:+.1f}dB harm=({f['row_harm']:.2f},{f['col_harm']:.2f}) "
              f"rms={f['rms']:.3f}")
