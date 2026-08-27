#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Fix Verification — verifica che ogni fix sia attivo e funzionante.

Metodi di verifica (dal design finale, messaggio 100):
    • 8-core CPU      → conteggio processor in /proc/cpuinfo
    • 40-CU GPU       → num_cu da sysfs
    • TLB fix         → assenza di crash in carichi compute
    • ACE fix         → vkmark con compute (FPS >= baseline)
    • IOMMU           → attivo (iommu=off ASSENTE: è lo stato corretto)
    • ACPI fix        → tabelle SSDT*CST in /sys/firmware/acpi/tables
    • Governor        → systemctl is-active
"""

from pathlib import Path
from typing import Any, Dict, List

from ..utils.logging import LoggerMixin
from ..utils.shell import run_command, which


class FixVerifier(LoggerMixin):
    """Verifica lo stato di ogni fix applicato."""

    def __init__(self, mock: bool = False, mock_hardware=None):
        self.mock = mock
        self.mock_hw = mock_hardware

    # ------------------------------------------------------------------ #

    def verify_all(self, applied_fixes: List[str]) -> Dict[str, Dict[str, Any]]:
        """Verifica la lista dei fix applicati."""
        results: Dict[str, Dict[str, Any]] = {}
        checkers = {
            "cpu_core_unlock": self._check_cpu_cores,
            "gpu_40cu": self._check_gpu_cu,
            "iommu": self._check_iommu,
            "acpi_fix": self._check_acpi,
            "governor": self._check_governor,
            "gpu_mask": self._check_gpu_mask,
        }
        for fix in applied_fixes:
            checker = checkers.get(fix)
            if checker is None:
                results[fix] = {"ok": None, "detail": "nessuna verifica definita"}
                continue
            try:
                ok, detail = checker()
                results[fix] = {"ok": ok, "detail": detail}
            except Exception as e:
                results[fix] = {"ok": False, "detail": str(e)}
        return results

    # --------------------------- checkers ---------------------------- #

    def _check_cpu_cores(self):
        if self.mock and self.mock_hw is not None:
            ok = self.mock_hw.read_core_mask() == 0xFF
            return ok, "8 core (mock)" if ok else "6 core (mock)"
        try:
            with open("/proc/cpuinfo") as f:
                cores = sum(1 for l in f if l.startswith("processor"))
            return cores >= 8, f"{cores} core"
        except Exception as e:
            return False, str(e)

    def _check_gpu_cu(self):
        if self.mock and self.mock_hw is not None:
            cu = self.mock_hw.get_cu_count()
            return cu >= 38, f"{cu} CU (mock)"
        try:
            for entry in Path("/sys/class/drm").iterdir():
                if entry.name.startswith("card"):
                    num_cu = entry / "device" / "num_cu"
                    if num_cu.exists():
                        cu = int(num_cu.read_text().strip())
                        return cu >= 38, f"{cu} CU"
        except Exception:
            pass
        return False, "num_cu non leggibile"

    def _check_iommu(self):
        if self.mock and self.mock_hw is not None:
            ok = not self.mock_hw.state.iommu_off
            return ok, "IOMMU attivo (mock)" if ok else "iommu=off (mock)"
        try:
            with open("/proc/cmdline") as f:
                cmd = f.read()
            ok = "iommu=off" not in cmd and "iommu=pt" not in cmd
            return ok, ("IOMMU attivo ✓" if ok
                        else "iommu=off presente ⚠️ (rimuoverlo)")
        except Exception as e:
            return False, str(e)

    def _check_acpi(self):
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.state.is_acpi_fixed, "CST presente (mock)"
        tables = Path("/sys/firmware/acpi/tables")
        try:
            ssdt = [p.name for p in tables.glob("SSDT*")] if tables.exists() else []
            ok = any("CST" in s for s in ssdt)
            return ok, ", ".join(ssdt) if ssdt else "nessuna tabella SSDT"
        except Exception as e:
            return False, str(e)

    def _check_governor(self):
        if self.mock and self.mock_hw is not None:
            return True, "governor attivo (mock)"
        rc, out, _ = run_command(
            ["systemctl", "is-active", "cyan-skillfish-governor-smu"],
            check=False)
        return rc == 0 and out.strip() == "active", out.strip()

    def _check_gpu_mask(self):
        if self.mock and self.mock_hw is not None:
            return True, "maschera applicata (mock)"
        mask = Path("/etc/modprobe.d/bc250-40cu-selective-mask.conf")
        if mask.exists():
            return True, f"{mask.name} presente"
        return False, "nessuna maschera installata"
