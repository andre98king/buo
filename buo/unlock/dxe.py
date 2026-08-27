#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
CPU 8-Core Unlock permanente via DXE driver (RescueMei).

⚠️ AVVERTENZE dallo studio (messaggio 24):
    • progetto SUPERSEDED → V2: RescueMei/BC250-DXEv2-BIOSMOD
    • file .c non accessibile (404) — analisi basata sul README
    • "CAUTION: ONLY USE THIS ON BC250s THAT HAVE BEEN VERIFIED TO HAVE
      ALL 8 CPU CORES FUNCTIONAL VIA ANOTHER METHOD FIRST"
    • rollback NON documentato: richiede reflash del BIOS originale

BUO NON esegue il flash DXE automaticamente: lo tratta come operazione
manuale a rischio, con avvisi bloccanti e verifica dei prerequisiti.
"""

from typing import Any, Dict, Optional

from ..exceptions import SafetyViolation
from ..utils.logging import LoggerMixin


class DXECoreUnlock(LoggerMixin):
    """Gestione del DXE core unlock (permanente, a livello firmware)."""

    def __init__(self, mock: bool = False, mock_hardware=None):
        self.mock = mock
        self.mock_hw = mock_hardware

    def prerequisites_ok(self, cores_verified: bool) -> bool:
        """
        Verifica i prerequisiti prima di consentire il DXE unlock.

        Args:
            cores_verified: True se i core sono stati verificati funzionanti
                            con un altro metodo (health test CPU)
        """
        return cores_verified

    def apply(self, cores_verified: bool = False) -> Dict[str, Any]:
        """
        NON esegue il flash (operazione manuale rischiosa): verifica e
        fornisce le istruzioni precise.

        Returns:
            dict con stato e istruzioni
        """
        if not self.prerequisites_ok(cores_verified):
            raise SafetyViolation(
                "DXE core unlock: i core NON sono stati verificati "
                "funzionanti con un altro metodo. Bloccato per sicurezza."
            )

        if self.mock:
            return {
                "applied": True,
                "permanent": True,
                "method": "mock",
                "note": "Simulato — nessuna modifica reale al firmware",
            }

        return {
            "applied": False,
            "permanent": True,
            "method": "manual",
            "warning": (
                "Operazione MANUALE a rischio. Procedi solo se: "
                "1) i core sono verificati funzionanti; "
                "2) hai un backup del BIOS (opzione 0f del menu UEFI). "
                "Istruzioni: build con build_ffs.sh (podman), inserire il "
                ".ffs nel volume DXE AMI con UEFITool, flash via UEFI shell. "
                "Rollback: reflash del BIOS originale."
            ),
        }

    def rollback(self) -> bool:
        """Rollback non automatizzabile: richiede reflash del BIOS."""
        self.logger.warning(
            "DXE rollback NON documentato — serve reflash del BIOS originale "
            "(o rimozione del driver dal volume firmware con UEFITool)"
        )
        return False
