#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Safety Monitor — thread di monitoraggio hardware in tempo reale.

Campiona ogni 0.5 secondi temperature, voltaggi e potenza. Se un HARD
LIMIT viene superato, chiama la callback di abort (che ferma tutto e
avvia il rollback). La stima VRAM genera solo warning, non abort.

Design (dalla chat): "La sicurezza è il requisito numero uno. Nessuno
userà mai un'app che rischia di brickare la sua BC-250."
"""

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ..exceptions import SafetyViolation
from ..utils.logging import get_logger
from .limits import SAFETY_LIMITS, SafetyLimits

try:
    from ..models.vram_estimator import VRAMTemperatureEstimator
    _HAS_VRAM = True
except Exception:  # pragma: no cover — dipendenza opzionale
    _HAS_VRAM = False


@dataclass
class SafetyReadings:
    """Letture campionate dal monitor (None = sensore non leggibile)."""
    cpu_temp: Optional[float]
    cpu_vid: Optional[int]
    gpu_temp: Optional[float]
    gpu_voltage: Optional[int]
    gpu_power: Optional[float]
    total_power: Optional[float]
    vram_temp_estimated: Optional[float]
    timestamp: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "cpu_temp": round(self.cpu_temp, 1) if self.cpu_temp is not None else None,
            "cpu_vid": self.cpu_vid,
            "gpu_temp": round(self.gpu_temp, 1) if self.gpu_temp is not None else None,
            "gpu_voltage": self.gpu_voltage,
            "gpu_power": round(self.gpu_power, 1) if self.gpu_power is not None else None,
            "total_power": (round(self.total_power, 1)
                            if self.total_power is not None else None),
            "vram_temp_estimated": (
                round(self.vram_temp_estimated, 1)
                if self.vram_temp_estimated is not None else None
            ),
            "timestamp": self.timestamp,
        }


class SafetyMonitor(threading.Thread):
    """Monitor di sicurezza in thread separato (daemon)."""

    SAMPLE_INTERVAL = 0.5  # secondi

    def __init__(
        self,
        limits: Optional[Dict[str, Any]] = None,
        abort_callback: Optional[Callable[[str], None]] = None,
        hardware: Any = None,
        vram_estimation: bool = True,
        vram_alpha: Optional[float] = None,
        vram_beta: Optional[float] = None,
        vram_tau: Optional[float] = None,
        vram_warning_threshold: Optional[float] = None,
        vram_critical_threshold: Optional[float] = None,
    ):
        super().__init__(daemon=True)
        self.logger = get_logger("safety")

        # I limiti non possono MAI superare gli hard limits immutabili
        base = SAFETY_LIMITS.as_dict()
        if limits:
            for key in ("cpu_temp_max", "gpu_temp_max", "power_budget"):
                if key in limits:
                    base[key] = min(base[key], limits[key])
        self.limits = SafetyLimits(
            cpu_temp_max=base["cpu_temp_max"],
            gpu_temp_max=base["gpu_temp_max"],
            power_budget=base["power_budget"],
        )

        self.abort_callback = abort_callback
        self.hardware = hardware

        self._running = True
        self._violation = False
        self._violation_reason = ""
        self._last_readings: Optional[SafetyReadings] = None
        self._missing_warned = set()  # avvisi "sensore assente" una sola volta

        # Stima VRAM (opzionale, mai bloccante) — usa i coefficienti di default
        self._vram_enabled = vram_estimation and _HAS_VRAM
        self._vram_estimator = None
        if self._vram_enabled:
            self._vram_estimator = VRAMTemperatureEstimator(
                alpha=vram_alpha,
                beta=vram_beta,
                tau=vram_tau,
                warning_threshold=vram_warning_threshold,
                critical_threshold=vram_critical_threshold,
            )

    # ------------------------------------------------------------------ #

    def run(self) -> None:
        self.logger.info("🛡️ Safety Monitor avviato (sampling %.1fs)",
                         self.SAMPLE_INTERVAL)
        while self._running:
            try:
                readings = self._sample()
                self._last_readings = readings
                self._check_limits(readings)
            except Exception as e:  # il monitor non deve mai morire
                self.logger.error("Errore nel safety monitor: %s", e)
            time.sleep(self.SAMPLE_INTERVAL)
        self.logger.info("Safety Monitor fermato")

    def stop(self) -> None:
        """Ferma il monitor."""
        self._running = False

    # ------------------------------------------------------------------ #

    def _sample(self) -> SafetyReadings:
        if self.hardware is not None:
            # Reader reale (RealHardwareReader) o MockHardware: ogni
            # get_* può restituire None = sensore non leggibile.
            cpu_temp = getattr(self.hardware, "get_cpu_temp", lambda: None)()
            cpu_vid = getattr(self.hardware, "get_cpu_vid", lambda: None)()
            gpu_temp = getattr(self.hardware, "get_gpu_temp", lambda: None)()
            gpu_voltage = getattr(self.hardware, "get_gpu_voltage", lambda: None)()
            gpu_power = getattr(self.hardware, "get_gpu_power", lambda: None)()
            total_power = getattr(self.hardware, "get_total_power", lambda: None)()
        else:
            # Nessun hardware: valori SIMULATI espliciti (mai usati in
            # produzione: l'orchestratore inietta RealHardwareReader).
            self.logger.warning(
                "SafetyMonitor senza hardware: campioni SIMULATI — "
                "nessun limite reale verificabile!")
            cpu_temp, cpu_vid = 45.0, 1206
            gpu_temp, gpu_voltage = 40.0, 1050
            gpu_power, total_power = 85.0, 85.0

        vram_est = None
        if self._vram_enabled and self._vram_estimator is not None:
            try:
                est = self._vram_estimator.estimate(
                    gpu_temp=float(gpu_temp),
                    gpu_power=float(gpu_power),
                )
                vram_est = est.temperature
            except Exception:
                vram_est = None

        return SafetyReadings(
            cpu_temp=float(cpu_temp) if cpu_temp is not None else None,
            cpu_vid=int(cpu_vid) if cpu_vid is not None else None,
            gpu_temp=float(gpu_temp) if gpu_temp is not None else None,
            gpu_voltage=int(gpu_voltage) if gpu_voltage is not None else None,
            gpu_power=float(gpu_power) if gpu_power is not None else None,
            total_power=float(total_power) if total_power is not None else None,
            vram_temp_estimated=vram_est,
            timestamp=time.time(),
        )

    def _check_limits(self, r: SafetyReadings) -> None:
        # Ogni valore None = sensore non leggibile: il limite NON è
        # verificabile → avviso esplicito (una volta) e si salta. Mai
        # valori fittizi al posto dei limiti (fail-visible, fix C1).
        def _check(label: str, value, limit, hard: bool = False):
            if value is None:
                if label not in self._missing_warned:
                    self._missing_warned.add(label)
                    self.logger.warning(
                        "🛡️ Sensore '%s' non leggibile: limite %s NON "
                        "verificabile", label,
                        "HARD" if hard else "configurato")
                return
            if value > limit:
                self._trigger(f"{label} {value}{'mV' if hard else '°C/W'} > "
                              f"{limit} ({'HARD LIMIT!' if hard else 'limite'})")

        # CPU VID — HARD LIMIT (brick)
        _check("CPU VID", r.cpu_vid, SAFETY_LIMITS.cpu_vid_absolute_max,
               hard=True)
        # CPU Temp
        _check("CPU Temp", r.cpu_temp, self.limits.cpu_temp_max)
        # GPU Voltage — HARD LIMIT (degrado)
        _check("GPU Voltage", r.gpu_voltage,
               SAFETY_LIMITS.gpu_voltage_absolute_max, hard=True)
        # GPU Temp
        _check("GPU Temp", r.gpu_temp, self.limits.gpu_temp_max)
        # Potenza totale (budget)
        _check("Potenza", r.total_power, self.limits.power_budget)

        # VRAM stimata — solo warning (mai abort)
        if r.vram_temp_estimated is not None:
            if r.vram_temp_estimated > SAFETY_LIMITS.vram_critical:
                self.logger.warning(
                    "VRAM stimata %.1f°C > soglia critica %.1f°C — "
                    "verificare raffreddamento backplate",
                    r.vram_temp_estimated, SAFETY_LIMITS.vram_critical)
            elif r.vram_temp_estimated > SAFETY_LIMITS.vram_warning:
                self.logger.warning(
                    "VRAM stimata %.1f°C > soglia di avviso %.1f°C",
                    r.vram_temp_estimated, SAFETY_LIMITS.vram_warning)

    def _trigger(self, reason: str) -> None:
        self._violation = True
        self._violation_reason = reason
        self.logger.error("🚨 SAFETY VIOLATION: %s", reason)
        if self.abort_callback:
            self.abort_callback(reason)
        else:
            raise SafetyViolation(reason)

    # ------------------------------------------------------------------ #

    def get_last_readings(self) -> Optional[SafetyReadings]:
        return self._last_readings

    def is_violation(self) -> bool:
        return self._violation

    def get_violation_reason(self) -> str:
        return self._violation_reason
