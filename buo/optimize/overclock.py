#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Overclock power-limited — spinge CPU/GPU fino al power budget.

Dal design: partendo dalle V/F curve (undervolt), alza la frequenza
finché il consumo stimato non supera il budget (default 300W per un
PSU da 350W, con margine di sicurezza). Sceglie il punto di massima
efficienza (performance/watt).
"""

from typing import Any, Dict, List, Optional

from ..constants import LIMITS
from ..utils.logging import LoggerMixin


class OverclockOptimizer(LoggerMixin):
    """Overclock entro il power budget."""

    def __init__(self, mock: bool = False, mock_hardware=None):
        self.mock = mock
        self.mock_hw = mock_hardware

    # ------------------------------------------------------------------ #

    def optimize_cpu(self, vf_points: List[Dict[str, Any]],
                     power_budget: int = 300) -> Dict[str, Any]:
        """Frequenza CPU massima entro il budget."""
        if self.mock:
            return self._mock_cpu()

        max_freq, max_vid, max_power = None, None, 0
        for point in sorted(vf_points, key=lambda p: p["freq"]):
            power = self._estimate_cpu_power(point["freq"], point["vid"])
            if power > power_budget:
                break
            max_freq, max_vid, max_power = point["freq"], point["vid"], power

        return {
            "max_freq": max_freq,
            "vid_at_max": max_vid,
            "power_at_max": round(max_power, 1),
            "recommended_freq": max_freq,
            "recommended_vid": max_vid,
        }

    def optimize_gpu(self, safe_points: List[Dict[str, Any]],
                     power_budget: int = 300) -> Dict[str, Any]:
        """Frequenza GPU massima entro il budget."""
        if self.mock:
            return self._mock_gpu()

        max_freq, max_volt, max_power = None, None, 0
        for point in sorted(safe_points, key=lambda p: p["freq"]):
            power = self._estimate_gpu_power(point["freq"], point["voltage"])
            if power > power_budget:
                break
            max_freq, max_volt, max_power = point["freq"], point["voltage"], power

        return {
            "max_freq": max_freq,
            "voltage_at_max": max_volt,
            "power_at_max": round(max_power, 1),
            "recommended_freq": max_freq,
            "recommended_voltage": max_volt,
        }

    # --------------------- stime di consumo -------------------------- #

    @staticmethod
    def _estimate_cpu_power(freq: int, vid: int) -> float:
        # Modello semplificato della community (dal design)
        base = 65.0
        return base * (freq / 3500.0) * (vid / 1206.0)

    @staticmethod
    def _estimate_gpu_power(freq: int, volt: int) -> float:
        base = 85.0
        return base * (freq / 1500.0) * (volt / 1050.0)

    # --------------------------- mock -------------------------------- #

    def _mock_cpu(self) -> Dict[str, Any]:
        if self.mock_hw is not None:
            self.mock_hw.set_cpu_freq(3800)
            self.mock_hw.set_cpu_vid(1040)
        return {"max_freq": 3800, "vid_at_max": 1040, "power_at_max": 72,
                "recommended_freq": 3700, "recommended_vid": 1012}

    def _mock_gpu(self) -> Dict[str, Any]:
        if self.mock_hw is not None:
            self.mock_hw.set_gpu_freq(1700)
            self.mock_hw.set_gpu_voltage(940)
        return {"max_freq": 1700, "voltage_at_max": 940, "power_at_max": 130,
                "recommended_freq": 1500, "recommended_voltage": 900}
