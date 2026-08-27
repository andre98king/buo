#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Fan Control — driver SuperIO NCT6686/NCT6687 per sensori e ventole.

Dallo studio:
    • chip Nuvoton NCT6686D: serve `modprobe nct6683 force=true`
    • driver out-of-tree nct6687 (Fred78290/nct6687d) per PWM ventole
    • kernel 6.15+ ha supporto nativo per temperature GPU via nct6683
"""

from typing import Any, Dict, Optional

from ..utils.logging import LoggerMixin
from ..utils.shell import run_command


class FanControl(LoggerMixin):
    """Abilitazione sensori/ventole SuperIO."""

    def __init__(self, mock: bool = False, mock_hardware=None):
        self.mock = mock
        self.mock_hw = mock_hardware

    def apply(self) -> Dict[str, Any]:
        """modprobe nct6683 force=true (sensori) — base per il PWM."""
        if self.mock and self.mock_hw is not None:
            return {"applied": True, "module": "nct6683", "needs_reboot": False}

        rc, out, err = run_command(
            ["modprobe", "nct6683", "force=true"], sudo=True)
        if rc != 0:
            # Prova senza force (alcuni kernel lo accettano)
            rc, out, err = run_command(["modprobe", "nct6683"], sudo=True)
        return {
            "applied": rc == 0,
            "module": "nct6683",
            "needs_reboot": False,
            "stderr": err,
            "note": (
                "Per il controllo PWM completo installare il driver "
                "out-of-tree nct6687 (Fred78290/nct6687d)"
                if rc != 0 else None
            ),
        }

    def verify(self) -> bool:
        """True se il modulo risulta caricato."""
        if self.mock and self.mock_hw is not None:
            return True
        rc, out, _ = run_command(["lsmod"], check=False)
        return rc == 0 and "nct668" in out

    def rollback(self) -> bool:
        """Rimuove il modulo (se caricato)."""
        rc, _, _ = run_command(["rmmod", "nct6683"], sudo=True, check=False)
        return True  # rmmod può fallire se in uso: non bloccante
