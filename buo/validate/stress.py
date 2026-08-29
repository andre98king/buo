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

Fix C2: il carico viene eseguito con campionamento LIVE ogni secondo
(letture reali hwmon via RealHardwareReader, o mock nei test). Al
superamento di un limite o su violazione del safety monitor il processo
di stress viene TERMINATO e si solleva SafetyViolation — non si aspetta
la fine del run.
"""

import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..constants import LIMITS
from ..exceptions import SafetyViolation
from ..utils.logging import LoggerMixin
from ..utils.shell import which


class StressTest(LoggerMixin):
    """Esegue lo stress test di validazione."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 safety_monitor=None, reader=None):
        self.mock = mock
        self.mock_hw = mock_hardware
        self.safety_monitor = safety_monitor
        self._reader_override = reader  # iniettabile nei test
        self.deadline_grace = 60  # secondi oltre la durata (iniettabile)

    # ------------------------------------------------------------------ #

    def _get_reader(self) -> Any:
        """Reader: override nei test, altrimenti mock o reale."""
        if self._reader_override is not None:
            return self._reader_override
        if self.mock_hw is not None:
            return self.mock_hw
        from ..safety.reader import RealHardwareReader
        return RealHardwareReader()

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

        # BUG DI CAMPO (29/08/2026): con durata 0 si spawnava comunque
        # `stress-ng --timeout 0`, che in stress-ng significa NESSUN
        # timeout (stress all'infinito): il "test saltato" caricava tutti
        # i core CPU per fino a deadline_grace secondi, portando la CPU a
        # ~90°C e facendo scattare un falso abort termico (3 volte sul
        # campo). Durata 0 = SKIP vero: nessun spawn, nessun carico.
        if duration_s <= 0:
            self.logger.info("   Stress test saltato (durata 0)")
            return {
                "passed": True, "skipped": True,
                "duration_minutes": duration_minutes,
                "cpu_temp_max": None, "gpu_temp_max": None,
                "power_max": None, "errors": 0,
            }

        reader = self._get_reader()
        cpu_temp_max, gpu_temp_max, power_max = 0.0, 0.0, 0.0

        # Carico CPU (con campionamento live)
        cpu_rc = 1
        if which("stress-ng"):
            cpu_rc, t1, t2, p = self._run_loaded(
                ["stress-ng", "--cpu", "0", "--timeout", str(duration_s),
                 "--metrics-brief"], duration_s, reader, power_budget)
            cpu_temp_max, gpu_temp_max, power_max = t1, t2, p
        elif which("stress"):
            cpu_rc, t1, t2, p = self._run_loaded(
                ["stress", "--cpu", "0", "--timeout", str(duration_s)],
                duration_s, reader, power_budget)
            cpu_temp_max, gpu_temp_max, power_max = t1, t2, p

        # Carico GPU (FurMark o glmark2)
        gpu_rc = 1
        if which("glmark2"):
            gpu_rc, t1, t2, p = self._run_loaded(
                ["glmark2", "--run-forever", "--seconds", str(duration_s)],
                duration_s, reader, power_budget)
            cpu_temp_max = max(cpu_temp_max, t1)
            gpu_temp_max = max(gpu_temp_max, t2)
            power_max = max(power_max, p)
        elif which("furmark"):
            gpu_rc, t1, t2, p = self._run_loaded(
                ["furmark", "--benchmark", "--duration", str(duration_s)],
                duration_s, reader, power_budget)
            cpu_temp_max = max(cpu_temp_max, t1)
            gpu_temp_max = max(gpu_temp_max, t2)
            power_max = max(power_max, p)

        passed = cpu_rc == 0 and gpu_rc == 0
        return {
            "passed": passed,
            "duration_minutes": duration_minutes,
            "cpu_temp_max": round(cpu_temp_max, 1),
            "gpu_temp_max": round(gpu_temp_max, 1),
            "power_max": round(power_max, 1),
            "errors": 0,
        }

    # ------------------------------------------------------------------ #

    def _run_loaded(self, cmd: List[str], duration_s: int, reader: Any,
                    power_budget: int,
                    on_tick: Optional[Callable[[], None]] = None
                    ) -> Tuple[int, float, float, float]:
        """Esegue un comando di stress con campionamento LIVE e abort.

        Ogni secondo campiona temperature/potenza (reali o mock) e invoca
        `on_tick` (hook per letture aggiuntive durante il carico, es. la
        VDDGFX sotto carico del probe sweep GPU — fix 30/08). Se un
        limite viene superato, o il safety monitor segnala violazione, il
        processo viene TERMINATO e si solleva SafetyViolation (C2).
        Sensori non leggibili (None) → avviso e limite saltato, MAI
        valori fittizi.
        """
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cpu_temp_max = gpu_temp_max = power_max = 0.0
        deadline = (time.monotonic() + duration_s
                    + self.deadline_grace)
        warned = set()
        try:
            while proc.poll() is None:
                if time.monotonic() > deadline:
                    self.logger.error("Stress oltre la deadline, terminato")
                    proc.terminate()
                    break
                if on_tick is not None:
                    on_tick()
                if (self.safety_monitor is not None
                        and self.safety_monitor.is_violation()):
                    proc.terminate()
                    raise SafetyViolation(
                        self.safety_monitor.get_violation_reason() or
                        "SafetyMonitor: violazione")
                for label, value, limit, bucket in (
                        ("CPU", reader.get_cpu_temp(), LIMITS.cpu.temp_max,
                         "cpu"),
                        ("GPU", reader.get_gpu_temp(), LIMITS.gpu.temp_max,
                         "gpu"),
                        ("Potenza", reader.get_total_power(), power_budget,
                         "power")):
                    if value is None:
                        if label not in warned:
                            warned.add(label)
                            self.logger.warning(
                                "Stress: sensore %s non leggibile — limite "
                                "non verificabile", label)
                        continue
                    if bucket == "cpu":
                        cpu_temp_max = max(cpu_temp_max, value)
                    elif bucket == "gpu":
                        gpu_temp_max = max(gpu_temp_max, value)
                    else:
                        power_max = max(power_max, value)
                    if value > limit:
                        proc.terminate()
                        raise SafetyViolation(
                            f"{label} temp {value:.1f}°C > {limit}°C"
                            if bucket != "power" else
                            f"Potenza {value:.1f}W > {limit}W",
                            value, limit)
                time.sleep(1)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        return proc.returncode, cpu_temp_max, gpu_temp_max, power_max

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
