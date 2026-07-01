"""Logging setup. Single logger, line-oriented, written to stderr."""
from __future__ import annotations

import logging
import os
import sys


def setup(level: str | None = None) -> logging.Logger:
    lvl_name = (level or os.getenv("MAOER_LOG_LEVEL") or "INFO").upper()
    lvl = getattr(logging, lvl_name, logging.INFO)
    # Force UTF-8 on stderr so Chinese usernames and emoji in log lines don't
    # mojibake (Windows defaults stderr to GBK) or crash the handler.
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-5s %(name)s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root.addHandler(handler)
    root.setLevel(lvl)
    # Quiet noisy libs.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return logging.getLogger("maoer")


log = logging.getLogger("maoer")
