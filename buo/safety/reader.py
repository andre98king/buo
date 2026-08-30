#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Lettore hardware REALE per il SafetyMonitor e `buo status` (fix C1).

Prima di questo modulo, in modalità reale il monitor riceveva
`hardware=None` e campionava VALORI FITTIZI costanti (45°C, 1206mV…)
che non superavano mai i limiti: il "SafetyMonitor 0.5s" era un no-op.

Questo lettore espone la stessa interfaccia get_* di MockHardware ma
legge i sensori veri: hwmon (k10temp/amdgpu/nct6686), sysfs (online,
scaling_cur_freq, num_cu), PCI config (SMN core mask), debugfs
(amdgpu_pm_info), la libreria SMU locale (bc250_smu di
bc250-collective/bc250_smu_oc, VID CPU) e systemctl (40-CU). Ogni
valore NON leggibile è `None`: il monitor salta quel limite con un
avviso esplicito (fail-visible), mai valori inventati.

REGOLE HARDWARE (research/SENSORS_BC250.md):
    • l'accesso SMN (PCI config 0xB8/0xBC, usato da core mask e dalla
      libreria SMU) CONCORRE con cyan-skillfish-governor-smu: il
      governor va SEMPRE verificato inattivo prima di leggere l'SMU,
      anche in sola lettura (regola di progetto — accessi concorrenti
      corrompono letture e scritture del governor);
    • il VID CPU esiste SOLO via SMU mailbox Q3/msg 0x36 (bc250_smu);
    • la potenza SoC totale e il VDDGFX si leggono da debugfs
      amdgpu_pm_info (root) — anche il debugfs interroga l'SMU via
      driver (mailbox UNICO): SOLO a governor inattivo, mai in
      concorrenza (INCIDENTE 30/08: freeze silenzioso del SoC);
    • le letture hwmon (amdgpu in0/power1/freq1, k10temp, nct6686)
      sono SICURE con governor attivo: metrics table cached, nessun
      mailbox.
