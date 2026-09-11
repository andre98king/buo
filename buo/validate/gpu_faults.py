#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Rilevatore di fault GPU dal journal del kernel del boot corrente.

PERCHÉ esiste: sulle CU extra (40 CU) un fault GPU NON è recuperabile su
questa APU — niente GPU reset: freeze o schermo nero. I segnali osservabili
in journal sono quindi di tipo "crash totale": ring timeout / VM fault di
amdgpu, `GPU reset failed`, `amdgpu_job_timedout`. Il rilevatore NON
attribuisce la colpa a una WGP specifica (dal journal non si può: la
diagnosi per-WGP richiede il protocollo a reboot, non una lettura di log):
serve solo a dire "con le CU extra attive è comparso un fault ⇒ si torna a
24 CU". Lo consuma l'orchestratore; qui c'è la sola lettura, testabile e
iniettabile.

Un freeze SILENZIOSO non lascia righe in journal (incidente 30/08: zero
panic/oops/WHEA): quel caso si riconosce dal boot (marcatori di stato), non
da qui — vedi il design della validazione post-unlock.

C1: journal non leggibile / stato non determinabile → ``None``, mai un
esito inventato (né "pulito" né "in fault").
"""

import re
from typing import Callable, List, Optional, Sequence, Tuple

from ..utils.shell import run_command

# Firme di fault GPU (le stesse classi dello sweep/validazione: amdgpu
# reset/fault/timeout, ring gfx, VM_L2_PROTECTION), in minuscolo e match
# case-insensitive: sul campo un check case-sensitive ha già causato un
# falso negativo (lezione 11/09).
FAULT_PATTERNS: Tuple[str, ...] = (
    r"amdgpu.*ring \S+ timeout",            # [drm:amdgpu_job_timedout] ring gfx_0.0.0 timeout
    r"amdgpu_job_timedout",
    r"amdgpu.*gpu reset",                   # reset tentato/fallito = GPU già persa
    r"gpu reset failed",
    r"amdgpu.*failed to reset",
    r"vm_l2_protection_fault",
    r"amdgpu.*no-retry page fault",
    r"amdgpu.*gpu fault detected",
    r"amdgpu.*ring .* test failed",
    r"\bgpu hang\b",
)

# Rumore noto su questa APU: righe innocue (o di altro sottosistema) che
# NON sono indizi di CU guaste. Una riga che contiene uno di questi
# marcatori viene scartata anche se combacia con una firma.
NOISE_MARKERS: Tuple[str, ...] = (
    "dal_irq_service_dummy",
    "failed to clear hpd",
    "vendor infoframe",
    "mce: in-kernel mce decoding enabled",
)

_FAULT_RE = re.compile("|".join(FAULT_PATTERNS), re.IGNORECASE)
_NOISE_RE = re.compile("|".join(re.escape(m) for m in NOISE_MARKERS),
                       re.IGNORECASE)


def fault_signatures() -> Tuple[str, ...]:
    """Firme (regex) riconosciute come fault GPU — per documentazione,
    consumatori e test."""
    return FAULT_PATTERNS


def noise_markers() -> Tuple[str, ...]:
    """Rumore noto escluso dal rilevatore (mai segnalato come fault)."""
    return NOISE_MARKERS


def gpu_fault_since_boot(
        runner: Optional[Callable[..., Tuple[int, str, str]]] = None,
        timeout: int = 20) -> Optional[List[str]]:
    """Righe di fault GPU nel journal del kernel del BOOT CORRENTE.

    Solo `journalctl -b -k` (boot corrente, kernel: il journal completo è
    lento e pieno di rumore utente). ``None`` = non determinabile
    (journalctl assente, rc != 0, timeout: C1); ``[]`` = leggibile e
    pulito; altrimenti le righe di fault, senza alcuna attribuzione a una
    WGP.

    Consumatore: con CU extra attive un fault ⇒ rollback a 24 CU.
    """
    run = runner or run_command
    try:
        rc, out, _ = run(["journalctl", "-b", "-k", "--no-pager"],
                         timeout=timeout, sudo=True, check=False)
    except Exception:
        return None
    if rc != 0:
        return None
    return fault_lines((out or "").splitlines())


def fault_lines(lines: Sequence[str]) -> List[str]:
    """Righe di fault tra quelle date, meno il rumore noto (funzione pura:
    usata da gpu_fault_since_boot e dai test)."""
    return [ln for ln in lines
            if _FAULT_RE.search(ln) and not _NOISE_RE.search(ln)]


__all__ = ["FAULT_PATTERNS", "NOISE_MARKERS", "fault_lines",
           "fault_signatures", "gpu_fault_since_boot", "noise_markers"]
