"""Volume and routing helpers. REAPER stores track volume as linear gain, the UI works in dB."""
import math
import re

from .errors import BadInput

# Voice in Nolzy from The Big Lez Show: 
# yenayenayenayena fuken y da fug right u need tests for?
# it's like fuken weed without fuken thc khantz
# oh right, u do need tests execpt this fuken vibecoded shit XD
# ENJOY!

MIN_DB = -60.0
MAX_DB = 12.0

_HW_MONO_FLAG = 1024  # I_DSTCHAN of a hardware output: low 10 bits = first channel, &1024 = mono
_HW_LABEL = re.compile(r"^Out (\d{1,4})(?:/(\d{1,4}))?$")


def db_to_gain(db: float) -> float:
    return 10 ** (db / 20)


def gain_to_db(gain: float) -> float:
    return max(MIN_DB, 20 * math.log10(gain)) if gain > 0 else MIN_DB


def hw_output_label(dest: int) -> str:
    """REAPER I_DSTCHAN of a hardware output -> 'Out 3/4' (stereo) or 'Out 3' (mono)."""
    first = dest & 1023
    return f"Out {first + 1}" if dest & _HW_MONO_FLAG else f"Out {first + 1}/{first + 2}"


def hw_output_dest(label: str) -> int:
    """'Out 3/4' / 'Out 3' -> REAPER I_DSTCHAN value."""
    match = _HW_LABEL.match(label.strip()) if isinstance(label, str) else None
    if not match:
        raise BadInput(f"Ismeretlen kimenet: {label}")
    first, second = int(match.group(1)), match.group(2)
    if first < 1 or (second is not None and int(second) != first + 1):
        raise BadInput(f"Ismeretlen kimenet: {label}")
    return first - 1 if second else (first - 1) | _HW_MONO_FLAG


def hw_output_labels(channels: int) -> list[str]:
    """Selectable outputs of a device with this many channels: stereo pairs first, then mono."""
    stereo = [f"Out {c + 1}/{c + 2}" for c in range(0, channels - 1, 2)]
    mono = [f"Out {c + 1}" for c in range(channels)]
    return stereo + mono
