"""
Audio capture engine — runs in a SEPARATE PROCESS.

ONLY handles capture: reads caller audio, resamples to 24kHz, sends to main process.
Output is handled directly by AudioManager in the main process.
"""
import collections
import time
import os
import sys
import numpy as np
from loguru import logger

# Pin BLAS threads in audio subprocesses (spawned processes don't inherit setdefault
# timing if parent already imported numpy with different settings).
for _thr_env in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
):
    os.environ.setdefault(_thr_env, "1")

# ── Windows DLL Loading Fix (Python 3.14+) ──
if sys.platform == 'win32' and sys.version_info >= (3, 8):
    try:
        import sounddevice
        # If sounddevice is already working, great.
    except OSError as e:
        if 'PortAudio library not found' in str(e):
            logger.warning("👷 Attempting manual PortAudio DLL recovery...")
            try:
                import site
                for path in site.getsitepackages():
                    # Standard path
                    dll_dir = os.path.join(path, '_sounddevice_data', 'portaudio-binaries')
                    if not os.path.exists(dll_dir):
                        # Alternative path for some installations
                        dll_dir = os.path.join(path, 'sounddevice_data', 'portaudio-binaries')
                    
                    if os.path.exists(dll_dir):
                        os.add_dll_directory(dll_dir)
                        logger.success(f"📌 Added DLL directory: {dll_dir}")
                        break
            except Exception as recovery_err:
                logger.error(f"❌ DLL recovery failed: {recovery_err}")

try:
    import soxr as _soxr
    _HAS_SOXR = True
except ImportError:
    _HAS_SOXR = False


def _resample_quality() -> str:
    """soxr quality: LQ default for multi-worker weak CPUs (override via env)."""
    q = os.getenv("AUDIO_RESAMPLE_QUALITY", "LQ").strip() or "LQ"
    return q


