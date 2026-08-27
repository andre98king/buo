#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Sistema di logging strutturato per BUO.

Usa `rich` se disponibile (CLI colorata), altrimenti un formatter standard.
"""

import logging
import sys
from pathlib import Path
from typing import Optional

from ..constants import LOG_DIR

try:  # rich è opzionale: la CLI ne dipende, il core no
    from rich.logging import RichHandler
    _HAS_RICH = True
except Exception:  # pragma: no cover
    _HAS_RICH = False


def setup_logging(level: str = "INFO",
                  log_file: Optional[Path] = None,
                  rich: bool = True) -> logging.Logger:
    """Configura il logger root di BUO e lo restituisce."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    logger = logging.getLogger("buo")
    logger.setLevel(numeric_level)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    logger.propagate = False

    if rich and _HAS_RICH:
        console_handler = RichHandler(
            rich_tracebacks=True,
            show_time=True,
            show_level=True,
            markup=True,
        )
    else:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        )
    console_handler.setLevel(numeric_level)
    logger.addHandler(console_handler)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        )
        logger.addHandler(file_handler)
    else:
        # Log di default (con fallback per utenti non-root)
        try:
            from ..utils.paths import log_file as _default_log
            path = _default_log()
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(str(path), encoding="utf-8")
            file_handler.setLevel(numeric_level)
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
                )
            )
            logger.addHandler(file_handler)
        except Exception:
            pass  # nessun permesso di scrittura: solo console

    return logger


def get_logger(name: str = "buo") -> logging.Logger:
    """Ottiene un logger con il prefisso 'buo'."""
    return logging.getLogger(f"buo.{name}")


class LoggerMixin:
    """Mixin che espone un logger per classe."""

    @property
    def logger(self) -> logging.Logger:
        if not hasattr(self, "_logger"):
            self._logger = get_logger(self.__class__.__name__)
        return self._logger
