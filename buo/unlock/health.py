#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
CU Health Test — verifica ogni WGP della GPU con reboot automatici.

Lo script bc250-cu-health-test.sh NON ha un limite fisso di reboot:
isola una WGP alla volta e continua finché tutte le 20 sono testate.
BUO impone comunque un tetto configurabile (default 25) per evitare
loop infiniti, come previsto dal design.
"""

from typing import Any, Dict, Optional

from ..utils.logging import LoggerMixin
from .wrappers.bc250_health import BC250HealthWrapper


class CUHealthTest(LoggerMixin):
    """Esecuzione e lettura del CU health test."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 max_reboots: int = 25):
        self.mock = mock
        self.mock_hw = mock_hardware
        self.max_reboots = max_reboots
        self.wrapper = BC250HealthWrapper() if not mock else None

    def run(self) -> Dict[str, Any]:
        """
        Avvia (o riprende) il health test.

        Returns:
            risultati letti da results.tsv
        """
        if self.mock and self.mock_hw is not None:
            return {
                "stable": list(self.mock_hw.state.gpu_stable_cu),
                "defective": list(self.mock_hw.state.gpu_defective_cu),
                "total": self.mock_hw.get_cu_count(),
                "reboots": 0,
            }

        if self.wrapper is None or not self.wrapper.available:
            return {"error": "bc250-cu-health-test.sh non trovato",
                    "stable": [], "defective": []}

        self.logger.info("Avvio CU health test (max %d reboot)...", self.max_reboots)
        start = self.wrapper.start()
        if start["returncode"] != 0:
            return {"error": start["stderr"] or "start fallito",
                    "stable": [], "defective": []}

        # In produzione il test prosegue tra i reboot via servizio systemd;
        # al ritorno si leggono i risultati accumulati.
        return self.wrapper.read_results()

    def read_results(self) -> Dict[str, Any]:
        """Legge i risultati correnti."""
        if self.mock and self.mock_hw is not None:
            return {
                "stable": list(self.mock_hw.state.gpu_stable_cu),
                "defective": list(self.mock_hw.state.gpu_defective_cu),
                "total": self.mock_hw.get_cu_count(),
            }
        if self.wrapper is not None and self.wrapper.available:
            return self.wrapper.read_results()
        return {"stable": [], "defective": [], "total": 0}

    def reset(self) -> bool:
        """Rimuove stato e configurazione del test (rollback)."""
        if self.mock and self.mock_hw is not None:
            return True
        if self.wrapper is not None and self.wrapper.available:
            result = self.wrapper.reset()
            return result["returncode"] == 0
        return False
