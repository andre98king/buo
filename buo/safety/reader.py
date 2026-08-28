#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Lettore hardware REALE per il SafetyMonitor (fix C1).

Prima di questo modulo, in modalità reale il monitor riceveva
`hardware=None` e campionava VALORI FITTIZI costanti (45°C, 1206mV…)
che non superavano mai i limiti: il "SafetyMonitor 0.5s" era un no-op.

Questo lettore espone la stessa interfaccia get_* di MockHardware ma
legge i sensori veri (hwmon: k10temp/amdgpu). Ogni valore NON leggibile
è `None`: il monitor salta quel limite con un avviso esplicito
(fail-visible), mai valori inventati.
"""

import os
from typing import Optional

from ..utils.logging import get_logger


class RealHardwareReader:
    """Letture reali via hwmon (interfaccia compatibile con MockHardware)."""

    def __init__(self, hwmon_base: str = "/sys/class/hwmon"):
        self._hwmon = hwmon_base
        self.logger = get_logger("safety.reader")

    # ------------------------------------------------------------------ #

    def _hwmon_value(self, kind: str, attr: str) -> Optional[float]:
        """Primo valore `attr*_input` del sensore `kind` (in unità grezze)."""
        try:
            for entry in sorted(os.listdir(self._hwmon)):
                name_file = f"{self._hwmon}/{entry}/name"
                if not os.path.exists(name_file):
                    continue
                with open(name_file) as f:
                    name = f.read().strip().lower()
                match = (kind in name) or (kind == "gpu" and "amdgpu" in name)
                if not match:
                    continue
                for t in sorted(os.listdir(f"{self._hwmon}/{entry}")):
                    if (t.startswith(attr)
                            and (t.endswith("_input")
                                 or t.endswith("_average"))):
                        with open(f"{self._hwmon}/{entry}/{t}") as f:
                            return float(f.read().strip())
        except Exception:
            pass
        return None

    # ------------------- API usate dal SafetyMonitor ------------------ #

    def get_cpu_temp(self) -> Optional[float]:
        """Temperatura CPU (°C) da k10temp (milligradi → gradi)."""
        v = self._hwmon_value("k10temp", "temp")
        return v / 1000.0 if v is not None else None

    def get_gpu_temp(self) -> Optional[float]:
        """Temperatura GPU (°C) da amdgpu (milligradi → gradi)."""
        v = self._hwmon_value("amdgpu", "temp")
        return v / 1000.0 if v is not None else None

    def get_gpu_voltage(self) -> Optional[int]:
        """Voltaggio GPU (mV) da amdgpu (in0_input è già in mV)."""
        v = self._hwmon_value("amdgpu", "in")
        return int(round(v)) if v is not None else None

    def get_gpu_power(self) -> Optional[float]:
        """Potenza GPU (W) da amdgpu (power1_average è in microwatt)."""
        v = self._hwmon_value("amdgpu", "power")
        return v / 1e6 if v is not None else None

    def get_cpu_vid(self) -> Optional[int]:
        """VID CPU (mV) via SMN — non implementato: None = limite VID non
        verificabile (il monitor avvisa esplicitamente)."""
        return None

    def get_total_power(self) -> Optional[float]:
        """Potenza totale (W). La CPU non espone power via hwmon: senza
        misura CPU NON si dichiara un totale (sottostimare sarebbe
        pericoloso per il budget check) → None = non verificabile."""
        return None
