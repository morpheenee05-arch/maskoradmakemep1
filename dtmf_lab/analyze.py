"""Analyze DTMF recordings: find where the current detector fires and what the
spectrum looks like at those points, for both real and fake presses."""
import sys
import numpy as np
from scipy.io import wavfile
from pathlib import Path

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

DTMF_ROWS = np.array([697.0, 770.0, 852.0, 941.0])
DTMF_COLS = np.array([1209.0, 1336.0, 1477.0, 1633.0])
KEYS = {(0,0):"1",(0,1):"2",(0,2):"3",(0,3):"A",(1,0):"4",(1,1):"5",(1,2):"6",(1,3):"B",
        (2,0):"7",(2,1):"8",(2,2):"9",(2,3):"C",(3,0):"*",(3,1):"0",(3,2):"#",(3,3):"D"}


def load(fp):
    sr, data = wavfile.read(fp)
    data = data.astype(np.float32)
    if data.dtype == np.float32 and np.abs(data).max() > 100:  # int16 range
        data /= 32768.0
    return sr, data


def band_mag(x, f, sr):
    n = len(x)
    k = int(round(n * f / sr))
    w = 2.0 * np.pi * k / n
    t = np.arange(n)
    return abs(np.dot(x, np.exp(-1j * w * t)))


def analyze_windows(x, sr, win_ms=80, step_ms=40):
    """Per window: best row/col, their relative mags, twist, dominance vs guard."""
    win = int(sr * win_ms / 1000)
    step = int(sr * step_ms / 1000)
    guard = np.arange(620.0, 1701.0, 45.0)
    rows = []
    for start in range(0, len(x) - win + 1, step):
        seg = x[start:start + win]
        rms = float(np.sqrt(np.mean(seg ** 2)))
        rm = np.array([band_mag(seg, f, sr) for f in DTMF_ROWS])
        cm = np.array([band_mag(seg, f, sr) for f in DTMF_COLS])
        br, bc = int(np.argmax(rm)), int(np.argmax(cm))
        norm = rms * len(seg) + 1e-12
        # guard: strongest non-pair tone in band
        om = max(band_mag(seg, f, sr) for f in guard
                 if abs(f - DTMF_ROWS[br]) > 40 and abs(f - DTMF_COLS[bc]) > 40)
        twist = 20 * np.log10(max(cm[bc], 1e-12) / max(rm[br], 1e-12))
        rows.append(dict(t=start / sr, rms=rms, br=br, bc=bc,
                         row_rel=rm[br] / norm, col_rel=cm[bc] / norm,
                         row_mag=rm[br], col_mag=cm[bc],
                         key=KEYS[(br, bc)], twist=twist,
                         dom_r=rm[br] / (om + 1e-12), dom_c=cm[bc] / (om + 1e-12),
                         row_ratio=rm[br] / (np.sort(rm)[-2] + 1e-12),
                         col_ratio=cm[bc] / (np.sort(cm)[-2] + 1e-12)))
    return rows


def report_candidate_regions(name, rows):
    """Show windows that pass the current detector's per-window gates."""
    cand = [r for r in rows if r["rms"] >= 0.005 and r["row_rel"] >= 0.09
            and r["col_rel"] >= 0.09 and r["row_ratio"] >= 1.6 and r["col_ratio"] >= 1.6
            and -10 <= r["twist"] <= 6 and r["dom_r"] >= 2.0 and r["dom_c"] >= 2.0]
    print(f"\n=== {name}: {len(cand)} candidate windows (current gates) ===")
    for r in cand:
        print(f"  t={r['t']:7.2f}s key={r['key']} rms={r['rms']:.4f} "
              f"row_rel={r['row_rel']:.3f} col_rel={r['col_rel']:.3f} "
              f"twist={r['twist']:+.1f}dB dom=({r['dom_r']:.2f},{r['dom_c']:.2f}) "
              f"sel=({r['row_ratio']:.2f},{r['col_ratio']:.2f})")
    return cand


def strongest_tone_regions(name, rows):
    """Where is the strongest 697+1209-like energy (for locating real presses)."""
    scored = [r for r in rows if r["key"] == "1"]
    scored.sort(key=lambda r: -(r["row_rel"] + r["col_rel"]))
    print(f"--- {name}: top-5 '1'-like windows by relative energy ---")
    for r in scored[:5]:
        print(f"  t={r['t']:7.2f}s rms={r['rms']:.4f} row_rel={r['row_rel']:.3f} "
              f"col_rel={r['col_rel']:.3f} twist={r['twist']:+.1f} "
              f"dom=({r['dom_r']:.2f},{r['dom_c']:.2f}) sel=({r['row_ratio']:.2f},{r['col_ratio']:.2f})")


def main():
    for name, fn in FILES:
        sr, data = load(ATT / fn)
        ch = data.shape[1] if data.ndim > 1 else 1
        dur = len(data) / sr
        print(f"\n########## {name} sr={sr} ch={ch} dur={dur:.1f}s ##########")
        left = data[:, 0] if ch == 2 else data
        right = data[:, 1] if ch == 2 else None
        if right is not None:
            print(f"  L rms={np.sqrt(np.mean(left**2)):.4f}  R rms={np.sqrt(np.mean(right**2)):.4f}")
        rows = analyze_windows(left, sr)
        report_candidate_regions(name, rows)
        strongest_tone_regions(name, rows)


if __name__ == "__main__":
    main()
