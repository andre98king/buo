#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Validazione post-unlock (design research/DESIGN_POSTUNLOCK_VALIDATION.md).

Due validazioni SHORT delle unità "extra" appena sbloccate (mai full
system, mai FurMark: carico mirato e moderato):

- ``CpuUnlockValidation``: 4 processi stress-ng ``--verify`` (uno per
  thread extra, 12-15, calcolati da /sys/devices/system/cpu/online) con
  sampling 1s della temperatura e dmesg WHEA before/after.
- ``GpuUnlockValidation``: vkmark ``-b desktop:duration=N --size
  1920x1080`` (env display + radv forzato, sintassi validata sul campo)
  con sampling 1s della temp GPU e dmesg WHEA/fault amdgpu.

Esito TRI-STATE per entrambe: ``pass`` / ``fail`` (condanna: whea,
stress, timeout, gpu_fault) / ``inconclusive`` (termico HARD, tool GPU
assente) — **mai** SafetyViolation (D10). In mock/dry-run nessun
subprocess, nessun sleep reale e letture SOLO dal mock_hardware (C1),
pattern ``_sim`` di buo/oc/smoke.py.

``UnlockVerdict``: verdetto durevole del silicio (unlock-verdict.json,
schema 1, scrittura atomica tmp+fsync+replace) in ``state_dir()``.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..constants import LIMITS
from ..oc.smoke import _whea_delta
from ..utils.logging import LoggerMixin
from ..utils.paths import state_dir
from ..utils.shell import run_command

logger = logging.getLogger("buo.unlock.validation")

VERDICT_SCHEMA = 1
VERDICT_FILE = "unlock-verdict.json"

# Righe di fault amdgpu (stessa classe di quelle dello sweep GPU:
# amdgpu reset/fault/timeout, ring gfx, VM_L2_PROTECTION).
GPU_FAULT_RE = re.compile(r"amdgpu.*(GPU reset|fault|timeout)|ring gfx|"
                          r"VM_L2_PROTECTION")

# ICD radv forzato (il default selezionerebbe llvmpipe → GPU a
# 22-100MHz = carico finto, verificato sul campo 30/08).
VK_ICD_RADV = "/usr/share/vulkan/icd.d/radeon_icd.x86_64.json"


# --------------------------------------------------------------------- #
# Helper
# --------------------------------------------------------------------- #

