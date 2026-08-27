#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Limiti di sicurezza — ri-export degli hard limits immutabili.

Questo modulo NON deve mai contenere valori modificabili: i limiti vivono
in constants.LIMITS (dataclass frozen). La classe SafetyLimits li espone
in un formato comodo per il SafetyMonitor e per la verifica.
"""

from dataclasses import dataclass

from ..constants import LIMITS


@dataclass(frozen=True)
class SafetyLimits:
    """Limiti usati dal SafetyMonitor (sempre <= hard limits)."""

    cpu_vid_absolute_max: int = LIMITS.cpu.vid_absolute_max
    cpu_temp_max: int = LIMITS.cpu.temp_max
    gpu_voltage_absolute_max: int = LIMITS.gpu.voltage_absolute_max
    gpu_temp_max: int = LIMITS.gpu.temp_max
    power_budget: int = LIMITS.power.power_budget
    vram_warning: float = LIMITS.vram.warning_threshold
    vram_critical: float = LIMITS.vram.critical_threshold

    def as_dict(self) -> dict:
        """Serializza i limiti."""
        return {
            "cpu_vid_absolute_max": self.cpu_vid_absolute_max,
            "cpu_temp_max": self.cpu_temp_max,
            "gpu_voltage_absolute_max": self.gpu_voltage_absolute_max,
            "gpu_temp_max": self.gpu_temp_max,
            "power_budget": self.power_budget,
            "vram_warning": self.vram_warning,
            "vram_critical": self.vram_critical,
        }


# Istanza globale dei limiti di sicurezza
SAFETY_LIMITS = SafetyLimits()
