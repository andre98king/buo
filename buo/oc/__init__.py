#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Tool OC integrato in BUO (design research/DESIGN_BUO_OC_TUI.md).

Il motore di ricerca OC resta bash (`oc3600.sh`, deployato su
/var/lib/buo/oc): questo pacchetto lo CONTROLLA (lancio/stop/status),
gestisce i profili (Stock/Certificato/Custom) e l'apply con smoke 30s e
rollback automatico, ed espone la cockpit `buo oc-tui`.

Import lazy: i moduli pesanti (click, textual) si caricano solo al bisogno.
"""

from ..utils.logging import get_logger

logger = get_logger("buo.oc")


def _lazy(name: str):
    """Import pigro del modulo `buo.oc.<name>` (pattern BUO)."""
    import importlib
    return importlib.import_module(f"{__name__}.{name}")


def get_controller(*args, **kwargs):
    return _lazy("controller").OcController(*args, **kwargs)


def get_profile_store(*args, **kwargs):
    return _lazy("profiles").ProfileStore(*args, **kwargs)


def get_apply_manager(*args, **kwargs):
    return _lazy("apply").ApplyManager(*args, **kwargs)


__all__ = ["get_controller", "get_profile_store", "get_apply_manager"]
