"""Stress-test the detector with synthetic DTMF presses injected into real
call backgrounds: sweep level, duration, detune, twist, and keys '1'/'2'."""
import sys
import numpy as np
from scipy.io import wavfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio.dtmf import DTMFDetector

ATT = Path(r"C:\Users\konon\.kimi-code\sessions\wd_p1_from_great_maskorad_3eee7114b542\session_61858712-8f14-483c-b7ac-46d5788aba08\attachments")
SR = 24000

KEY_FREQS = {"1": (697.0, 1209.0), "2": (697.0, 1336.0), "5": (770.0, 1336.0),
             "9": (852.0, 1477.0), "0": (941.0, 1336.0)}


def load_bg(fn="f_17859eec-ad28-4868-b469-d217156dcf57-fake_p1_1.wav"):
    sr, d = wavfile.read(ATT / fn)
    d = d.astype(np.float32)
    if np.abs(d).max() > 100:
        d /= 32768.0
    return d[:, 0]


def tone(key, dur_ms, rms, detune_pct=0.0, twist_db=0.0):
    n = int(SR * dur_ms / 1000)
    t = np.arange(n) / SR
    fr, fc = KEY_FREQS[key]
    fr *= 1 + detune_pct / 100
    fc *= 1 + detune_pct / 100
    col_gain = 10 ** (twist_db / 20)
    x = np.sin(2 * np.pi * fr * t) + col_gain * np.sin(2 * np.pi * fc * t)
    x /= np.sqrt(np.mean(x ** 2))
    # 5ms raised-cosine edges like a real keypad
    e = int(SR * 0.005)
    env = np.ones(n)
    env[:e] = 0.5 - 0.5 * np.cos(np.pi * np.arange(e) / e)
    env[-e:] = 0.5 - 0.5 * np.cos(np.pi * np.arange(e) / e)[::-1]
    return (x * env * rms).astype(np.float32)


def run(x):
    det = DTMFDetector(sample_rate=SR, window_ms=80, step_ms=40,
                       persistence=2, cooldown_ms=300)
    chunk = int(SR * 0.02)
    hits = []
    for s in range(0, len(x), chunk):
        k = det.feed(x[s:s + chunk])
        if k is not None:
            hits.append((s / SR, k))
    return hits


def find_quiet_spot(bg, need_ms=600, max_rms=0.01):
    fl = int(SR * 0.05)
    nfr = len(bg) // fl
    fr = np.sqrt(np.mean(bg[:nfr * fl].reshape(nfr, fl) ** 2, axis=1))
    need = need_ms // 50
    for i in range(nfr - need):
        if np.all(fr[i:i + need] < max_rms):
            return (i * fl) / SR
    return 10.0


def inject(bg, tn, at_s):
    out = bg.copy()
    i = int(at_s * SR)
    out[i:i + len(tn)] += tn
    return out


def main():
    bg = load_bg()
    fails = 0
    QUIET = find_quiet_spot(bg)
    print(f"quiet injection spot: {QUIET:.2f}s")

    print("== level sweep (key 1, 150ms) ==")
    for rms in (0.15, 0.1, 0.05, 0.03, 0.02, 0.015, 0.01, 0.007, 0.005):
        hits = run(inject(bg, tone("1", 150, rms), QUIET))
        got = [k for _, k in hits]
        ok = got == ["1"]
        fails += not ok
        print(f"  rms={rms:.3f}: {'OK' if ok else 'FAIL'} {hits}")

    print("== duration sweep (key 1, rms=0.1) ==")
    for dur in (40, 60, 80, 100, 150, 250, 500):
        hits = run(inject(bg, tone("1", dur, 0.1), QUIET))
        got = [k for _, k in hits]
        ok = got == ["1"]
        fails += not ok
        print(f"  dur={dur:3d}ms: {'OK' if ok else 'FAIL'} {hits}")

    print("== detune sweep (key 1, 150ms, rms=0.1) ==")
    for d in (-1.4, -1.0, -0.5, 0.0, 0.5, 1.0, 1.4):
        hits = run(inject(bg, tone("1", 150, 0.1, detune_pct=d), QUIET))
        got = [k for _, k in hits]
        ok = got == ["1"]
        fails += not ok
        print(f"  detune={d:+.1f}%: {'OK' if ok else 'FAIL'} {hits}")

    print("== twist sweep (key 1, 150ms, rms=0.1) ==")
    for tw in (-6, -3, 0, 3, 6, 8):
        hits = run(inject(bg, tone("1", 150, 0.1, twist_db=tw), QUIET))
        got = [k for _, k in hits]
        ok = got == ["1"]
        fails += not ok
        print(f"  twist={tw:+d}dB: {'OK' if ok else 'FAIL'} {hits}")

    print("== key coverage (150ms, rms=0.1) ==")
    for key in ("1", "2", "5", "9", "0"):
        hits = run(inject(bg, tone(key, 150, 0.1), QUIET))
        got = [k for _, k in hits]
        ok = got == [key]
        fails += not ok
        print(f"  key={key}: {'OK' if ok else 'FAIL'} {hits}")

    print("== double press (2x key 1, 400ms apart) ==")
    out = inject(bg, tone("1", 150, 0.1), QUIET)
    i = int((QUIET + 0.55) * SR)
    t2 = tone("1", 150, 0.1)
    out[i:i + len(t2)] += t2
    hits = run(out)
    ok = [k for _, k in hits] == ["1", "1"]
    fails += not ok
    print(f"  {'OK' if ok else 'FAIL'} {hits}")

    print(f"\n{'ALL PASS' if fails == 0 else f'{fails} FAILURES'}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
