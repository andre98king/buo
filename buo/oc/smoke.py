#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Smoke test CPU in Python — SPEC = p3_smoke del motore (P3_SMOKE_STRESS=30).

Semantica: marcatore test (hang auto-detection),
stress-ng --verify 30s (MAI --timeout 0); fail termico SOLO al limite
HARD (LIMITS.cpu.temp_max, politica a due livelli 03/09) — il carico
sintetico e' piu' caldo del reale (~13-16°C): una config che in smoke
fa 85-94°C (in game ~70-80°C) DEVE passare; il throttle operativo
(SMU temp_apply/governor) gestisce il calore sotto l'HARD.
freq_min >= freq−50 (clock stretching), WHEA delta 0
(whitelist AER/GHES corrected). Campionamento 1s via reader con on_tick.

Mai hardware reale nei test: reader mockabile, comandi iniettabili.
"""

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from ..constants import LIMITS
from ..utils.shell import run_command
from .constants import (
    OC_DIR_DEFAULT,
    SMOKE_FREQ_MARGIN,
    SMOKE_MARKER,
    SMOKE_STRESS_S,
    SMOKE_TIMEOUT_S,
)

logger = logging.getLogger("buo.oc.smoke")

# Whitelist WHEA/MCE (identica a whea_delta() del motore): le righe corrected
# AER/GHES e le righe di enablement NON contano come fail.
_WHEA_RE = re.compile(
    r"whea|machine check|mce: \[hardware error\]|hardware error from apei|"
    r"corrected machine check",
    re.IGNORECASE,
)
_WHEA_IGNORE_RE = re.compile(
    r"in-kernel mce decoding enabled|thermal monitoring enabled|"
    r"mce: [a-z0-9]+: (cleared|pending)|aer.*corrected|ghes.*corrected",
    re.IGNORECASE,
)


def boot_epoch() -> Optional[int]:
    """Epoch del boot da /proc/stat btime (fallback: uptime -s)."""
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("btime"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _whea_delta(lines: List[str]) -> int:
    return len([l for l in lines if _WHEA_RE.search(l)
                and not _WHEA_IGNORE_RE.search(l)])


@dataclass(frozen=True)
class SmokeResult:
    ok: bool
    cause: Optional[str] = None  # thermal|critical|whea|stretch|stress|timeout|hang
    temp_max: Optional[float] = None
    freq_min: Optional[int] = None
    whea_delta: int = 0
    duration_s: float = 0.0
    marker_cleared: bool = True


class CpuSmoke:
    """Smoke 30s sulla config candidata (spec p3_smoke del motore).

    reader: interfaccia RealHardwareReader/MockHardware (get_cpu_temp,
    get_cpu_freq). stress_cmd iniettabile (lista); systemctl_cmd per il
    marcatore (non usato direttamente — riservato). In mock non esegue
    comandi reali.
    """

    def __init__(self, reader, mock: bool = False, mock_hardware=None,
                 stress_cmd: Optional[List[str]] = None,
                 systemctl_cmd: str = "systemctl",
                 oc_dir: Optional[Path] = None,
                 sudo: bool = True,
                 timeout_s: int = SMOKE_TIMEOUT_S,
                 dmesg_cmd: str = "dmesg",
                 dry_run: bool = False,
                 freq_warmup_s: int = 3):
        """freq_warmup_s: secondi di warmup PRIMA di tracciare freq_min —
        dopo l'apply la freq deve rampare al target (falso stretch senza)."""
        self.reader = reader
        self.mock = mock
        self.dry_run = dry_run
        self.mock_hw = mock_hardware
        # Simulato = mock O dry-run: nessun subprocess, nessun marcatore
        self._sim = mock or dry_run
        # Default reader su path REALE: reader=None (cli `buo oc apply`,
        # fallback TUI) → RealHardwareReader, altrimenti _safe_temp/
        # _safe_freq darebbero sempre None → stretch/thermal MAI valutati
        # (falso pass silenzioso). Import LAZY in __init__ (niente import
        # top-level: zero rischio di cicli); se fallisce reader resta None
        # e lo smoke degrada come oggi (fail visibile in log, mai abort).
        if self.reader is None and not self._sim:
            try:
                from ..safety.reader import RealHardwareReader
                self.reader = RealHardwareReader()
            except Exception:
                self.reader = None
        self.oc_dir = Path(oc_dir) if oc_dir else Path(OC_DIR_DEFAULT)
        self._marker = self.oc_dir / SMOKE_MARKER
        self._sudo = sudo
        self._timeout_s = timeout_s
        self._freq_warmup_s = freq_warmup_s
        self._dmesg_cmd = dmesg_cmd
        self._stress_cmd = stress_cmd or self._default_stress_cmd()
        self._systemctl = systemctl_cmd

    def _default_stress_cmd(self) -> List[str]:
        nproc = os.cpu_count() or 1
        return ["stress-ng", "--cpu", str(nproc), "--cpu-method", "all",
                "--verify", "--timeout", str(SMOKE_STRESS_S)]

    # ----------------------------- marcatore --------------------------- #

    def _write_marker(self, freq: int, vid_cap: Optional[int]) -> None:
        self.oc_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "freq": freq,
            "vid_cap": vid_cap,
            "kind": "smoke",
            "started_epoch": int(time.time()),
        }
        tmp = self._marker.with_suffix(self._marker.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._marker)

    def _clear_marker(self) -> bool:
        try:
            self._marker.unlink()
            return True
        except OSError:
            return False

    def stale_smoke_marker(self) -> bool:
        """True se il marcatore smoke è STALE (boot successivo): hang
        confermato dal sistema ripartito durante lo smoke."""
        try:
            data = json.loads(self._marker.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        started = data.get("started_epoch")
        boot = boot_epoch()
        if not started or not boot:
            return False
        return int(started) < boot

    # ------------------------------- run ------------------------------ #

    def run(self, freq: int, vid_cap: Optional[int]) -> SmokeResult:
        """Smoke 30s; (ok, cause) con marcatore scritto e pulito.

        Un marcatore GIÀ stale (precedente smoke hangato) → fail-closed:
        non si rientra, cause=hang. In modalità SIMULATA (mock/dry-run)
        nessun subprocess e nessun marcatore scritto (M2): letture solo
        dal mock_hw (mai valori inventati se assente).
        """
        if not self._sim and self.stale_smoke_marker():
            logger.warning("smoke marker stale (hang precedente) — "
                           "fail-closed, nessun nuovo smoke")
            return SmokeResult(ok=False, cause="hang", marker_cleared=False)

        if not self._sim:
            self._write_marker(freq, vid_cap)
        started = time.monotonic()
        before = [] if self._sim else self._dmesg_snapshot()
        rc = 0
        temp_max: Optional[float] = None
        freq_min: Optional[int] = None

        if self._sim:
            # mock/dry-run: nessun comando reale; letture SOLO dal
            # mock_hardware (dry-run senza mock_hw → None, C1)
            rc = 0
            temp_max = self._mock_temp()
            freq_min = self._mock_freq()
            time.sleep(0)  # deterministico nei test
        else:
            try:
                proc = subprocess.Popen(
                    self._stress_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                rc = 127
                proc = None
            if proc is not None:
                deadline = started + self._timeout_s
                sample = 0
                while proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(1)
                    sample += 1
                    t = self._safe_temp()
                    if t is not None:
                        temp_max = t if temp_max is None else max(temp_max, t)
                        if t >= LIMITS.cpu.temp_max:  # HARD: kill immediato
                            self._kill(proc)
                            rc = -1
                            temp_max = t
                            break
                    f = self._safe_freq()
                    # Warmup (freq_warmup_s) prima di tracciare freq_min:
                    # dopo l'apply la freq DEVE rampare al target (parte
                    # dallo stato precedente, es. 3500) — i primi campioni
                    # darebbero un FALSO stretch (osservato sul campo:
                    # apply 3825 falliva 'stretch' in modo intermittente,
                    # mai a regime).
                    #
                    # CUTOFF TEMPORALE a fine run (SMOKE_STRESS_S − 1):
                    # i worker stress-ng escono in STAGGER al loro timeout
                    # (~30s) e la core campionata (cpu0) resta scarica col
                    # parent ancora vivo (poll() None) → la freq crolla a
                    # idle. Campionato lì dentro = FALSO stretch con drop
                    # dall'8% al 60% in UN campione (osservato sul campo:
                    # 1398/1552/2028/2883/3194 MHz a t≈30s, artefatto ~1
                    # run su 3, anche su STOCK 3500 = stretch impossibile).
                    # Un filtro per entità del drop NON basta (drop piccoli
                    # tipo 3194 = −8.6% sfuggono): la finestra è TEMPORALE
                    # (uscita al timeout), quindi si smette di tracciare
                    # freq_min ~1s prima della durata nominale. I campioni
                    # tracciati (1..~28s) sono tutti sotto carico pieno:
                    # una stretch VERA (graduale o sostenuta, o rc≠0 con
                    # --verify) resta rilevata.
                    elapsed = time.monotonic() - started
                    if (f is not None and sample > self._freq_warmup_s
                            and elapsed < SMOKE_STRESS_S - 1):
                        freq_min = (f if freq_min is None
                                    else min(freq_min, f))
                if proc.poll() is None:
                    self._kill(proc)
                    rc = 124 if rc == 0 else rc
                else:
                    rc = proc.returncode if rc == 0 else rc

        after = [] if self._sim else self._dmesg_snapshot()
        whea = _whea_delta([l for l in after if l not in before])
        duration = time.monotonic() - started

        ok, cause = self._evaluate(freq, rc, temp_max, freq_min, whea)
        cleared = True if self._sim else self._clear_marker()
        if self._sim:
            logger.info("[MOCK/DRY-RUN] smoke %ss simulato (freq=%d, "
                        "vid=%s)", SMOKE_STRESS_S, freq, vid_cap)
        return SmokeResult(ok=ok, cause=cause, temp_max=temp_max,
                           freq_min=freq_min, whea_delta=whea,
                           duration_s=duration, marker_cleared=cleared)

    # ---------------------------- sampling ---------------------------- #

    def _safe_temp(self) -> Optional[float]:
        try:
            return self.reader.get_cpu_temp()
        except Exception:
            return None

    def _safe_freq(self) -> Optional[int]:
        try:
            return self.reader.get_cpu_freq()
        except Exception:
            return None

    def _mock_temp(self) -> Optional[float]:
        if self.mock_hw is not None:
            try:
                return float(self.mock_hw.get_cpu_temp())
            except Exception:
                return None
        return None

    def _mock_freq(self) -> Optional[int]:
        if self.mock_hw is not None:
            try:
                return int(self.mock_hw.get_cpu_freq())
            except Exception:
                return None
        return None

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

    # ----------------------------- dmesg ------------------------------ #

    def _dmesg_snapshot(self) -> List[str]:
        if self._sim:
            return []
        rc, out, _err = run_command([self._dmesg_cmd], timeout=10,
                                    sudo=self._sudo, capture=True)
        if rc != 0:
            return []
        return out.splitlines()

    # ---------------------------- evaluation -------------------------- #

    def _evaluate(self, freq: int, rc: int, temp_max: Optional[float],
                  freq_min: Optional[int], whea: int
                  ) -> "tuple[bool, Optional[str]]":
        if rc == -1:
            return False, "critical"
        # Fail termico SOLO all'HARD (politica 2 livelli 03/09): sotto
        # l'HARD il throttle operativo gestisce il calore; una config che
        # in smoke fa 85-94°C (in game ~70-80°C) DEVE passare.
        if temp_max is not None and temp_max >= LIMITS.cpu.temp_max:
            return False, "thermal"
        if freq_min is not None and freq_min < freq - SMOKE_FREQ_MARGIN:
            return False, "stretch"
        if whea > 0:
            return False, "whea"
        if rc == 124:
            return False, "timeout"
        if rc != 0:
            return False, "stress"
        return True, None
