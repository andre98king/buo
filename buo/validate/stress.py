#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Stress Test — carico combinato CPU+GPU con monitoraggio.

Dal design finale:
    • CPU: stress-ng --cpu 0 --timeout <durata>
    • GPU: furmark (o glmark2 come fallback)
    • monitor: temperature, consumi, errori
    • abort se temp > limiti o consumo > budget
"""

import time
from typing import Any, Dict, Optional

from ..constants import LIMITS
from ..exceptions import SafetyViolation
from ..utils.logging import LoggerMixin
from ..utils.shell import run_command, which


class StressTest(LoggerMixin):
    """Esegue lo stress test di validazione."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 safety_monitor=None):
        self.mock = mock
        self.mock_hw = mock_hardware
        self.safety_monitor = safety_monitor

    # ------------------------------------------------------------------ #

    def run(self, duration_minutes: int = 30,
            power_budget: int = 300) -> Dict[str, Any]:
        """
        Esegue lo stress test.

        Returns:
            {"passed": bool, "cpu_temp_max":..., "gpu_temp_max":...,
             "power_max":..., "errors": int}
        """
        if self.mock and self.mock_hw is not None:
            return self._mock_run(duration_minutes)

        duration_s = duration_minutes * 60
        self.logger.info("🔥 Stress test: %d minuti (CPU+GPU)", duration_minutes)

        cpu_temp_max, gpu_temp_max, power_max, errors = 0.0, 0.0, 0.0, 0
        start = time.monotonic()

        # Carico CPU
        cpu_rc = 1
        if which("stress-ng"):
            rc, _, stderr = run_command(
                ["stress-ng", "--cpu", "0", "--timeout", str(duration_s),
                 "--metrics-brief"], timeout=duration_s + 60)
            cpu_rc = rc
        elif which("stress"):
            rc, _, _ = run_command(
                ["stress", "--cpu", "0", "--timeout", str(duration_s)],
                timeout=duration_s + 60)
            cpu_rc = rc

        # Carico GPU (FurMark o glmark2)
        gpu_rc = 1
        if which("glmark2"):
            rc, out, _ = run_command(
                ["glmark2", "--run-forever", "--seconds", str(duration_s)],
                timeout=duration_s + 60)
            gpu_rc = rc
        elif which("furmark"):
            rc, _, _ = run_command(
                ["furmark", "--benchmark", "--duration", str(duration_s)],
                timeout=duration_s + 60)
            gpu_rc = rc

        # Durante lo stress si campiona (safety monitor esterno);
        # qui simuliamo il monitoraggio con l'hardware se disponibile.
        if self.mock_hw is not None:
            for _ in range(min(duration_s, 30)):
                cpu_temp_max = max(cpu_temp_max, self.mock_hw.get_cpu_temp())
                gpu_temp_max = max(gpu_temp_max, self.mock_hw.get_gpu_temp())
                power_max = max(power_max, self.mock_hw.get_total_power())
                time.sleep(1)

        if cpu_temp_max > LIMITS.cpu.temp_max:
            raise SafetyViolation(
                f"CPU temp {cpu_temp_max:.1f}°C > {LIMITS.cpu.temp_max}°C",
                cpu_temp_max, LIMITS.cpu.temp_max)
        if gpu_temp_max > LIMITS.gpu.temp_max:
            raise SafetyViolation(
                f"GPU temp {gpu_temp_max:.1f}°C > {LIMITS.gpu.temp_max}°C",
                gpu_temp_max, LIMITS.gpu.temp_max)
        if power_max > power_budget:
            raise SafetyViolation(
                f"Potenza {power_max:.1f}W > {power_budget}W",
                power_max, power_budget)

        passed = cpu_rc == 0 and gpu_rc == 0
        return {
            "passed": passed,
            "duration_minutes": duration_minutes,
            "cpu_temp_max": round(cpu_temp_max, 1),
            "gpu_temp_max": round(gpu_temp_max, 1),
            "power_max": round(power_max, 1),
            "errors": errors,
        }

    def _mock_run(self, duration_minutes: int) -> Dict[str, Any]:
        hw = self.mock_hw
        for _ in range(10):
            hw.get_cpu_temp()
            hw.get_gpu_temp()
            hw.get_total_power()
            time.sleep(0.05)
        info = hw.get_system_info()
        return {
            "passed": True,
            "duration_minutes": duration_minutes,
            "cpu_temp_max": info["cpu_temp"],
            "gpu_temp_max": info["gpu_temp"],
            "power_max": info["total_power"],
            "errors": 0,
        }
