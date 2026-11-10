"""Self-test: call recorder WAV finalize + cleanup."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    import numpy as np
    from audio.call_recorder import CallRecorder, read_recording_b64

    rec = CallRecorder(tag="selftest")
    tone = (np.sin(np.linspace(0, 1, 2400)) * 16000).astype(np.int16).tobytes()
    rec.append_remote(tone)
    rec.append_playback_float32(
        np.sin(np.linspace(0, 1, 480)).astype(np.float32).tobytes(), 48000
    )
    # Capture is already running when playback starts, so the prompt must sit
    # on the same timeline (local file at least as long as the remote leg).
    paths = rec.finalize()
    assert paths and paths.get("call") and not paths.get("remote")
    import wave
    with wave.open(str(paths["call"])) as wav:
        assert wav.getnchannels() == 2
        assert wav.getframerate() == 24000
        frames = wav.readframes(wav.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16)
    left, right = samples[0::2], samples[1::2]
    assert left.size == right.size and left.size > 2400
    assert int(np.max(np.abs(left[:2400]))) > 1000
    assert int(np.max(np.abs(right[:2400]))) == 0
    assert int(np.max(np.abs(right[2400:]))) > 1000
    b64 = read_recording_b64(paths["call"])
    assert b64 and len(b64) > 100
    CallRecorder.delete_paths(paths)
    assert not paths["call"].is_file()
    assert rec.finalize() is None  # second finalize is a no-op
    print("PASS call recorder")


if __name__ == "__main__":
    main()
