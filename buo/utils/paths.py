#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Risoluzione dei percorsi di sistema con fallback per utenti non-root.

In produzione BUO usa /var/lib/buo, /var/log/buo, /etc/buo. Quando questi
non sono scrivibili (test, utenti senza root, sandbox), si ripiega su
percorsi nella home dell'utente. È possibile forzare la directory con la
variabile d'ambiente BUO_STATE_DIR.
"""

import os
from pathlib import Path

# Directory di stato di SISTEMA (usata da CLI per capire se lo stato letto
# è quello reale o un fallback locale: state_dir() == SYSTEM_STATE_DIR).
SYSTEM_STATE_DIR = Path("/var/lib/buo")


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        test = path / ".buo_write_test"
        test.write_text("ok")
        test.unlink()
        return True
    except Exception:
        return False


def state_dir() -> Path:
    """Directory di stato (checkpoint, report, backup)."""
    env = os.environ.get("BUO_STATE_DIR")
    if env:
        return Path(env)

    var = SYSTEM_STATE_DIR
    if _is_writable(var):
        return var

    fallback = Path.home() / ".local" / "state" / "buo"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def log_dir() -> Path:
    """Directory dei log."""
    var = Path("/var/log/buo")
    if _is_writable(var):
        return var
    fallback = state_dir() / "log"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def config_dir() -> Path:
    """Directory di configurazione."""
    var = Path("/etc/buo")
    if _is_writable(var):
        return var
    fallback = Path.home() / ".config" / "buo"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def state_file() -> Path:
    return state_dir() / "state.json"


def deps_dir() -> Path:
    """Directory dei checkout delle repo della community."""
    env = os.environ.get("BUO_DEPS_DIR")
    if env:
        return Path(env)

    var = Path("/opt/buo-deps")
    if _is_writable(var):
        return var

    fallback = Path.home() / ".local" / "share" / "buo-deps"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def report_file_md() -> Path:
    return state_dir() / "report.md"


def report_file_json() -> Path:
    return state_dir() / "report.json"


def log_file() -> Path:
    return log_dir() / "buo.log"


def undervolt_log_file() -> Path:
    """Log dedicato dell'undervolt, SEMPRE in home (leggibile anche quando
    BUO gira con sudo, che altrimenti scriverebbe in /var/log/buo)."""
    p = Path.home() / ".local" / "state" / "buo" / "undervolt.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
