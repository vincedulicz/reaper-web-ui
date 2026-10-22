"""Overview waveform: reads the audio files of the project's items and reduces them to peaks.

Optional dependencies: numpy + soundfile (pip install numpy soundfile). Without them the
waveform is flat, everything else keeps working.
"""
import logging
import math
import os

from .models import MediaItem

log = logging.getLogger(__name__)
_warned = False


def signature(items: list[MediaItem]) -> tuple:
    """Cheap fingerprint of the project content, used as a cache key."""
    out = []
    for it in items:
        try:
            mtime = os.path.getmtime(it.path)
        except OSError:
            mtime = 0
        out.append((it.path, it.position, it.length, it.offset, it.rate, mtime))
    return tuple(out)


def compute_peaks(items: list[MediaItem], length: float, buckets: int) -> list[float]:
    """Max absolute sample value per time bucket over [0, length], 0..1, all items mixed."""
    global _warned
    try:
        import numpy as np
        import soundfile as sf
    except ImportError:
        if not _warned:
            _warned = True
            log.warning("numpy/soundfile hiányzik, a hullámforma nem készül el (pip install numpy soundfile)")
        return [0.0] * buckets

    peaks = np.zeros(buckets)
    if length <= 0:
        return peaks.tolist()
    per = length / buckets

    for item in items:
        try:
            with sf.SoundFile(item.path) as f:
                sr = f.samplerate
                end = item.position + item.length
                first = max(0, int(item.position / per))
                last = min(buckets, math.ceil(end / per))
                for b in range(first, last):
                    t0 = max(b * per, item.position)
                    t1 = min((b + 1) * per, end)
                    if t1 <= t0:
                        continue
                    s0 = max(0, int((item.offset + (t0 - item.position) * item.rate) * sr))
                    s1 = min(f.frames, int((item.offset + (t1 - item.position) * item.rate) * sr))
                    if s1 <= s0:
                        continue
                    f.seek(s0)
                    data = f.read(s1 - s0, dtype="float32", always_2d=True)
                    peaks[b] = max(peaks[b], float(np.abs(data).max()))
        except Exception as exc:  # unreadable / unsupported file: skip, keep the rest
            log.debug("Hullámforma kihagyva (%s): %s", item.path, exc)

    top = max(float(peaks.max()), 0.1)  # floor so near-silence doesn't get blown up
    return [round(float(p) / top, 3) for p in peaks]
