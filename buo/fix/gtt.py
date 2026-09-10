#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
GTT Tuning — aumento del limite GTT (memoria GPU accessibile).

Dallo studio (messaggio 94): Vulkan vede ~10GB di 12GB e il driver
amdgpu limita il GTT a ~7.4 GiB. La soluzione è alzare
`ttm.pages_limit` (e `ttm.page_pool_size`) via modprobe.

Valore consigliato dalla community: ttm.pages_limit=3959290 (~15 GiB)
oppure 4194304 per 16 GiB.
"""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from ..state.ostree import _run_ostree_txn
from ..utils.distro import detect_distro
from ..utils.logging import LoggerMixin
from ..utils.shell import run_command

GTT_LIMIT_DEFAULT = 3959290
GTT_CONF = "/etc/modprobe.d/buo-gtt.conf"
# Parametro EFFETTIVO a runtime: il conf in /etc entra nell'initramfs SOLO
# se la rigenerazione è abilitata (su ostree è OFF di default) → senza
# questo controllo il fix risulterebbe "applicato" ma inerte (bug campo
# 10/09/2026: runtime 1944679 vs 3959290 configurato).
GTT_PARAM_PATH = "/sys/module/ttm/parameters/pages_limit"

CmdRunner = Callable[..., Tuple[int, str, str]]


class GTTTuning(LoggerMixin):
    """Aumenta il limite GTT della GPU."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 pages_limit: int = GTT_LIMIT_DEFAULT,
                 param_path: Optional[str] = None,
                 ostree_runner: Optional[CmdRunner] = None):
        self.mock = mock
        self.mock_hw = mock_hardware
        self.pages_limit = pages_limit
        self.param_path = (Path(param_path) if param_path
                           else Path(GTT_PARAM_PATH))
        self._ostree_txn = ostree_runner or _run_ostree_txn

    def verify(self) -> bool:
        """True se il limite GTT è EFFETTIVO a runtime.

        Non basta il file di conf: su ostree senza rigenerazione
        dell'initramfs il parametro resta quello di default. Parametro
        non leggibile → False (fail-closed: non si dichiara ciò che non
        si è verificato).
        """
        if self.mock and self.mock_hw is not None:
            return True  # mock: assumiamo applicato se richiesto
        try:
            return int(self.param_path.read_text().strip()) >= self.pages_limit
        except (OSError, ValueError):
            return False

    @staticmethod
    def _is_ostree() -> bool:
        try:
            return detect_distro().initramfs_tool == "ostree"
        except Exception:
            return False

    def apply(self) -> Dict[str, Any]:
        """Scrive /etc/modprobe.d/buo-gtt.conf e, su ostree, ABILITA la
        rigenerazione dell'initramfs (senza la quale il conf è inerte).

        Se la rigenerazione non riesce il fix NON è applicato: si ritorna
        `applied: False` con warning (fail-honest, mai "applicato" per un
        file scritto ma inefficace). Mai eccezioni.
        """
        if self.mock and self.mock_hw is not None:
            return {"applied": True, "pages_limit": self.pages_limit,
                    "needs_reboot": True}

        content = (
            "# BUO GTT tuning — aumenta la memoria GPU accessibile\n"
            f"options ttm pages_limit={self.pages_limit}\n"
            f"options ttm page_pool_size={self.pages_limit}\n"
        )
        try:
            Path("/etc/modprobe.d").mkdir(parents=True, exist_ok=True)
            # Scrittura diretta (BUO gira da root); se i permessi non lo
            # consentono si passa da `install` con sudo (nessuna shell).
            try:
                Path(GTT_CONF).write_text(content, encoding="utf-8")
            except OSError:
                tmpdir = tempfile.mkdtemp(prefix="buo-gtt-")
                try:
                    src = Path(tmpdir) / "buo-gtt.conf"
                    src.write_text(content, encoding="utf-8")
                    rc, _, err = run_command(
                        ["install", "-m", "644", str(src), GTT_CONF],
                        sudo=True)
                    if rc != 0:
                        return {"applied": False, "error": err}
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception as e:
            return {"applied": False, "error": str(e)}

        # ostree: /etc/modprobe.d non è nell'initramfs pre-generato →
        # abilita la rigenerazione (txn rpm-ostree staccata: mai uccisa a
        # metà commit) e riporta l'esito REALE.
        if self._is_ostree():
            rc, _o, err = self._ostree_txn(
                ["rpm-ostree", "initramfs", "--enable"],
                "buo-gtt-initramfs")
            if rc != 0:
                return {
                    "applied": False, "needs_reboot": True,
                    "initramfs": "error",
                    "warning": ("conf scritto ma initramfs NON rigenerato "
                                f"(rpm-ostree initramfs --enable rc={rc}): "
                                "il parametro resta inerte — "
                                + (err or "").strip()[:120]),
                }
        return {"applied": True, "pages_limit": self.pages_limit,
                "needs_reboot": True}

    def rollback(self) -> bool:
        """Rimuove il file modprobe.

        Guard mock (stesso pattern di apply/verify): MAI rm di /etc in
        modalità simulata (`buo rollback --mock`).
        """
        if self.mock and self.mock_hw is not None:
            return True
        if os.path.exists(GTT_CONF):
            rc, _, _ = run_command(["rm", "-f", GTT_CONF], sudo=True)
            return rc == 0
        return True