def parse_cpu_list(text: str) -> List[int]:
    """'0-3,7' → [0, 1, 2, 3, 7] (formato dei file /sys/devices/...)."""
    out: List[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return out


def extra_threads(online_file: str = "/sys/devices/system/cpu/online"
                  ) -> List[int]:
    """Thread extra (indice >= 12) tra gli online — mai hard-codati.

    [] se il file non è leggibile o non ci sono thread >= 12 (12T).
    """
    try:
        text = Path(online_file).read_text(encoding="utf-8").strip()
    except OSError:
        return []
    return [t for t in parse_cpu_list(text) if t >= 12]


def cpu_online_count(online_file: str = "/sys/devices/system/cpu/online"
                     ) -> Optional[int]:
    """Numero di thread online (None se illeggibile)."""
    try:
        text = Path(online_file).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return len(parse_cpu_list(text))


def evidence(**extra: Any) -> Dict[str, Any]:
    """Evidenza del verdetto (schema D6) con timestamp ISO."""
    data: Dict[str, Any] = {"at": datetime.now().isoformat()}
    data.update(extra)
    return data


def _cpu_stress_cmd(thread: int, seconds: int) -> List[str]:
    """stress-ng --verify su UN thread extra (D3): copertura certa di
    tutti i thread extra (un --cpu N può impacchettare i worker)."""
    return ["taskset", "-c", str(thread), "stress-ng",
            "--cpu", "1", "--cpu-method", "all", "--verify",
            "--timeout", str(seconds)]


def gpu_vkmark_cmd(seconds: int) -> List[str]:
    """Sintassi validata sul campo (probe sweep): rc=0 dopo N secondi."""
    return ["vkmark", "-b", f"desktop:duration={seconds}",
            "--size", "1920x1080"]


def gpu_vkmark_env() -> Dict[str, str]:
    """Env per vkmark: display KDE reale + radv forzato (senza radv il
    loader seleziona llvmpipe → GPU a 22-100MHz = carico finto). Solo i
    campi con un valore reale presente sulla macchina."""
    env: Dict[str, str] = {}
    if os.path.exists("/run/user/1000/wayland-0"):
        env["XDG_RUNTIME_DIR"] = "/run/user/1000"
        env["DISPLAY"] = ":0"
        try:
            xa = next(Path("/run/user/1000").glob("xauth_*"))
            env["XAUTHORITY"] = str(xa)
        except StopIteration:
            pass
    if os.path.exists(VK_ICD_RADV):
        env["VK_ICD_FILENAMES"] = VK_ICD_RADV
    return env


# --------------------------------------------------------------------- #
# Verdetto durevole del silicio (D6)
# --------------------------------------------------------------------- #

class UnlockVerdict:
    """unlock-verdict.json (schema 1) in state_dir().

    Machine-scoped: il "fingerprint" è la macchina stessa (lo state dir
    persiste tra deployment ostree e cold boot). Verdict ammessi:
    cpu.never_unlock, gpu.never_enable_all (condanna), gpu.stable_short
    (evidenza positiva). File corrotto/assente → nessun veto (la
    macchina ri-sblocca e ri-valida: auto-guarigione documentata).
    """

    def __init__(self, path: Optional[Path] = None, sim: bool = False):
        self.path = Path(path) if path else state_dir() / VERDICT_FILE
        # sim (mock/dry-run): set() aggiorna SOLO la memoria (i gate
        # in-process restano coerenti); nessun file scritto (M2).
        self.sim = sim
        self._data: Dict[str, Any] = {"schema": VERDICT_SCHEMA}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and data.get("schema") == VERDICT_SCHEMA:
            self._data = data

    def save(self) -> None:
        """Scrittura atomica (tmp + fsync + os.replace, pattern smoke)."""
        if self.sim:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def get(self, unit: str) -> Optional[str]:
        """Verdict corrente di cpu/gpu (None = nessun veto)."""
        return (self._data.get(unit) or {}).get("verdict")

    def set(self, unit: str, verdict: str,
            extra_evidence: Optional[Dict[str, Any]] = None) -> None:
        """Scrive il verdetto di cpu/gpu con evidenza (schema D6)."""
        self._data[unit] = {
            "verdict": verdict,
            "evidence": extra_evidence or {},
        }
        self.save()


# --------------------------------------------------------------------- #
# Validatore CPU (thread extra)
# --------------------------------------------------------------------- #

class CpuUnlockValidation(LoggerMixin):
    """4× stress-ng --verify sui thread extra (D3) + temp 1s + WHEA.

    Tri-state: pass / fail (whea|stress|timeout) / inconclusive
    (termico HARD: problema di raffreddamento, non condanna — D4).
    """

    def __init__(self, mock: bool = False, dry_run: bool = False,
                 mock_hardware=None, reader=None,
                 stress_cmd=None, dmesg_cmd: str = "dmesg",
                 grace_s: int = 30):
        self._sim = mock or dry_run
        self.mock_hw = mock_hardware
        # reader: RealHardwareReader di default nel path reale (import
        # lazy, come lo smoke) — la temp è lettura hwmon, SICURA con il
        # governor attivo (nessun mailbox SMU).
        if reader is None and not self._sim:
            try:
                from ..safety.reader import RealHardwareReader
                reader = RealHardwareReader()
            except Exception:
                reader = None
        self.reader = reader
        self._stress_cmd = stress_cmd or _cpu_stress_cmd
        self._dmesg_cmd = dmesg_cmd
        self._grace_s = grace_s

    # ------------------------------- run ----------------------------- #

    def run(self, duration_s: int) -> Dict[str, Any]:
        if self._sim:
            return self._run_sim()
        return self._run_real(duration_s)

    def _run_sim(self) -> Dict[str, Any]:
        """mock/dry-run: esito letto SOLO dal mock_hardware (C1);
        nessun subprocess, nessun sleep reale (deterministico)."""
        out: Dict[str, Any] = {
            "outcome": "pass", "cause": None, "temp_max": None,
            "whea_delta": 0, "threads": [], "simulated": True,
        }
        if self.mock_hw is None:
            return out
        st = self.mock_hw.state
        n = int(getattr(st, "cpu_cores", 0) or 0)
        if n >= 8:
            out["threads"] = [12, 13, 14, 15]
        if bool(getattr(st, "unlock_validate_thermal", False)):
            out.update(outcome="inconclusive", cause="thermal",
                       temp_max=float(LIMITS.cpu.temp_max))
        elif int(getattr(st, "whea_delta", 0) or 0) > 0:
            out.update(outcome="fail", cause="whea",
                       whea_delta=int(getattr(st, "whea_delta", 0)))
        elif not bool(getattr(st, "cpu_unlock_ok", True)):
            out.update(outcome="fail", cause="stress")
        return out

    def _run_real(self, duration_s: int) -> Dict[str, Any]:
        threads = extra_threads()
        if not threads:
            return {"outcome": "inconclusive", "cause": "no_extra_threads",
                    "threads": [], "temp_max": None}
        before = self._dmesg()
        procs = []
        try:
            for t in threads:
                procs.append(subprocess.Popen(
                    self._stress_cmd(t, duration_s),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL))
        except FileNotFoundError:
            # m3: stress-ng/taskset assenti = problema AMBIENTALE, non
            # evidenza di unità difettose → inconcluso (revert senza
            # condanna), simmetrico al tool GPU assente
            for p in procs:
                self._kill(p)
            return {"outcome": "inconclusive", "cause": "tool_missing",
                    "threads": threads, "temp_max": None}
        except OSError:
            for p in procs:
                self._kill(p)
            return {"outcome": "fail", "cause": "stress",
                    "threads": threads, "temp_max": None}

        started = time.monotonic()
        deadline = started + duration_s + self._grace_s
        temp_max: Optional[float] = None
        timed_out = False
        while any(p.poll() is None for p in procs):
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(1)
            t = self._safe_temp()
            if t is not None:
                temp_max = t if temp_max is None else max(temp_max, t)
                if t >= LIMITS.cpu.temp_max:  # HARD: kill immediato
                    for p in procs:
                        self._kill(p)
                    return {"outcome": "inconclusive", "cause": "thermal",
                            "threads": threads, "temp_max": t}
        for p in procs:
            if p.poll() is None:
                self._kill(p)
        if timed_out:
            return {"outcome": "fail", "cause": "timeout",
                    "threads": threads, "temp_max": temp_max}

        after = self._dmesg()
        whea = _whea_delta([ln for ln in after if ln not in before])
        rc_bad = [p.returncode for p in procs
                  if p.returncode is not None and p.returncode != 0]
        cause = None
        if whea > 0:
            cause = "whea"
        elif rc_bad:
            cause = "stress"
        return {
            "outcome": "fail" if cause else "pass",
            "cause": cause, "temp_max": temp_max, "whea_delta": whea,
            "threads": threads,
        }

    # ---------------------------- supporto --------------------------- #

    def _safe_temp(self) -> Optional[float]:
        try:
            return self.reader.get_cpu_temp()
        except Exception:
            return None

    def _dmesg(self) -> List[str]:
        try:
            rc, out, _ = run_command([self._dmesg_cmd], timeout=10,
                                     sudo=True, capture=True)
        except Exception:
            return []
        if rc != 0:
            return []
        return out.splitlines()

    @staticmethod
    def _kill(proc) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# --------------------------------------------------------------------- #
# Validatore GPU (40-CU)
# --------------------------------------------------------------------- #

class GpuUnlockValidation(LoggerMixin):
    """vkmark (D3) sulle 40 CU + temp GPU 1s + dmesg WHEA/fault amdgpu.

    Tri-state: pass / fail (gpu_fault|whea|stress|timeout) / inconclusive
    (termico HARD, tool assente). FurMark ESCLUSO (carico massimo
    sintetico → falso termico pre-cap, design D3).
    """

    def __init__(self, mock: bool = False, dry_run: bool = False,
                 mock_hardware=None, reader=None,
                 dmesg_cmd: str = "dmesg", grace_s: int = 30):
        self._sim = mock or dry_run
        self.mock_hw = mock_hardware
        if reader is None and not self._sim:
            try:
                from ..safety.reader import RealHardwareReader
                reader = RealHardwareReader()
            except Exception:
                reader = None
        self.reader = reader
        self._dmesg_cmd = dmesg_cmd
        self._grace_s = grace_s

    def tool_available(self) -> bool:
        """vkmark + radv presenti? Senza radv il loader userebbe
        llvmpipe (carico finto): fail-closed. In mock/dry-run:
        simulato presente (mai `which` reale); i test forzano False
        per lo scenario tool assente (inconcluso + ricetta, D4)."""
        if self._sim:
            return True
        return (shutil.which("vkmark") is not None
                and os.path.exists(VK_ICD_RADV))

    def run(self, duration_s: int) -> Dict[str, Any]:
        if not self.tool_available():
            return {"outcome": "inconclusive", "cause": "tool_missing",
                    "tool": "vkmark", "temp_max": None}
        if self._sim:
            return self._run_sim()
        return self._run_real(duration_s)

    def _run_sim(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "outcome": "pass", "cause": None, "temp_max": None,
            "whea_delta": 0, "gpu_faults": 0, "tool": "vkmark",
            "simulated": True,
        }
        if self.mock_hw is None:
            return out
        st = self.mock_hw.state
        faults = int(getattr(st, "gpu_fault_lines", 0) or 0)
        whea = int(getattr(st, "whea_delta", 0) or 0)
        if bool(getattr(st, "unlock_validate_thermal", False)):
            out.update(outcome="inconclusive", cause="thermal",
                       temp_max=float(LIMITS.gpu.temp_max))
        elif not bool(getattr(st, "gpu_unlock_ok", True)) or faults > 0:
            out.update(outcome="fail", cause="gpu_fault",
                       gpu_faults=faults)
        elif whea > 0:
            out.update(outcome="fail", cause="whea", whea_delta=whea)
        return out

    def _run_real(self, duration_s: int) -> Dict[str, Any]:
        cmd = gpu_vkmark_cmd(duration_s)
        env = dict(os.environ)
        env.update(gpu_vkmark_env())
        before = self._dmesg()
        started = time.monotonic()
        try:
            proc = subprocess.Popen(cmd, env=env,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError:
            return {"outcome": "fail", "cause": "stress", "tool": "vkmark",
                    "temp_max": None}
        deadline = started + duration_s + self._grace_s
        temp_max: Optional[float] = None
        timed_out = False
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(1)
            t = self._safe_temp()
            if t is not None:
                temp_max = t if temp_max is None else max(temp_max, t)
                if t >= LIMITS.gpu.temp_max:  # HARD: kill immediato
                    self._kill(proc)
                    return {"outcome": "inconclusive", "cause": "thermal",
                            "tool": "vkmark", "temp_max": t}
        if proc.poll() is None:
            self._kill(proc)
        if timed_out:
            return {"outcome": "fail", "cause": "timeout", "tool": "vkmark",
                    "temp_max": temp_max}

        after = self._dmesg()
        new_lines = [ln for ln in after if ln not in before]
        whea = _whea_delta(new_lines)
        faults = len([ln for ln in new_lines if GPU_FAULT_RE.search(ln)])
        rc = proc.returncode
        # rc != 0 SENZA fault/WHEA in dmesg = tool/display fallito (es.
        # nessun display): non è evidenza di CU difettose → inconcluso
        # (torna a stock senza condanna, D4); con fault → fail reale.
        cause = None
        if faults > 0:
            cause = "gpu_fault"
        elif whea > 0:
            cause = "whea"
        elif rc != 0:
            return {"outcome": "inconclusive", "cause": "tool",
                    "tool": "vkmark", "temp_max": temp_max,
                    "whea_delta": whea, "gpu_faults": faults}
        return {
            "outcome": "fail" if cause else "pass",
            "cause": cause, "temp_max": temp_max, "whea_delta": whea,
            "gpu_faults": faults, "tool": "vkmark",
        }

    # ---------------------------- supporto --------------------------- #

    def _safe_temp(self) -> Optional[float]:
        try:
            return self.reader.get_gpu_temp()
        except Exception:
            return None

    def _dmesg(self) -> List[str]:
        try:
            rc, out, _ = run_command([self._dmesg_cmd], timeout=10,
                                     sudo=True, capture=True)
        except Exception:
            return []
        if rc != 0:
            return []
        return out.splitlines()

    @staticmethod
    def _kill(proc) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


__all__ = [
    "CpuUnlockValidation", "GpuUnlockValidation", "UnlockVerdict",
    "VERDICT_FILE", "VERDICT_SCHEMA", "GPU_FAULT_RE",
    "cpu_online_count", "evidence", "extra_threads",
    "gpu_vkmark_cmd", "gpu_vkmark_env", "parse_cpu_list",
]