"""

import glob
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, Optional, Tuple

from ..constants import GOVERNOR_SERVICE
from ..utils import smn
from ..utils.logging import get_logger

# Path di un import riuscito della libreria bc250_smu (cache di processo:
# evita di ripetere la ricerca dei candidate path a ogni campionamento).
_BC250_SMU_DIR: Optional[str] = None


def _bc250_smu_import():
    """Importa la libreria SMU locale (bc250-collective/bc250_smu_oc)
    senza MAI sollevare.

    Cerca in ordine: (1) la directory di un import già riuscito in questo
    processo (es. fatto da bc250-detect); (2) /usr/local/bin
    (installazione standalone, verificata sul campo); (3) la directory
    deps di buo (repo bc250_smu_oc, che copia la libreria nel checkout —
    vedi buo/install/deps.py). Un candidato che fallisce NON oscura i
    successivi: si ritorna None SOLO se ogni candidato fallisce
    (fail-soft). Patchabile nei test come punto di mock unico.
    """
    global _BC250_SMU_DIR
    try:
        # (1) modulo già importato altrove nel processo
        mod = sys.modules.get("bc250_smu")
        if mod is not None and getattr(mod, "__file__", None):
            _BC250_SMU_DIR = os.path.dirname(mod.__file__)
            return mod

        # (2) + (3) candidate path: path di un import già riuscito
        # (cache), installazione standalone, poi deps di buo.
        from ..utils.paths import deps_dir
        candidates = ["/usr/local/bin",
                      str(deps_dir() / "bc250_smu_oc" / "bc250_smu")]
        if _BC250_SMU_DIR is not None:
            candidates = [_BC250_SMU_DIR] + [c for c in candidates
                                             if c != _BC250_SMU_DIR]

        for cand in candidates:
            if not (os.path.isdir(cand)
                    and os.path.exists(os.path.join(cand, "__init__.py"))):
                continue
            added = cand not in sys.path
            if added:
                sys.path.insert(0, cand)
            try:
                import bc250_smu
            except Exception:
                # Un candidato fallito NON deve oscurare i successivi:
                # pulisci sys.modules/sys.path e prova il prossimo.
                sys.modules.pop("bc250_smu", None)
                if added:
                    sys.path.remove(cand)
                continue
            _BC250_SMU_DIR = cand
            return bc250_smu
    except Exception:
        sys.modules.pop("bc250_smu", None)
        return None
    return None


class RealHardwareReader:
    """Letture reali via hwmon/sysfs/debugfs (interfaccia compatibile con
    MockHardware). Tutti i percorsi sono iniettabili per i test (mai
    hardware reale nei test)."""

    def __init__(self, hwmon_base: str = "/sys/class/hwmon",
                 sysfs_base: str = "/sys",
                 debugfs_base: str = "/sys/kernel/debug",
                 conf_path: str = "/etc/bc250-cu-live-manager.conf",
                 online_path: Optional[str] = None,
                 cpuinfo_path: str = "/proc/cpuinfo",
                 systemctl_cmd: str = "systemctl",
                 governor_ttl: float = 10.0):
        self._hwmon = hwmon_base
        self._sysfs = sysfs_base
        self._debugfs = debugfs_base
        self._conf = conf_path
        self._online = online_path or f"{sysfs_base}/devices/system/cpu/online"
        self._cpuinfo = cpuinfo_path
        self._systemctl = systemctl_cmd
        # TTL (s) della cache dello stato del governor: evita un subprocess
        # ogni 0.5s di campionamento del SafetyMonitor. TTL=0 → nessuna
        # cache (ogni lettura riesegue systemctl).
        self._governor_ttl = governor_ttl
        self._governor_cache: Optional[Tuple[float, Optional[bool]]] = None
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
            # Fail-soft con traccia in debug (semantica invariata: None).
            self.logger.debug("Lettura %s/%s non riuscita (hwmon=%s)",
                              kind, attr, self._hwmon, exc_info=True)
        return None

    def _hwmon_dirs(self, kind: str):
        """Directory hwmon il cui `name` contiene `kind` (o amdgpu per gpu).

        Generatore: un'eccezione durante l'esplorazione ferma la ricerca
        senza propagarsi (fail-soft).
        """
        try:
            for entry in sorted(os.listdir(self._hwmon)):
                name_file = f"{self._hwmon}/{entry}/name"
                if not os.path.exists(name_file):
                    continue
                with open(name_file) as f:
                    name = f.read().strip().lower()
                match = (kind in name) or (kind == "gpu" and "amdgpu" in name)
                if match:
                    yield f"{self._hwmon}/{entry}"
        except Exception:
            return

    def _governor_active(self) -> Optional[bool]:
        """Stato di cyan-skillfish-governor-smu (con cache TTL).

        True = attivo, False = inattivo, None = sconosciuto (systemctl
        non eseguibile o rc diverso da 0/3 — fail-soft). La cache TTL
        (default 10s, iniettabile) evita un subprocess a ogni
        campionamento del monitor. REGOLA di progetto: prima di QUALSIASI
        accesso SMN (core mask, VID SMU) il governor deve essere
        CONFERMATO inattivo — accessi concorrenti sul paio PCI config
        0xB8/0xBC corrompono letture e scritture del governor.
        """
        now = time.monotonic()
        if (self._governor_cache is not None
                and now - self._governor_cache[0] < self._governor_ttl):
            return self._governor_cache[1]
        try:
            r = subprocess.run([self._systemctl, "is-active",
                                GOVERNOR_SERVICE],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                active: Optional[bool] = True
            elif r.returncode == 3:
                active = False
            else:
                active = None
        except Exception:
            self.logger.debug("Check governor (systemctl is-active) non "
                              "eseguibile (cmd=%s)", self._systemctl,
                              exc_info=True)
            active = None
        self._governor_cache = (now, active)
        return active

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
        """Voltaggio GPU (mV): hwmon amdgpu in0_input — SICURO con
        governor attivo (metrics table cached, nessun mailbox) — poi
        fallback VDDGFX da debugfs amdgpu_pm_info SOLO a governor
        inattivo (regex identica a buo/optimize/gpu.py, fix 35af68e;
        il debugfs interroga l'SMU: mai in concorrenza col governor).
        Ordine: hwmon in0 → pm_info VDDGFX (gated) → None."""
        v = self._hwmon_value("amdgpu", "in")
        if v is not None:
            return int(round(v))
        text = self._pm_info_text()
        if text:
            m = re.search(r"(\d+)\s*mV\s*\(VDDGFX\)", text)
            if m:
                return int(m.group(1))
        return None

    def _pm_info_text(self) -> Optional[str]:
        """Testo del primo amdgpu_pm_info leggibile (debugfs, root).

        Helper condiviso tra total_power (riga SoC) e gpu_voltage
        (VDDGFX). GATE SMU↔governor (INCIDENTE 30/08): il debugfs
        amdgpu_pm_info interroga l'SMU via driver — mailbox UNICO — e
        una lettura mentre il governor scrive corrompe letture E
        scritture (wedge dell'SMU → freeze silenzioso del SoC senza
        traccia kernel). La lettura è permessa SOLO a governor
        CONFERMATO inattivo (cache TTL); attivo o stato sconosciuto →
        None (fail-closed). Nessun file leggibile → None (fail-soft).
        """
        if self._governor_active() is not False:
            return None
        try:
            for path in sorted(
                    glob.glob(f"{self._debugfs}/dri/*/amdgpu_pm_info")):
                with open(path) as f:
                    return f.read()
        except Exception:
            self.logger.debug("Lettura amdgpu_pm_info non riuscita "
                              "(debugfs=%s)", self._debugfs, exc_info=True)
        return None

    def get_gpu_power(self) -> Optional[float]:
        """Potenza GPU (W) da amdgpu (power1_average è in microwatt)."""
        v = self._hwmon_value("amdgpu", "power")
        return v / 1e6 if v is not None else None

    def get_cpu_vid(self) -> Optional[int]:
        """VID CPU (mV) dalla libreria SMU locale (bc250_smu di
        bc250-collective/bc250_smu_oc).

        Verificato sul campo: `Bc250Smu(use_flock=True)` +
        `q3_0x36_get_current_cpu_voltage()` → 993 mV. SOLO letture: non
        viene MAI chiamato un metodo di scrittura (force/unforce/set).
        GATE SMU↔governor: la lettura è permessa SOLO a governor
        CONFERMATO inattivo (systemctl is-active); governor attivo o
        stato sconosciuto → None (mai toccare l'SMU in concorrenza).
        Import o lettura falliti → None (fail-soft, mai un VID inventato).
        """
        if self._governor_active() is not False:
            return None
        try:
            smu = _bc250_smu_import()
            if smu is None:
                return None
            handle = smu.Bc250Smu(use_flock=True)
            return int(handle.q3_0x36_get_current_cpu_voltage())
        except Exception:
            # Fail-soft con traccia in debug (semantica invariata: None).
            self.logger.debug("Lettura VID CPU (bc250_smu) non riuscita",
                              exc_info=True)
            return None

    def get_cpu_freq(self) -> Optional[int]:
        """Frequenza CPU (MHz) da scaling_cur_freq (kHz → MHz, intero)."""
        try:
            path = (f"{self._sysfs}/devices/system/cpu/cpu0/cpufreq/"
                    "scaling_cur_freq")
            with open(path) as f:
                return int(f.read().strip()) // 1000
        except Exception:
            self.logger.debug("Lettura frequenza CPU non riuscita "
                              "(sysfs=%s)", self._sysfs, exc_info=True)
            return None

    def get_cpu_cores(self) -> Optional[int]:
        """Core CPU FISICI (non thread SMT).

        Conta le coppie (physical id, core id) da /proc/cpuinfo — stessa
        logica di buo/audit/hardware.py (`_count_cpuinfo`): sulla BC-250
        l'online file dice "0-11" (12 thread) ma i core sono 6. Fallback
        (cpuinfo illeggibile): thread dal file `online`, core = thread/2
        (SMT2). Nessuna fonte → None.
        """
        cores = self._count_physical_cores()
        if cores is not None and cores > 0:
            return cores
        threads = self._count_online_threads()
        if threads is not None and threads > 0:
            return threads // 2  # SMT2 su questa macchina
        return None

    def _count_physical_cores(self) -> Optional[int]:
        """Coppie (physical id, core id) da /proc/cpuinfo (core fisici)."""
        pairs = set()
        processors = 0
        try:
            with open(self._cpuinfo) as f:
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
            return None
        if not pairs:
            return processors if processors else None
        return len(pairs)

    def _count_online_threads(self) -> Optional[int]:
        """Numero di thread online dal file `online` (es. '0-11' → 12)."""
        try:
            with open(self._online) as f:
                text = f.read().strip()
            count = 0
            for part in text.split(","):
                part = part.strip()
                if "-" in part:
                    lo, _, hi = part.partition("-")
                    count += int(hi) - int(lo) + 1
                elif part.isdigit():
                    count += 1
            return count
        except Exception:
            return None

    def get_core_mask(self) -> Optional[int]:
        """Maschera core via SMN (PCI config space) — helper condiviso.

        GATE SMU↔governor (come get_cpu_vid): la lettura è permessa SOLO
        a governor CONFERMATO inattivo — il paio PCI config 0xB8/0xBC è
        condiviso con il governor e accessi concorrenti lo corrompono.
        Fail-soft: governor attivo/sconosciuto o PCI assente → None, mai
        0x77/0xFF inventati (policy diversa da unlock, che assume stock
        quando l'hardware non c'è).
        """
        if self._governor_active() is not False:
            return None
        try:
            return smn.read_core_mask()
        except Exception:
            self.logger.debug("Lettura core mask (SMN) non riuscita",
                              exc_info=True)
            return None

    def get_gpu_cu(self) -> Optional[int]:
        """CU GPU attive: 1) sysfs num_cu; 2) config WGP masks
        (popcount×2); 3) bc250-cu-live-manager status. Ogni passo è in
        try/except → None (fail-soft, mai un conteggio inventato)."""
        cu = self._gpu_cu_sysfs()
        if cu is not None:
            return cu
        cu = self._gpu_cu_conf()
        if cu is not None:
            return cu
        return self._gpu_cu_wrapper()

    def _gpu_cu_sysfs(self) -> Optional[int]:
        """num_cu da /sys/class/drm/card*/device/ (assente su questa
        macchina, ma è la fonte canonica quando presente)."""
        try:
            for path in sorted(
                    glob.glob(f"{self._sysfs}/class/drm/card*/device/num_cu")):
                with open(path) as f:
                    text = f.read().strip()
                if text.isdigit():
                    return int(text)
        except Exception:
            pass
        return None

    def _gpu_cu_conf(self) -> Optional[int]:
        """CU dal file di config del live-manager: riga
        `BC250_WGP_MASKS=0x1f,0x1f,0x1f,0x1f` → per maschera
        popcount(mask)×2 (un WGP = 2 CU), somma: 0x1f=5 bit → 10 CU × 4
        maschere = 40."""
        try:
            with open(self._conf) as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith("BC250_WGP_MASKS="):
                        continue
                    total = 0
                    for mask in line.split("=", 1)[1].split(","):
                        total += bin(int(mask.strip(), 0)).count("1") * 2
                    return total
        except Exception:
            pass
        return None

    def _gpu_cu_wrapper(self) -> Optional[int]:
        """Fallback: `status` del bc250-cu-live-manager (stessa regex di
        buo/audit/hardware.py `_parse_routed_cus`)."""
        try:
            from ..unlock.wrappers.bc250_live_manager import \
                BC250LiveManagerWrapper
            wrapper = BC250LiveManagerWrapper()
            if not wrapper.available:
                return None
            result = wrapper.status()
            m = re.search(r"CUs active & routed\s*:\s*(\d+)\s*/\s*(\d+)",
                          result.get("stdout", ""))
            if m:
                return int(m.group(1))
        except Exception:
            pass
        return None

    def get_gpu_freq(self) -> Optional[int]:
        """Frequenza GPU (MHz) da hwmon amdgpu freq1_input (Hz → MHz)."""
        v = self._hwmon_value("amdgpu", "freq")
        return int(v / 1e6) if v is not None else None

    def get_fan_speed(self) -> Optional[int]:
        """Ventole (RPM): massimo dei fan*_input NON zero del sensore
        nct6686 (su questa macchina fan1=0, fan2=2090 → 2090)."""
        try:
            best = None
            for d in self._hwmon_dirs("nct6686"):
                for t in sorted(os.listdir(d)):
                    if t.startswith("fan") and t.endswith("_input"):
                        with open(f"{d}/{t}") as f:
                            v = int(f.read().strip())
                        if v > 0 and (best is None or v > best):
                            best = v
            return best
        except Exception:
            self.logger.debug("Lettura ventole (nct6686) non riuscita",
                              exc_info=True)
            return None

    def get_ambient_temp(self) -> Optional[float]:
        """Temperatura ambiente (°C): temp*_input il cui temp*_label ==
        'System' sul sensore nct6686 (milligradi → gradi). Mai la prima
        temp qualunque: serve il label esatto."""
        try:
            for d in self._hwmon_dirs("nct6686"):
                labels = {}
                for t in os.listdir(d):
                    if t.startswith("temp") and t.endswith("_label"):
                        with open(f"{d}/{t}") as f:
                            labels[t[:-len("_label")]] = f.read().strip()
                for t in sorted(os.listdir(d)):
                    if t.startswith("temp") and t.endswith("_input"):
                        if labels.get(t[:-len("_input")]) == "System":
                            with open(f"{d}/{t}") as f:
                                return int(f.read().strip()) / 1000.0
        except Exception:
            self.logger.debug("Lettura temp ambiente (nct6686) non riuscita",
                              exc_info=True)
        return None

    def get_total_power(self) -> Optional[float]:
        """Potenza totale SoC (W) da debugfs amdgpu_pm_info (root, GATED).

        Riga reale: `57.82 W (current SoC including CPU)` → 57.82 W.
        La lettura è un accesso mailbox SMU (il debugfs interroga l'SMU
        via driver): SOLO a governor CONFERMATO inattivo — attivo o
        stato sconosciuto → None (fail-closed, incidente 30/08: freeze
        silenzioso del SoC). Nessun file debugfs → None (mai un totale
        inventato).

        Candidato FUTURO senza mailbox: sysfs `gpu_metrics`
        (world-readable, metrics table cached, nessun accesso SMU) per
        la potenza — riferimento: patch kernel "drm/amd/pm: fill in the
        data member of v2 gpu metrics table for vangogh". NON introdotto
        in questo round: gli offset v2_2 non sono verificabili con le
        fonti disponibili (commento, non parsing).
        """
        text = self._pm_info_text()
        if not text:
            return None
        m = re.search(r"([\d.]+)\s*W \(current SoC including CPU\)", text)
        return float(m.group(1)) if m else None

    def get_is_40cu_enabled(self) -> Optional[bool]:
        """40-CU attive: `systemctl is-active bc250-cu-live-manager`.

        rc 0 → True; rc 3 → False; qualsiasi altro rc o eccezione
        (servizio assente, systemctl inesistente) → None (fail-soft)."""
        try:
            r = subprocess.run([self._systemctl, "is-active",
                                "bc250-cu-live-manager"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return True
            if r.returncode == 3:
                return False
            return None
        except Exception:
            self.logger.debug("systemctl is-active non eseguibile "
                              "(cmd=%s)", self._systemctl, exc_info=True)
            return None

    def get_system_info(self) -> Dict[str, Any]:
        """Riepilogo per `buo status`: valori REALI o None (mai fittizi).

        Stessa forma di MockHardware.get_system_info(); ogni sensore non
        leggibile è None → la CLI mostra "non rilevabile" (fail-soft C1:
        mai inventare valori). I campi di stato non sensore
        (is_undervolted, fix ACPI…) restano None: non sono misurabili
        da un reader passivo.
        """
        mask = self.get_core_mask()
        return {
            # Formato allineato all'audit (buo/audit/hardware.py):
            # "0x%02X" — maiuscolo, 2 cifre.
            "core_mask": f"0x{mask:02X}" if mask is not None else None,
            "cpu_cores": self.get_cpu_cores(),
            "cpu_freq": self.get_cpu_freq(),
            "cpu_vid": self.get_cpu_vid(),
            "cpu_temp": self.get_cpu_temp(),
            "gpu_cu": self.get_gpu_cu(),
            "gpu_freq": self.get_gpu_freq(),
            "gpu_voltage": self.get_gpu_voltage(),
            "gpu_temp": self.get_gpu_temp(),
            "gpu_power": self.get_gpu_power(),
            "total_power": self.get_total_power(),
            "ambient_temp": self.get_ambient_temp(),
            "fan_speed": self.get_fan_speed(),
            "is_undervolted": None,
            "is_overclocked": None,
            "is_40cu_enabled": self.get_is_40cu_enabled(),
            "is_acpi_fixed": None,
            "is_tlb_fixed": None,
            "is_ace_fixed": None,
            "iommu_off": None,
            "reboot_count": None,
        }
