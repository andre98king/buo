#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Auto-provvigionamento del tool di validazione GPU (design
research/DESIGN_AUTOPROVISION_GPU_TOOLS.md, P1/P2): `ensure_vkmark`.

vkmark è l'unico tool GPU la cui assenza blocca (a) la certificazione
40-CU post-unlock e (b) lo sweep per-silicio, ed è l'unico disponibile
come pacchetto distro con controllo durata reale. NON è una dep A7
(repo pinnata): è un pacchetto della distro → si installa col package
manager, MAI dal catalogo/bundle offline (P1).

PERCHÉ un modulo dedicato e non DependencyManager: il servizio è
chiamato da due punti indipendenti (validazione post-unlock e sweep)
con semantiche diverse — SEMPRE fail-soft (mai ConfigurationError:
un tool opzionale non blocca la run).
"""

import re
import shutil
from typing import Any, Callable, Dict, Optional, Tuple

# Dettaglio di errore troncato (mai stderr intero nei messaggi).
DETAIL_MAX = 200

# "already requested" / "already layered": un rpm-ostree install
# precedente è ancora in pending → niente doppio staging.
_ALREADY_STAGED_RE = re.compile(r"already\s+(?:requested|layered)",
                                re.IGNORECASE)

# Unità systemd staccata della txn (pattern ostree.py, mai killata).
OSTREE_TXN_UNIT = "buo-install-vkmark"

# (rc, stdout, stderr) di una installazione — runner iniettabili nei
# test (regola C1: MAI subprocess reali nei test).
CmdRunner = Callable[..., Tuple[int, str, str]]


def _truncate(text: str) -> str:
    text = (text or "").strip()
    return text[:DETAIL_MAX]


def ensure_vkmark(distro: Any = None,
                  txn_runner: Optional[CmdRunner] = None,
                  install_runner: Optional[CmdRunner] = None
                  ) -> Dict[str, Any]:
    """Assicura vkmark installato (tool di validazione GPU / sweep).

    Auto-install best-effort, MAI bloccante: ritorna sempre un dict di
    esito, mai eccezioni non gestite (i runner reali possono fallire:
    offline, pacchetto assente, non-root).

    - Idempotente: ``shutil.which("vkmark")`` → ok senza installare.
    - ostree (``pkg_manager == "rpm-ostree"``): ``rpm-ostree install
      vkmark`` come txn STACCATA via ``_run_ostree_txn`` (mai killata:
      rc 124 = il daemon completerà da solo); "already requested/layered"
      nello stderr = già staged → ok senza doppio staging; successo →
      ``needs_reboot: True`` (attivo al prossimo reboot).
    - non-ostree: ``distro.install_package`` (dnf/apt/pacman) → attivo
      subito (``needs_reboot: False``).
    - Fallimenti: ``status: "failed"`` + ``detail`` = stderr troncato.

    Runner/distro iniettabili nei test; in produzione i default
    costruiscono il percorso reale (``_run_ostree_txn`` /
    ``DistroInfo``).

    Returns:
        {"status": "ok"|"failed", "needs_reboot": bool,
         "detail": str, "installed": bool}
    """
    # Idempotenza PRIMA di qualunque chiamata (già presente → no-op).
    if shutil.which("vkmark"):
        return {"status": "ok", "installed": False,
                "needs_reboot": False, "detail": "vkmark già presente"}

    if distro is None:
        from ..utils.distro import DistroInfo
        distro = DistroInfo()

    try:
        if distro.pkg_manager == "rpm-ostree":
            return _install_ostree(txn_runner)
        runner = install_runner or distro.install_package
        rc, _, err = runner("vkmark")
        if rc != 0:
            return {"status": "failed", "installed": False,
                    "needs_reboot": False,
                    "detail": _truncate(err or f"installazione vkmark "
                                       f"fallita (rc {rc})")}
        return {"status": "ok", "installed": True, "needs_reboot": False,
                "detail": "vkmark installato (attivo subito)"}
    except Exception as e:  # runner rotto → failed pulito, mai crash
        return {"status": "failed", "installed": False,
                "needs_reboot": False,
                "detail": _truncate(str(e))}


def _install_ostree(txn_runner: Optional[CmdRunner]) -> Dict[str, Any]:
    """Layering ostree come txn staccata (mai cancellata a metà commit)."""
    if txn_runner is None:
        from ..state.ostree import _run_ostree_txn
        txn_runner = _run_ostree_txn
    rc, _, err = txn_runner(["rpm-ostree", "install", "vkmark"],
                            OSTREE_TXN_UNIT)
    if rc == 0 or _ALREADY_STAGED_RE.search(err or ""):
        return {"status": "ok", "installed": True, "needs_reboot": True,
                "detail": "vkmark installato (staged — attivo al prossimo "
                          "reboot)"}
    if rc == 124:
        # Timeout del CLIENT systemd-run: l'unità e la txn restano vive
        # (MAI killate — regola di campo AGENTS); il run successivo
        # verifica/ritenta.
        return {"status": "failed", "installed": False,
                "needs_reboot": False,
                "detail": ("txn rpm-ostree ancora attiva: NON ucciderla — "
                           "il daemon completerà da solo (verifica al "
                           "prossimo run)")}
    return {"status": "failed", "installed": False, "needs_reboot": False,
            "detail": _truncate(err or f"rpm-ostree install vkmark "
                                f"fallito (rc {rc})")}


__all__ = ["ensure_vkmark"]
