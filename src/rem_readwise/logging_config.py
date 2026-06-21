"""Logging configuration."""

from __future__ import annotations

import logging


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # rmscene can be chatty at DEBUG; keep it at WARNING unless we're debugging.
    if level.upper() != "DEBUG":
        logging.getLogger("rmscene").setLevel(logging.WARNING)
