#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Mock hardware per sviluppo e testing senza una BC-250 reale.

Simula: maschera core, CU, temperature, VID/voltage, potenza, ventole,
stato di undervolt/overclock/40-CU e reboot. Tutti i comandi BUO
supportano `--mock` per usare questa classe.
"""

import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..constants import CORE_MASK_STOCK, CORE_MASK_UNLOCKED


@dataclass
class MockHardwareState:
    """Stato del mock hardware."""
    core_mask: int = CORE_MASK_STOCK
    cpu_temp: float = 45.0
    cpu_freq: int = 3500
    cpu_vid: int = 1206
    cpu_cores: int = 6
    cpu_stable_cores: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5])

    gpu_cu_count: int = 24
    gpu_temp: float = 40.0
    gpu_freq: int = 1500
    gpu_voltage: int = 1050
    gpu_power: float = 85.0
    gpu_utilization: float = 50.0
    gpu_stable_cu: List[int] = field(default_factory=lambda: list(range(24)))
    gpu_defective_cu: List[int] = field(default_factory=list)
    # Ricerca per-silicio GPU (design GPU_UV §7): mappa freq → tensione
    # minima stabile; default {} = sempre stabile.
    gpu_stable_voltages: Dict[int, int] = field(default_factory=dict)
    wgp_mask: str = "0x000FFFFF"

    vram_temp_real: Optional[float] = None

    total_power: float = 85.0
    ambient_temp: float = 22.0
    fan_speed: int = 1200

    is_overclocked: bool = False
    is_undervolted: bool = False
    is_40cu_enabled: bool = False
    is_acpi_fixed: bool = False
    is_tlb_fixed: bool = False
    is_ace_fixed: bool = False
    iommu_off: bool = False


class MockHardware:
    """Simula l'hardware BC-250 per sviluppo e test."""

    def __init__(self, state: Optional[MockHardwareState] = None,
                 seed: Optional[int] = None):
        self.state = state or MockHardwareState()
        self.reboot_count = 0
        if seed is not None:
            random.seed(seed)
        self._simulation_start = time.time()

    # ============================ CPU ============================ #

    def read_core_mask(self) -> int:
        return self.state.core_mask

    def write_core_mask(self, mask: int) -> bool:
        if mask == CORE_MASK_UNLOCKED:
            self.state.core_mask = mask
            self.state.cpu_cores = 8
            self.state.cpu_stable_cores = [0, 1, 2, 3, 4, 5, 6, 7]
            return True
        return False

    def get_cpu_temp(self) -> float:
        self.state.cpu_temp += random.uniform(-0.5, 0.5)
        return self.state.cpu_temp

    def get_cpu_freq(self) -> int:
        return self.state.cpu_freq

    def get_cpu_vid(self) -> int:
        return self.state.cpu_vid

    def set_cpu_freq(self, freq: int) -> bool:
        if 3500 <= freq <= 4000:
            self.state.cpu_freq = freq
            self.state.is_overclocked = True
            return True
        return False

    def set_cpu_vid(self, vid: int) -> bool:
        if 800 <= vid <= 1300:
            self.state.cpu_vid = vid
            self.state.is_undervolted = True
            return True
        return False

    def test_cpu_core(self, core_idx: int) -> bool:
        return core_idx in self.state.cpu_stable_cores

    # ============================ GPU ============================ #

    def get_gpu_temp(self) -> float:
        self.state.gpu_temp += random.uniform(-0.3, 0.3)
        return self.state.gpu_temp

    def get_gpu_freq(self) -> int:
        return self.state.gpu_freq

    def get_gpu_voltage(self) -> int:
        return self.state.gpu_voltage

    def get_gpu_power(self) -> float:
        self.state.gpu_power += random.uniform(-2.0, 2.0)
        return max(10.0, self.state.gpu_power)

    def get_gpu_utilization(self) -> float:
        self.state.gpu_utilization += random.uniform(-5.0, 5.0)
        return max(0.0, min(100.0, self.state.gpu_utilization))

    def get_cu_count(self) -> int:
        return self.state.gpu_cu_count

    def set_gpu_freq(self, freq: int) -> bool:
        if 500 <= freq <= 2230:
            self.state.gpu_freq = freq
            return True
        return False

    def set_gpu_voltage(self, volt: int) -> bool:
        if 700 <= volt <= 1100:
            self.state.gpu_voltage = volt
            self.state.is_undervolted = True
            return True
        return False

    def enable_40cu(self) -> bool:
        if self.state.is_40cu_enabled:
            return True
        self.state.gpu_cu_count = 40
        self.state.gpu_stable_cu = list(range(40))
        defective = random.sample(range(40), random.randint(0, 4))
        self.state.gpu_defective_cu = defective
        self.state.gpu_stable_cu = [cu for cu in range(40) if cu not in defective]
        self.state.is_40cu_enabled = True
        return True

    def disable_40cu(self) -> bool:
        if not self.state.is_40cu_enabled:
            return True
        self.state.gpu_cu_count = 24
        self.state.gpu_stable_cu = list(range(24))
        self.state.gpu_defective_cu = []
        self.state.is_40cu_enabled = False
        return True

    def get_wgp_mask(self) -> str:
        return self.state.wgp_mask

    def test_cu(self, cu_idx: int) -> bool:
        return cu_idx in self.state.gpu_stable_cu

    def probe_gpu_stable(self, freq: int, mv: int) -> bool:
        """Probe della ricerca per-silicio GPU: stabile se `mv` >= tensione
        minima stabile per `freq` (freq assente dalla mappa = sempre
        stabile). Solo per test — mai usata in percorsi reali."""
        min_stable = self.state.gpu_stable_voltages.get(freq)
        if min_stable is None:
            return True
        return mv >= min_stable

    # =========================== VRAM ============================ #

    def get_vram_temp(self) -> Optional[float]:
        return self.state.vram_temp_real

    # ========================= POTENZA =========================== #

    def get_total_power(self) -> float:
        cpu_power = (self.state.cpu_vid * self.state.cpu_freq / 1_000_000) * 0.8
        self.state.total_power = self.state.gpu_power + cpu_power + 15.0
        return self.state.total_power

    # ========================= VENTOLE =========================== #

    def get_fan_speed(self) -> int:
        return self.state.fan_speed

    def set_fan_speed(self, rpm: int) -> bool:
        if 0 <= rpm <= 3000:
            self.state.fan_speed = rpm
            return True
        return False

    # ========================= SISTEMA =========================== #

    def get_ambient_temp(self) -> float:
        self.state.ambient_temp += random.uniform(-0.1, 0.1)
        return self.state.ambient_temp

    def simulate_reboot(self) -> bool:
        self.reboot_count += 1
        return True

    def apply_acpi_fix(self) -> bool:
        self.state.is_acpi_fixed = True
        return True

    def remove_acpi_fix(self) -> bool:
        self.state.is_acpi_fixed = False
        return True

    def apply_tlb_fix(self) -> bool:
        self.state.is_tlb_fixed = True
        return True

    def remove_tlb_fix(self) -> bool:
        self.state.is_tlb_fixed = False
        return True

    def apply_ace_fix(self) -> bool:
        self.state.is_ace_fixed = True
        return True

    def remove_ace_fix(self) -> bool:
        self.state.is_ace_fixed = False
        return True

    def disable_iommu(self) -> bool:
        self.state.iommu_off = True
        return True

    def enable_iommu(self) -> bool:
        self.state.iommu_off = False
        return True

    # ========================== INFO ============================= #

    def get_system_info(self) -> Dict[str, Any]:
        """Riepilogo completo dello stato (usato da `buo status`)."""
        return {
            "core_mask": hex(self.state.core_mask),
            "cpu_cores": self.state.cpu_cores,
            "cpu_freq": self.state.cpu_freq,
            "cpu_vid": self.state.cpu_vid,
            "cpu_temp": round(self.state.cpu_temp, 1),
            "gpu_cu": self.state.gpu_cu_count,
            "gpu_freq": self.state.gpu_freq,
            "gpu_voltage": self.state.gpu_voltage,
            "gpu_temp": round(self.state.gpu_temp, 1),
            "gpu_power": round(self.state.gpu_power, 1),
            "total_power": round(self.get_total_power(), 1),
            "ambient_temp": round(self.state.ambient_temp, 1),
            "fan_speed": self.state.fan_speed,
            "is_undervolted": self.state.is_undervolted,
            "is_overclocked": self.state.is_overclocked,
            "is_40cu_enabled": self.state.is_40cu_enabled,
            "is_acpi_fixed": self.state.is_acpi_fixed,
            "is_tlb_fixed": self.state.is_tlb_fixed,
            "is_ace_fixed": self.state.is_ace_fixed,
            "iommu_off": self.state.iommu_off,
            "reboot_count": self.reboot_count,
        }
