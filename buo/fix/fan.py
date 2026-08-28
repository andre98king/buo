#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Fan Control — driver SuperIO NCT6686/NCT6687 per sensori e ventole.

Dalla community (elektricM/amd-bc250-docs, sezione Sensors):
    • chip Nuvoton NCT6686D
    • `nct6683` (in-kernel): SOLO lettura sensori (temp/volt/ventole),
      NON controlla il PWM — richiede `force=true` (non auto-detectato)
    • `nct6687` (out-of-tree, Fred78290/nct6687d): lettura + SCRITTURA
      PWM per il controllo ventole software — richiede build del modulo
    • entrambi riportano i sensori come `nct6686-isa-0a20`
"""

from pathlib import Path
from typing import Any, Dict, Optional

from ..utils.logging import LoggerMixin
from ..utils.shell import run_command

MODULES_LOAD = Path("/etc/modules-load.d/nct6683.conf")
MODPROBE_OPTS = Path("/etc/modprobe.d/nct6683.conf")


class FanControl(LoggerMixin):
    """Abilitazione sensori/ventole SuperIO."""

    def __init__(self, mock: bool = False, mock_hardware=None):
        self.mock = mock
        self.mock_hw = mock_hardware

    def apply(self) -> Dict[str, Any]:
        """modprobe nct6683 force=true (sensori) + persistenza al boot.

        G7: senza persistenza il modulo si perde al reboot (i sensori
        spariscono). Si scrivono /etc/modules-load.d e /etc/modprobe.d
        (opzione force) così il modulo torna a ogni avvio.
        """
        if self.mock and self.mock_hw is not None:
            return {"applied": True, "module": "nct6683",
                    "needs_reboot": False}

        rc, out, err = run_command(
            ["modprobe", "nct6683", "force=true"], sudo=True)
        if rc != 0:
            # Prova senza force (alcuni kernel lo accettano)
            rc, out, err = run_command(["modprobe", "nct6683"], sudo=True)
        persisted = self._persist() if rc == 0 else False
        return {
            "applied": rc == 0,
            "persisted": persisted,
            "module": "nct6683",
            "needs_reboot": False,
            "stderr": err,
            "note": (
                "Per il controllo PWM completo installare il driver "
                "out-of-tree nct6687 (Fred78290/nct6687d)"
                if rc != 0 else None
            ),
        }

    def _persist(self) -> bool:
        """G7: carica il modulo a ogni boot (modules-load.d + modprobe.d)."""
        ok = True
        try:
            MODULES_LOAD.write_text("nct6683\n")
        except Exception:
            ok = False
        try:
            MODPROBE_OPTS.write_text("options nct6683 force=true\n")
        except Exception:
            ok = False
        return ok

    def verify(self) -> bool:
        """True se il modulo risulta caricato."""
        if self.mock and self.mock_hw is not None:
            return True
        rc, out, _ = run_command(["lsmod"], check=False)
        return rc == 0 and "nct668" in out

    def rollback(self) -> bool:
        """Rimuove il modulo e la persistenza (G7). Non bloccante."""
        removed = True
        for p in (MODULES_LOAD, MODPROBE_OPTS):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                removed = False
        rc, _, _ = run_command(["rmmod", "nct6683"], sudo=True, check=False)
        return rc == 0 and removed
