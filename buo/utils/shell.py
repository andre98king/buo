#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Esecuzione di comandi di sistema con timeout, sudo opzionale e cattura
dell'output. Usata da tutti i wrapper di script esterni.
"""

import shlex
import subprocess
from typing import List, Optional, Tuple

from ..exceptions import TimeoutError


def run_command(
    cmd: List[str],
    timeout: int = 60,
    sudo: bool = False,
    check: bool = False,
    capture: bool = True,
    cwd: Optional[str] = None,
) -> Tuple[int, str, str]:
    """
    Esegue un comando e restituisce (returncode, stdout, stderr).

    Args:
        cmd: comando e argomenti
        timeout: timeout in secondi
        sudo: antepone `sudo -n` (non interattivo)
        check: se True, solleva FileNotFoundError-like su returncode != 0
        capture: se True cattura l'output
        cwd: directory di lavoro del comando (default: ereditata)

    Raises:
        TimeoutError: se il comando supera il timeout
    """
    full_cmd: List[str] = []
    if sudo:
        full_cmd += ["sudo", "-n"]
    full_cmd += cmd

    try:
        result = subprocess.run(
            full_cmd,
            capture_output=capture,
            text=capture,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"Timeout dopo {timeout}s: {' '.join(shlex.quote(c) for c in full_cmd)}")
    except FileNotFoundError as e:
        return 127, "", f"comando non trovato: {cmd[0]} ({e})"

    stdout = result.stdout.strip() if result.stdout else ""
    stderr = result.stderr.strip() if result.stderr else ""

    if check and result.returncode != 0:
        raise RuntimeError(
            f"Comando fallito (exit {result.returncode}): "
            f"{' '.join(full_cmd)}\nstdout: {stdout}\nstderr: {stderr}"
        )

    return result.returncode, stdout, stderr


def which(tool: str) -> Optional[str]:
    """Cerca un eseguibile nel PATH; None se assente."""
    import shutil
    return shutil.which(tool)
