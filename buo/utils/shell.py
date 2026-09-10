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


def stress_cwd() -> str:
    """Directory di lavoro SCRIVIBILE per i tool di stress.

    Bug di campo 10/09 (BC-250): `stress-ng` usa la CWD come temp-path e
    aborta all'istante con «temp-path '.' must be readable and writeable» se
    non è scrivibile. Dentro un'unità systemd la CWD è `/`, che su ostree è
    READ-ONLY → `stress-ng` esce rc=1 in meno di un secondo, la validate
    fallisce SEMPRE e il rollback automatico (T2) disinstalla una config
    buona. Le run lunghe sul campo si lanciano PROPRIO come unità transiente
    (`systemd-run`), quindi il caso non è teorico.

    `/tmp` (tempfile.gettempdir()) è sempre scrivibile anche su ostree.
    """
    import tempfile
    return tempfile.gettempdir()
