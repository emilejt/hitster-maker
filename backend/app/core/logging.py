"""Logging setup for the app, and the namespaced logger the pipeline uses.

The pipeline logs its decisions (matches found, every deck add/swap, when a fallback tier
unlocks); those traces are how the deck builder's constants were tuned. Everything logs under
the `app.*` namespace, separate from uvicorn's own loggers.
"""

from __future__ import annotations

import logging
import sys

_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    """Attach a stdout handler to the `app` logger. Idempotent."""
    global _configured
    if _configured:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    app_logger.addHandler(handler)
    app_logger.propagate = False  # don't double-log through the root logger

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, e.g. get_logger('pipeline.matching') → 'app.pipeline.matching'."""
    return logging.getLogger(f"app.{name}")
