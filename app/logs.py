"""Colored console logging: DEBUG blue, INFO green, WARNING yellow, ERROR red, CRITICAL white on red.

Colors turn off automatically when the output is not a terminal. Override with the
NO_COLOR (off) or FORCE_COLOR (on) environment variables.
"""
import logging
import os
import sys

RESET = "\033[0m"
BOLD = "\033[1m"
GREY = "\033[90m"
CYAN = "\033[36m"

_LEVEL_COLORS = {
    logging.DEBUG: "\033[94m",
    logging.INFO: "\033[92m",
    logging.WARNING: "\033[93m",
    logging.ERROR: "\033[91m",
    logging.CRITICAL: "\033[1;97;41m",
}


class ColorFormatter(logging.Formatter):
    def __init__(self, use_color: bool):
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        stamp = f"{self.formatTime(record, '%H:%M:%S')}.{int(record.msecs):03d}"
        where = f"{record.name}:{record.lineno}"
        level = f"{record.levelname:<8}"

        if self.use_color:
            color = _LEVEL_COLORS.get(record.levelno, "")
            if record.levelno >= logging.WARNING:
                message = f"{color}{message}{RESET}"
            text = f"{GREY}{stamp}{RESET} {color}{BOLD}{level}{RESET} {CYAN}{where:<30}{RESET} {message}"
        else:
            text = f"{stamp} {level} {where:<30} {message}"

        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            trace = record.exc_text
            text += "\n" + (f"{_LEVEL_COLORS[logging.ERROR]}{trace}{RESET}" if self.use_color else trace)
        return text


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stderr.isatty()


def setup_logging(level: str = "DEBUG") -> None:
    color = _supports_color()
    if color and os.name == "nt":
        os.system("")  # enables ANSI escape codes in the Windows console

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ColorFormatter(color))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())

    # we log every request ourselves (app/__init__.py), werkzeug's own access log would duplicate it
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
