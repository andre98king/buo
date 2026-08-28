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
        if self.mock:
            return self._audit_mock()
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

    def _audit_mock(self) -> Dict[str, Any]:
        """Audit completamente simulato (dry-run/mock).

        NON spawna subprocess (glxinfo/systemctl/modinfo) né legge
        /proc, /sys o il file di health: restituisce valori fake ma con
        la stessa forma dell'audit reale, così i consumatori a valle
        (problems.detect e report) funzionano senza modifiche.
        """
        from ..constants import GOVERNOR_SERVICE
        from ..utils.mock import MockHardware

        hw = self.mock_hw if self.mock_hw is not None else MockHardware()

        mask = hw.read_core_mask()
        stable_cu = list(hw.state.gpu_stable_cu)
        defective_cu = list(hw.state.gpu_defective_cu)
        iommu_off = bool(hw.state.iommu_off)
        acpi_fixed = bool(hw.state.is_acpi_fixed)

        return {
            "cpu": {
                "core_mask": hex(mask),
                "cores": 8 if mask == CORE_MASK_UNLOCKED else 6,
                "unlocked": mask == CORE_MASK_UNLOCKED,
            },
            "gpu": {
                "cu_count": hw.get_cu_count(),
                "stable_cu": len(stable_cu),
                "defective_cu": defective_cu,
                "wgp_mask": hw.get_wgp_mask(),
            },
            "system": {
                "hostname": "bc250-mock",
                "arch": "x86_64",
                "uptime": "0.0",
            },
            "kernel": {
                "release": "6.18.0-mock",
                "version": (6, 18),
                "meets_minimum": True,
            },
            "mesa": {
                "version": "25.2",
                "meets_minimum": True,
                "raw": "25.2.4",
            },
            "iommu": {
                "enabled": not iommu_off,
                "cmdline_has_iommu_off": iommu_off,
                "cmdline": ("BOOT_IMAGE=/vmlinuz-mock iommu=off"
                            if iommu_off else "BOOT_IMAGE=/vmlinuz-mock"),
            },
            "acpi": {
                "ssdt_tables": ["SSDT-CST", "SSDT-PST"] if acpi_fixed else [],
                "cst_present": acpi_fixed,
                "pst_present": acpi_fixed,
            },
            "governor": {
                "service": GOVERNOR_SERVICE,
                "active": True,
            },
            "amdgpu": {
                "patched_for_40cu": bool(hw.state.is_40cu_enabled),
            },
            "health": {
                "available": True,
                "stable_wgp": len(stable_cu),
                "defective_wgp": len(defective_cu),
            },
            "temps": {
                "cpu_temp": round(hw.get_cpu_temp(), 1),
                "gpu_temp": round(hw.get_gpu_temp(), 1),
                "ambient": round(hw.get_ambient_temp(), 1),
            },
        }

    # ---------------------------- CPU ------------------------------ #

    def _audit_cpu(self) -> Dict[str, Any]:
        if self.mock and self.mock_hw is not None:
            mask = self.mock_hw.read_core_mask()
            return {
                "core_mask": hex(mask),
                "cores": 8 if mask == CORE_MASK_UNLOCKED else 6,
                "unlocked": mask == CORE_MASK_UNLOCKED,
            }

        # Lettura reale via PCI config space (SMN 0x5A870). Se la lettura
        # fallisce o restituisce un valore fuori da {0x77, 0xFF} lo stato
        # resta NON verificato (fail-open: mai fabbricare una maschera).
        mask = self._read_core_mask_smn()
        # Fallback: cpuinfo (solo conteggio dei core FISICI; mai inferire
        # "unlocked" dal cpuinfo: senza la lettura SMN autoritativa lo stato
        # resta NON verificato).
        cores = self._count_cpuinfo()
        return {
            "core_mask": f"0x{mask:02X}" if mask is not None else None,
            "cores": cores,
            "unlocked": mask == CORE_MASK_UNLOCKED if mask is not None else None,
        }

    def _read_core_mask_smn(self) -> Optional[int]:
        """Legge la core presence mask via SMN (PCI config space).

        Valori validi sulla BC-250: 0x77 (stock, 6 core) e 0xFF (8 core).
        Qualsiasi altro valore è considerato garbage e trattato come
        "non verificato" (None) invece di fabbricare una maschera.
        """
        try:
            if not (os.path.exists(PCI_CONFIG_PATH) and os.geteuid() == 0):
                return None
            import struct
            fd = os.open(PCI_CONFIG_PATH, os.O_RDWR)
            try:
                os.pwrite(fd, struct.pack("<I", CORE_MASK_REG), 0xB8)
                raw = os.pread(fd, 4, 0xBC)
                value = struct.unpack("<I", raw)[0] & 0xFF
            finally:
                os.close(fd)
        except Exception as e:
            self.logger.warning("Lettura maschera core (SMN) fallita: %s", e)
            return None
        if value not in (CORE_MASK_STOCK, CORE_MASK_UNLOCKED):
            self.logger.warning("Maschera core SMN non valida: 0x%02X", value)
            return None
        return value

    @staticmethod
    def _count_cpuinfo() -> int:
        """Conta i core FISICI da /proc/cpuinfo (non i thread SMT).

        Una CPU 8c/16t deve dare 8 e non 16: raggruppa per coppia
        (physical id, core id). Fallback: conteggio grezzo dei processor.
        """
        pairs = set()
        processors = 0
        try:
            with open("/proc/cpuinfo") as f:
                pid: Optional[str] = None
                core: Optional[str] = None
                for line in f:
                    line = line.strip()
                    if line.startswith("processor"):
                        if pid is not None and core is not None:
                            pairs.add((pid, core))
                        pid = None
                        core = None
                        processors += 1
                    elif line.startswith("physical id") and ":" in line:
                        pid = line.split(":", 1)[1].strip()
                    elif line.startswith("core id") and ":" in line:
                        core = line.split(":", 1)[1].strip()
                if pid is not None and core is not None:
                    pairs.add((pid, core))
        except Exception:
            return 0
        return len(pairs) if pairs else processors

    # ---------------------------- GPU ------------------------------ #

    def _audit_gpu(self) -> Dict[str, Any]:
        if self.mock and self.mock_hw is not None:
            return {
                "cu_count": self.mock_hw.get_cu_count(),
                "stable_cu": len(self.mock_hw.state.gpu_stable_cu),
                "defective_cu": list(self.mock_hw.state.gpu_defective_cu),
                "wgp_mask": self.mock_hw.get_wgp_mask(),
            }

        raw_cu = self._read_sysfs("num_cu")
        if raw_cu is not None and str(raw_cu).strip().isdigit():
            cu_count = int(str(raw_cu).strip())
        else:
            # num_cu assente sul path runtime UMR (ostree): il conteggio
            # CU è noto via bc250-cu-live-manager.
            cu_count = self._read_cu_count_umr()
        return {
            "cu_count": cu_count,
            "stable_cu": None,
            "defective_cu": None,
            "wgp_mask": self._read_sysfs("wgp_mask"),
        }

    def _read_cu_count_umr(self) -> Optional[int]:
        """Fallback CU count via bc250-cu-live-manager (runtime UMR).

        Prova prima il file di config (/etc/bc250-cu-live-manager.conf),
        poi l'output di `status` ("CUs active & routed : X/Y"). Mai
        fabbrica un valore: ritorna None se la riga non viene trovata.
        """
        # 1) file di config (se contiene una riga "CUs active & routed")
        try:
            conf = Path("/etc/bc250-cu-live-manager.conf")
            if conf.exists():
                n = self._parse_routed_cus(conf.read_text())
                if n is not None:
                    return n
        except Exception:
            pass
        # 2) output di `status` (fonte autoritativa a runtime)
        try:
            from ..unlock.wrappers.bc250_live_manager import BC250LiveManagerWrapper
            wrapper = BC250LiveManagerWrapper()
            if wrapper.available:
                result = wrapper.status()
                n = self._parse_routed_cus(result.get("stdout", ""))
                if n is not None:
                    return n
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_routed_cus(text: str) -> Optional[int]:
        """Estrae le CU instradate dalla riga "CUs active & routed : X/Y"."""
        m = re.search(r"CUs active & routed\s*:\s*(\d+)\s*/\s*(\d+)", text)
        if m:
            return int(m.group(1))
        return None

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
        # 1) glxinfo -B (richiede un display; fallisce in SSH headless)
        try:
            import subprocess
            r = subprocess.run(["glxinfo", "-B"], capture_output=True,
                               text=True, timeout=10)
            parsed = self._parse_mesa_string(r.stdout or "")
            if parsed:
                return parsed
        except Exception:
            pass
        # 2) fallback senza display: versione reale dal package manager
        return self._detect_mesa_pkg()

    def _detect_mesa_pkg(self) -> Optional[str]:
        """Versione Mesa dal package manager (fallback headless).

        Legge la versione reale del pacchetto Mesa installato (mai un
        valore inventato). Bazzite/Fedora: rpm. Ritorna None se il
        pacchetto non esiste o il comando fallisce.
        """
        try:
            import subprocess
            r = subprocess.run(["rpm", "-q", "--qf", "%{VERSION}", "mesa-libGL"],
                               capture_output=True, text=True, timeout=10)
            out = (r.stdout or "").strip()
            m = re.match(r"(\d+\.\d+(?:\.\d+)?)", out)
            if m:
                return m.group(1)
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
            "cpu_present": any("CPU" in s for s in ssdt),
            # ostree: le tabelle caricate non compaiono per nome in /sys
            # (fuse negli slot SSDT1-N): segnale affidabile = boot entry
            "boot_fix_present": self._boot_acpi_blob_present(),
        }

    @staticmethod
    def _boot_acpi_blob_present(boot_dir: str = "/boot") -> bool:
        """True se la boot entry di default punta a un blob concatenato.

        Metodo ostree (initramfs concatenato): il segnale affidabile NON
        sono i nomi delle tabelle in /sys (il kernel fonde gli override
        negli slot SSDT1-N), ma la boot entry che carica un blob
        /boot/initramfs-acpi-*.img con magic cpio newc in testa.
        """
        loader = Path(boot_dir) / "loader" / "entries"
        if not loader.is_dir():
            return False
        try:
            for entry in sorted(loader.glob("*.conf")):
                text = entry.read_text(errors="replace")
                m = re.search(r"^initrd\s+(\S+)", text, re.M)
                if not m:
                    continue
                name = Path(m.group(1)).name
                if not name.startswith("initramfs-acpi-"):
                    continue
                blob = Path(boot_dir) / name
                if blob.is_file():
                    with open(blob, "rb") as f:
                        if f.read(6) == b"070701":
                            return True
        except Exception:
            return False
        return False

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
        except Exception as e:
            self.logger.warning("Sensore hwmon %s/%s non leggibile: %s",
                                kind, attr, e)
        return None

    # --------------------------------------------------------------- #

    @staticmethod
    def _read_file(path: str, default: str = "") -> str:
        try:
            with open(path) as f:
                return f.read().strip()
        except Exception:
            return default
