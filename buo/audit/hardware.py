#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Hardware Discovery — raccoglie lo stato reale della BC-250.

Legge (quando disponibili): maschera core CPU, numero CU GPU, versione
kernel, versione Mesa, stato IOMMU, tabelle ACPI, temperature, governor,
modulo amdgpu patchato. In modalità mock usa MockHardware.
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from ..constants import (CORE_MASK_REG, CORE_MASK_STOCK, CORE_MASK_UNLOCKED,
                         HEALTH_RESULTS_FILE, PCI_CONFIG_PATH)
from ..utils.logging import LoggerMixin


class HardwareAudit(LoggerMixin):
    """Raccoglie il quadro completo dell'hardware."""

    def __init__(self, mock: bool = False, mock_hardware=None):
        self.mock = mock
        self.mock_hw = mock_hardware

    # ------------------------------------------------------------------ #

    def run(self) -> Dict[str, Any]:
        """Esegue l'audit completo."""
        audit = {
            "cpu": self._audit_cpu(),
            "gpu": self._audit_gpu(),
            "system": self._audit_system(),
            "kernel": self._audit_kernel(),
            "mesa": self._audit_mesa(),
            "iommu": self._audit_iommu(),
            "acpi": self._audit_acpi(),
            "governor": self._audit_governor(),
            "amdgpu": self._audit_amdgpu(),
            "health": self._audit_health(),
            "temps": self._audit_temps(),
        }
        return audit

    # ---------------------------- CPU ------------------------------ #

    def _audit_cpu(self) -> Dict[str, Any]:
        if self.mock and self.mock_hw is not None:
            mask = self.mock_hw.read_core_mask()
            return {
                "core_mask": hex(mask),
                "cores": 8 if mask == CORE_MASK_UNLOCKED else 6,
                "unlocked": mask == CORE_MASK_UNLOCKED,
            }

        # Lettura reale via PCI config space (SMN 0x5A870)
        mask = None
        try:
            if os.path.exists(PCI_CONFIG_PATH) and os.geteuid() == 0:
                import struct
                fd = os.open(PCI_CONFIG_PATH, os.O_RDWR)
                try:
                    os.pwrite(fd, struct.pack("<I", CORE_MASK_REG), 0xB8)
                    raw = os.pread(fd, 4, 0xBC)
                    mask = struct.unpack("<I", raw)[0] & 0xFF
                finally:
                    os.close(fd)
        except Exception as e:
            self.logger.debug("Lettura maschera core fallita: %s", e)

        # Fallback: nproc / cpuinfo
        cores = self._count_cpuinfo()
        return {
            "core_mask": f"0x{mask:02X}" if mask is not None else None,
            "cores": cores,
            "unlocked": mask == CORE_MASK_UNLOCKED if mask is not None else (cores >= 8),
        }

    @staticmethod
    def _count_cpuinfo() -> int:
        try:
            with open("/proc/cpuinfo") as f:
                return sum(1 for line in f if line.startswith("processor"))
        except Exception:
            return 0

    # ---------------------------- GPU ------------------------------ #

    def _audit_gpu(self) -> Dict[str, Any]:
        if self.mock and self.mock_hw is not None:
            return {
                "cu_count": self.mock_hw.get_cu_count(),
                "stable_cu": len(self.mock_hw.state.gpu_stable_cu),
                "defective_cu": list(self.mock_hw.state.gpu_defective_cu),
                "wgp_mask": self.mock_hw.get_wgp_mask(),
            }

        cu_count = self._read_sysfs("num_cu")
        return {
            "cu_count": cu_count,
            "stable_cu": None,
            "defective_cu": None,
            "wgp_mask": self._read_sysfs("wgp_mask"),
        }

    def _read_sysfs(self, name: str) -> Any:
        """Legge una voce sysfs del dispositivo amdgpu."""
        drm = "/sys/class/drm"
        try:
            for entry in os.listdir(drm):
                if entry.startswith("card") and \
                   os.path.exists(f"{drm}/{entry}/device"):
                    path = f"{drm}/{entry}/device/{name}"
                    if os.path.exists(path):
                        with open(path) as f:
                            return f.read().strip()
        except Exception:
            pass
        return None

    # -------------------------- SYSTEM ----------------------------- #

    def _audit_system(self) -> Dict[str, Any]:
        return {
            "hostname": os.uname().nodename,
            "arch": os.uname().machine,
            "uptime": self._read_file("/proc/uptime", "").split()[0]
            if os.path.exists("/proc/uptime") else "",
        }

    # -------------------------- KERNEL ---------------------------- #

    def _audit_kernel(self) -> Dict[str, Any]:
        release = os.uname().release
        m = re.match(r"(\d+)\.(\d+)", release)
        version = tuple(int(x) for x in m.groups()) if m else None
        from ..constants import KERNEL_MIN
        return {
            "release": release,
            "version": version,
            "meets_minimum": version is not None and version >= KERNEL_MIN,
        }

    # --------------------------- MESA ------------------------------ #

    def _audit_mesa(self) -> Dict[str, Any]:
        version = self._detect_mesa_version()
        meets = False
        if version:
            from ..constants import MESA_MIN
            meets = version >= MESA_MIN
        return {
            "version": f"{version[0]}.{version[1]}" if version else None,
            "meets_minimum": meets,
            "raw": self._detect_mesa_raw(),
        }

    def _detect_mesa_version(self):
        raw = self._detect_mesa_raw()
        if not raw:
            return None
        m = re.search(r"(\d+)\.(\d+)", raw)
        if m:
            return tuple(int(x) for x in m.groups())
        return None

    @staticmethod
    def _parse_mesa_string(out: str) -> Optional[str]:
        """
        Estrae la versione MESA da glxinfo.

        ATTENZIONE (bug trovato sul campo): la stringa reale è
        "OpenGL version string: 4.6 (Compatibility Profile) Mesa 25.2.4"
        — il primo numero (4.6) è la versione OpenGL, NON Mesa.
        Va cercato il token "Mesa X.Y(.Z)".
        """
        m = re.search(r"Mesa\s+(\d+\.\d+(?:\.\d+)?)", out)
        if m:
            return m.group(1)
        m = re.search(r"OpenGL version string:\s*(\S+)", out)
        if m:
            return m.group(1)
        return None

    def _detect_mesa_raw(self) -> Optional[str]:
        try:
            import subprocess
            r = subprocess.run(["glxinfo", "-B"], capture_output=True,
                               text=True, timeout=10)
            return self._parse_mesa_string(r.stdout or "")
        except Exception:
            pass
        return None

    # -------------------------- IOMMU ------------------------------ #

    def _audit_iommu(self) -> Dict[str, Any]:
        cmdline = self._read_file("/proc/cmdline", "")
        return {
            "enabled": "iommu=off" not in cmdline and "iommu=pt" not in cmdline,
            "cmdline_has_iommu_off": "iommu=off" in cmdline,
            "cmdline": cmdline,
        }

    # --------------------------- ACPI ------------------------------ #

    def _audit_acpi(self) -> Dict[str, Any]:
        tables_dir = Path("/sys/firmware/acpi/tables")
        ssdt = []
        if tables_dir.exists():
            try:
                ssdt = sorted(p.name for p in tables_dir.glob("SSDT*"))
            except Exception:
                pass
        return {
            "ssdt_tables": ssdt,
            "cst_present": any("CST" in s for s in ssdt),
            "pst_present": any("PST" in s for s in ssdt),
        }

    # ------------------------- GOVERNOR ---------------------------- #

    def _audit_governor(self) -> Dict[str, Any]:
        from ..constants import GOVERNOR_SERVICE
        active = False
        try:
            import subprocess
            r = subprocess.run(["systemctl", "is-active", GOVERNOR_SERVICE],
                               capture_output=True, text=True, timeout=10)
            active = r.stdout.strip() == "active"
        except Exception:
            pass
        return {"service": GOVERNOR_SERVICE, "active": active}

    # -------------------------- AMDGPU ----------------------------- #

    def _audit_amdgpu(self) -> Dict[str, Any]:
        patched = False
        try:
            import subprocess
            r = subprocess.run(["modinfo", "amdgpu"], capture_output=True,
                               text=True, timeout=10)
            patched = "bc250_cc_write_mode" in r.stdout
        except Exception:
            pass
        return {"patched_for_40cu": patched}

    # -------------------------- HEALTH ----------------------------- #

    def _audit_health(self) -> Dict[str, Any]:
        if not os.path.exists(HEALTH_RESULTS_FILE):
            return {"available": False, "results": None}
        stable, defective = 0, 0
        try:
            with open(HEALTH_RESULTS_FILE) as f:
                for line in f:
                    parts = line.strip().split("\t")
                    if len(parts) >= 5 and not line.startswith("#"):
                        if parts[4] == "PASS":
                            stable += 1
                        else:
                            defective += 1
        except Exception:
            pass
        return {"available": True, "stable_wgp": stable, "defective_wgp": defective}

    # -------------------------- TEMPS ------------------------------ #

    def _audit_temps(self) -> Dict[str, Any]:
        if self.mock and self.mock_hw is not None:
            return {
                "cpu_temp": round(self.mock_hw.get_cpu_temp(), 1),
                "gpu_temp": round(self.mock_hw.get_gpu_temp(), 1),
                "ambient": round(self.mock_hw.get_ambient_temp(), 1),
            }
        return {
            "cpu_temp": self._read_hwmon("cpu", "temp"),
            "gpu_temp": self._read_hwmon("gpu", "temp"),
            "ambient": None,
        }

    def _read_hwmon(self, kind: str, attr: str):
        try:
            base = "/sys/class/hwmon"
            for entry in os.listdir(base):
                name_file = f"{base}/{entry}/name"
                if not os.path.exists(name_file):
                    continue
                with open(name_file) as f:
                    name = f.read().strip().lower()
                if kind in name or (kind == "gpu" and "amdgpu" in name):
                    for t in os.listdir(f"{base}/{entry}"):
                        if t.startswith("temp") and t.endswith("_input"):
                            with open(f"{base}/{entry}/{t}") as f:
                                return int(f.read().strip()) / 1000.0
        except Exception:
            pass
        return None

    # --------------------------------------------------------------- #

    @staticmethod
    def _read_file(path: str, default: str = "") -> str:
        try:
            with open(path) as f:
                return f.read().strip()
        except Exception:
            return default
