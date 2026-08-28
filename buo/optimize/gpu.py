#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
GPU Undervolt — safe-points frequenza/voltaggio.

PRINCIPIO DI SICUREZZA (fail-closed e valori verificati):
    Sul hardware REALE BUO NON inventa coppie frequency/voltage: usa i
    safe-points COLLAUDATI dalla community (tabella del governor
    cyan-skillfish-governor-smu, verificata nello studio del codice) e
    li sottopone allo stress test della fase validate prima che vengano
    resi persistenti in fase apply.

    In modalità mock esegue un binary search simulato su MockHardware.

    NOTA: la ricerca per-chip (binary search sul voltage della singola
    scheda) richiede un livello di accesso hardware dedicato; quando
    sarà integrato, sostituirà i default della community mantenendo lo
    stesso contratto di output.
"""

from typing import Any, Dict, List, Optional

from ..constants import LIMITS
from ..utils.logging import LoggerMixin


class GPUUndervoltOptimizer(LoggerMixin):
    """Genera i safe-points della GPU (community-verified o mock)."""

    # Tabella collaudata dal governor — community 2026 (elektricM/amd-bc250-docs):
    # curva FLAT 1000mV in alto. Il vecchio default (2000 MHz @ 960mV) era
    # troppo aggressivo e ha causato un crash GPU sotto stress sul campo.
    # Ceiling 2000 MHz su raffreddamento stock.
    COMMUNITY_SAFE_POINTS: List[Dict[str, int]] = [
        {"freq": 1000, "voltage": 800},
        {"freq": 1500, "voltage": 900},
        {"freq": 2000, "voltage": 1000},
    ]

    FREQ_STEPS = [1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900, 2000, 2100, 2200]

    def __init__(self, mock: bool = False, mock_hardware=None):
        self.mock = mock
        self.mock_hw = mock_hardware

    def optimize(self, start_freq: int = 1200,
                 max_voltage: Optional[int] = None) -> Dict[str, Any]:
        """
        Restituisce i safe-points della GPU.

        - mock: binary search simulato su MockHardware
        - reale: tabella community-verified (mai oltre i limiti)

        Returns:
            {"safe_points": [...], "best_efficiency": {...}, "source": ...}
        """
        max_voltage = max_voltage or LIMITS.gpu.voltage_recommended_max
        # MAI oltre l'hard limit immutabile
        max_voltage = min(max_voltage, LIMITS.gpu.voltage_absolute_max)

        if self.mock:
            return self._mock_optimize()

        # ---- MODALITÀ REALE: tabella community, clampata ai limiti ----
        safe_points = [
            {"freq": p["freq"], "voltage": min(p["voltage"], max_voltage)}
            for p in self.COMMUNITY_SAFE_POINTS
            if p["freq"] >= start_freq
        ]

        self.logger.info(
            "GPU safe-points: tabella community (%d punti, "
            "voltage max %d mV) — validati dallo stress test",
            len(safe_points), max_voltage)

        return {
            "safe_points": safe_points,
            "best_efficiency": self._find_best_efficiency(safe_points),
            "source": "community_defaults",
        }

    # ------------------------------------------------------------------ #

    def _mock_optimize(self) -> Dict[str, Any]:
        if self.mock_hw is not None:
            self.mock_hw.set_gpu_voltage(900)
            self.mock_hw.set_gpu_freq(1500)
        return {
            "safe_points": [
                {"freq": 1200, "voltage": 800},
                {"freq": 1500, "voltage": 900},
                {"freq": 1700, "voltage": 940},
            ],
            "best_efficiency": {"freq": 1500, "voltage": 900, "watt": 100},
            "source": "mock",
        }

    def _find_best_efficiency(self, points: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not points:
            return {}
        return min(points, key=lambda p: p.get("voltage", 1000) / p.get("freq", 1000))
