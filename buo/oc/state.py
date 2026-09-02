#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Letture PURE sullo stato del motore OC (state.json schema 3 + chiavi
additive FASE 1). Mai scritture, mai subprocess SMU: tutti i path sono
iniettabili per i test. File assente/corrotto → stato "fresco" con WARN,
mai eccezione (C1: mai valori inventati — campo non leggibile → None).
"""

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .constants import (
    ENGINE_SCRIPT,
    LOG_FILE,
    OC_DIR_DEFAULT,
    PHASE_LABELS,
    RUN_PID,
    STATE_FILE,
)

logger = logging.getLogger("buo.oc.state")

# ---------------------------------------------------------------------------
# Dataclass (frozen, campo assente → None)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestingPoint:
    freq: Optional[int] = None
    vid_cap: Optional[int] = None
    kind: Optional[str] = None
    started_epoch: Optional[int] = None


@dataclass(frozen=True)
class L2Status:
    status: Optional[str] = None
    target_freq: Optional[int] = None
    target_vid: Optional[int] = None
    runs: Optional[int] = None


@dataclass(frozen=True)
class WinnerVmin:
    cap: Optional[int] = None
    vid_measured: Optional[int] = None
    scale: Optional[int] = None


@dataclass(frozen=True)
class Bkg:
    freq: Optional[int] = None
    vid_cap: Optional[int] = None
    scale: Optional[int] = None


@dataclass(frozen=True)
class Point:
    freq: Optional[int] = None
    vid: Optional[int] = None


@dataclass(frozen=True)
class Floor:
    cap: Optional[int] = None
    vid_measured: Optional[int] = None
    scale: Optional[int] = None


@dataclass(frozen=True)
class Applied:
    freq: Optional[int] = None
    vid_cap: Optional[int] = None
    scale: Optional[int] = None
    source: Optional[str] = None
    at: Optional[str] = None


@dataclass(frozen=True)
class PointRecord:
    status: str = ""
    scale: Optional[int] = None
    vid: Optional[int] = None
    temp: Optional[float] = None
    whea: Optional[int] = None
    attempts: int = 0
    cause: Optional[str] = None


@dataclass(frozen=True)
class OcState:
    """Snapshot dello state.json del motore (schema 3 + additive).

    Chiave sconosciuta → ignorata; file assente/corrotto → fase "fresca".
    """

    phase: Optional[str] = None
    phase_label: str = "fresco (nessun checkpoint)"
    testing: Optional[TestingPoint] = None
    l2: Optional[L2Status] = None
    winner_clock: Optional[int] = None
    winner_vmin: Optional[WinnerVmin] = None
    best_known_good: Optional[Bkg] = None
    next_point: Optional[Point] = None
    vmin_3500: Optional[Floor] = None
    ceiling: Optional[int] = None
    coarse_winner: Optional[int] = None
    persisted: bool = False
    governor_stopped: bool = False
    applied: Optional[Applied] = None
    points: Dict[str, PointRecord] = field(default_factory=dict)
    updated_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers di parsing (numeri robusti: "null"/""/non-numerico → None)
# ---------------------------------------------------------------------------


def _to_int(v: Any) -> Optional[int]:
    if v is None or v == "" or v == "null":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "" or v == "null":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class OcStateReader:
    """Letture PURE sullo stato del motore (mai scritture)."""

    def __init__(self, oc_dir: Optional[Path] = None,
                 state_path: Optional[Path] = None,
                 pgrep_cmd: str = "pgrep"):
        self.oc_dir = Path(oc_dir) if oc_dir else Path(OC_DIR_DEFAULT)
        self._state_path = Path(state_path) if state_path else (
            self.oc_dir / STATE_FILE)
        self._pgrep = pgrep_cmd

    # ----------------------------- state.json --------------------------- #

    def read_state(self, state_path: Optional[Path] = None) -> OcState:
        path = Path(state_path) if state_path else self._state_path
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError, json.JSONDecodeError):
            logger.warning("state.json assente/corrotto (%s) — stato fresco",
                           path)
            return OcState()

        if not isinstance(raw, dict):
            logger.warning("state.json non è un oggetto (%s) — stato fresco",
                           path)
            return OcState()

        phase = raw.get("phase")
        testing_raw = raw.get("testing") or {}
        l2_raw = raw.get("l2") or {}
        wv = raw.get("winner_vmin") or {}
        bkg = raw.get("best_known_good") or {}
        np = raw.get("next_point") or {}
        floor = raw.get("vmin_3500") or {}
        app = raw.get("applied") or {}

        points: Dict[str, PointRecord] = {}
        for key, rec in (raw.get("points") or {}).items():
            if not isinstance(rec, dict):
                continue
            points[str(key)] = PointRecord(
                status=str(rec.get("status") or ""),
                scale=_to_int(rec.get("scale")),
                vid=_to_int(rec.get("vid_measured")),
                temp=_to_float(rec.get("temp_max")),
                whea=_to_int(rec.get("whea_delta")),
                attempts=_to_int(rec.get("attempts")) or 0,
                cause=rec.get("cause"),
            )

        return OcState(
            phase=phase,
            phase_label=PHASE_LABELS.get(str(phase) if phase else "",
                                         str(phase) if phase else
                                         "fresco (nessun checkpoint)"),
            testing=TestingPoint(
                freq=_to_int(testing_raw.get("freq")),
                vid_cap=_to_int(testing_raw.get("vid_cap")),
                kind=testing_raw.get("kind"),
                started_epoch=_to_int(testing_raw.get("started_epoch")),
            ),
            l2=L2Status(
                status=l2_raw.get("status"),
                target_freq=_to_int(l2_raw.get("target_freq")),
                target_vid=_to_int(l2_raw.get("target_vid")),
                runs=_to_int(l2_raw.get("runs")),
            ),
            winner_clock=_to_int(raw.get("winner_clock")),
            winner_vmin=WinnerVmin(
                cap=_to_int(wv.get("cap")),
                vid_measured=_to_int(wv.get("vid_measured")),
                scale=_to_int(wv.get("scale")),
            ),
            best_known_good=Bkg(
                freq=_to_int(bkg.get("freq")),
                vid_cap=_to_int(bkg.get("vid_cap")),
                scale=_to_int(bkg.get("scale")),
            ),
            next_point=Point(
                freq=_to_int(np.get("freq")),
                vid=_to_int(np.get("vid")),
            ),
            vmin_3500=Floor(
                cap=_to_int(floor.get("cap")),
                vid_measured=_to_int(floor.get("vid_measured")),
                scale=_to_int(floor.get("scale")),
            ),
            ceiling=_to_int(raw.get("ceiling")),
            coarse_winner=_to_int(raw.get("coarse_winner")),
            persisted=bool(raw.get("persisted", False)),
            governor_stopped=bool(raw.get("governor_stopped", False)),
            applied=Applied(
                freq=_to_int(app.get("freq")),
                vid_cap=_to_int(app.get("vid_cap")),
                scale=_to_int(app.get("scale")),
                source=app.get("source"),
                at=app.get("at"),
            ),
            points=points,
            updated_at=raw.get("updated_at"),
        )

    # ------------------------------- log ------------------------------- #

    def log_tail(self, n: int = 14,
                 path: Optional[Path] = None) -> List[str]:
        """Ultime n righe di oc.log (file assente → [])."""
        log = Path(path) if path else self.oc_dir / LOG_FILE
        try:
            with open(log, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return []
        return lines[-n:]

    # ------------------------------ pid -------------------------------- #

    def run_pid(self, pid_path: Optional[Path] = None) -> Optional[int]:
        """PID dal file run.pid (assente/invalido → None)."""
        p = Path(pid_path) if pid_path else self.oc_dir / RUN_PID
        try:
            return int(p.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    # ---------------------------- processo ----------------------------- #

    def engine_process_active(self, pattern: str = "[o]c3600[.]sh",
                              ) -> Optional[int]:
        """Primo PID di un processo engine attivo, o None.

        pgrep -f con pattern a DOPPIA parentesi (regola campo: mai includere
        il path letterale nel comando di check — self-match). Pattern e
        comando iniettabili nei test.
        """
        try:
            r = subprocess.run(
                [self._pgrep, "-f", pattern],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in r.stdout.splitlines():
            pid = line.strip()
            if pid.isdigit():
                return int(pid)
        return None