def resample(audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    if from_rate == to_rate or len(audio) == 0:
        return audio
    if _HAS_SOXR:
        return _soxr.resample(
            audio, from_rate, to_rate, quality=_resample_quality()
        ).astype(np.float32)
    ratio = to_rate / from_rate
    new_len = int(len(audio) * ratio)
    if new_len == 0:
        return audio
    return np.interp(
        np.linspace(0, len(audio) - 1, new_len),
        np.arange(len(audio)),
        audio
    ).astype(np.float32)


def find_wasapi(name_part: str, kind: str):
    """Find an audio device by name, preferring exact WASAPI matches first."""
    import sounddevice as sd

    def _channel_count(device):
        return device['max_input_channels'] if kind == 'input' else device['max_output_channels']

    def _host_name(device):
        try:
            return sd.query_hostapis(device['hostapi'])['name'].lower()
        except Exception:
            return ''

    def _normalize(name: str) -> str:
        return name.split('(')[0].strip().lower()

    target = name_part.strip().lower()
    target_base = _normalize(name_part)
    devices = sd.query_devices()

    def _find(require_wasapi: bool, exact: bool):
        for idx, device in enumerate(devices):
            if _channel_count(device) < 1:
                continue
            host = _host_name(device)
            if require_wasapi and 'wasapi' not in host:
                continue

            device_name = device['name'].strip().lower()
            device_base = _normalize(device['name'])
            if exact:
                if device_name == target or device_base == target_base:
                    return idx
            else:
                if target in device_name:
                    return idx
        return None

    for require_wasapi, exact in (
        (True, True),
        (False, True),
        (True, False),
        (False, False),
    ):
        match = _find(require_wasapi=require_wasapi, exact=exact)
        if match is not None:
            return match

    return None


def capture_process_main(capture_q, stop_event, device_name="Line 1"):
    """
    Capture subprocess. Only reads from microphone, resamples, and sends to main.
    No output, no playback — that's handled directly by AudioManager.
    """
    import sounddevice as sd

    logger.debug(f"🎧 Capture process started on device: {device_name}")

    DEEPGRAM_RATE = 24000
    try:
        SEND_INTERVAL = float(os.getenv("AUDIO_CAPTURE_SEND_INTERVAL_SEC", "0.080"))
    except ValueError:
        SEND_INTERVAL = 0.080
    try:
        BLOCKSIZE = int(os.getenv("AUDIO_CAPTURE_BLOCKSIZE", "4096"))
    except ValueError:
        BLOCKSIZE = 4096
    try:
        POLL_SEC = float(os.getenv("AUDIO_CAPTURE_POLL_SEC", "0.012"))
    except ValueError:
        POLL_SEC = 0.012
    quality = _resample_quality()

    # ── Find capture device ──
    cap_dev = find_wasapi(device_name, "input")
    if cap_dev is None:
        logger.error(f"❌ Capture device not found: {device_name}")
        return
    
    cap_info = sd.query_devices(cap_dev)
    cap_rate = int(cap_info['default_samplerate'])
    cap_ch = min(int(cap_info['max_input_channels']), 2)
    logger.debug(f"📥 Capture: [{cap_dev}] {cap_info['name']} @ {cap_rate}Hz")

    # Streaming resampler is far cheaper than oneshot soxr per block under load.
    stream_rs = None
    if cap_rate != DEEPGRAM_RATE and _HAS_SOXR:
        try:
            stream_rs = _soxr.ResampleStream(
                cap_rate, DEEPGRAM_RATE, 1, dtype="float32", quality=quality
            )
        except Exception as e:
            logger.warning(f"Streaming resampler unavailable ({e}); using oneshot")
            stream_rs = None

    # ── Capture callback ──
    cap_raw = collections.deque(maxlen=300)

    def cap_cb(indata, frames, time_info, status):
        cap_raw.append(indata.copy())

    try:
        cap_stream = sd.InputStream(
            device=cap_dev, samplerate=cap_rate, channels=cap_ch,
            dtype='float32', blocksize=max(512, BLOCKSIZE), latency='high',
            callback=cap_cb
        )
        cap_stream.start()
        logger.debug(
            f"✅ Capture stream started (blocksize {BLOCKSIZE}, soxr={quality})"
        )
    except Exception as e:
        logger.error(f"❌ Capture stream failed: {e}")
        return

    # ── Process loop: resample + batch send ──
    send_buf = bytearray()
    last_send = time.monotonic()

    while not stop_event.is_set():
        did_work = False

        while len(cap_raw) > 0:
            try:
                raw = cap_raw.popleft()
            except IndexError:
                break
            did_work = True

            if raw.ndim > 1 and raw.shape[1] > 1:
                mono = raw.mean(axis=1)
            else:
                mono = raw.flatten()

            if stream_rs is not None:
                dg = stream_rs.resample_chunk(mono, last=False)
            else:
                dg = resample(mono, cap_rate, DEEPGRAM_RATE)
            if len(dg) == 0:
                continue
            send_buf.extend((dg * 32767).astype(np.int16).tobytes())

        now = time.monotonic()
        if len(send_buf) > 0 and (now - last_send) >= SEND_INTERVAL:
            try:
                capture_q.put_nowait(bytes(send_buf))
            except Exception:
                pass
            send_buf.clear()
            last_send = now

        if not did_work:
            # Yield more aggressively on weak multi-worker hosts.
            time.sleep(max(0.005, POLL_SEC))

    logger.debug("🛑 Capture process stopping")
    cap_stream.stop()
    cap_stream.close()


def playback_process_main(playback_q, stop_event, device_name="Line 2"):
    """
    Playback subprocess. Manages the DAC (OutputStream) and drains commands.
    Immune to main process jitters.
    """
    import sounddevice as sd
    import queue as local_queue  # Avoid name clash with multiprocessing.Queue
    
    logger.debug(f"🔊 Playback process started on device: {device_name}")
    
    # ── Find output device ──
    out_dev = find_wasapi(device_name, "output")
    if out_dev is None:
        logger.error("❌ Output device not found")
        return
        
    out_info = sd.query_devices(out_dev)
    rate = int(out_info['default_samplerate'])
    ch = min(int(out_info['max_output_channels']), 2) or 1
    try:
        BLOCKSIZE = int(os.getenv("AUDIO_CAPTURE_BLOCKSIZE", "4096"))
    except ValueError:
        BLOCKSIZE = 4096
    
    logger.debug(f"📤 Output: [{out_dev}] {out_info['name']} @ {rate}Hz ch={ch}")
    
    try:
        out_stream = sd.OutputStream(
            device=out_dev, samplerate=rate, channels=ch,
            dtype='float32', blocksize=max(512, BLOCKSIZE)
        )
        out_stream.start()
        logger.debug(f"✅ Playback stream started (blocksize {BLOCKSIZE})")
    except Exception as e:
        logger.error(f"❌ Playback stream failed: {e}")
        return

    current_id = 0
    
    while not stop_event.is_set():
        try:
            # 1. Wait for command: (TYPE, ID, DATA)
            # Short timeout to allow check for stop_event
            cmd = playback_q.get(timeout=0.1)
            cmd_type, cmd_id, data = cmd
            
            if cmd_type == "FLUSH":
                # Clear all stale audio for this or older IDs
                current_id = max(current_id, cmd_id)
                logger.debug(f"🌊 Subprocess Flush (ID: {cmd_id})")
                
            elif cmd_type == "PLAY":
                if cmd_id < current_id:
                    # Stale audio, skip
                    continue
                
                # New audio session?
                if cmd_id > current_id:
                    current_id = cmd_id
                
                # Write to hardware
                if data is not None and len(data) > 0:
                    try:
                        # Data is already formatted as (N, channels) float32
                        out_stream.write(data)
                    except Exception as e:
                        logger.error(f"Subprocess stream write error: {e}")
                        
        except local_queue.Empty:
            continue
        except Exception as e:
            logger.error(f"Playback loop error: {e}")

    logger.debug("🛑 Playback process stopping")
    out_stream.stop()
    out_stream.close()
