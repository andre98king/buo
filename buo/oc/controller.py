#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
OcController — operazioni operative sul motore OC (status/run/stop/reset/
watch/heal). La ricerca OC resta nel motore bash (oc3600.sh): qui si lancia,
si ferma, si legge lo stato. Sequenze di sicurezza: start = unità systemd
TRANSIENTE (root-cause session-scope-kill); stop = SIGTERM pulito; mai SMU
con governor attivo (le azioni che scrivono l'SMU stanno in apply.py).

`--mock`/`--dry-run` → nessun comando reale (pattern BUO).
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..constants import GOVERNOR_SERVICE
from ..utils.paths import SYSTEM_STATE_DIR, state_dir
from ..utils.shell import run_command
from .constants import (
    APPLY_MARKER,
    CONSOLE_LOG,
    ENGINE_SCRIPT,
    LAUNCH_PID,
    OC_DIR_DEFAULT,
    RUN_PID,
    STATE_FILE,
    UNIT_NAME,
)
from .state import OcStateReader

logger = logging.getLogger("buo.oc.controller")


class OcController:
    def __init__(self, oc_dir: Optional[Path] = None, mock: bool = False,
                 dry_run: bool = False,
                 systemctl_cmd: str = "systemctl",
                 systemd_run_cmd: str = "systemd-run",
                 sudo: bool = True,
                 state_reader: Optional[OcStateReader] = None,
                 reader=None):
        self.oc_dir = Path(oc_dir) if oc_dir else Path(OC_DIR_DEFAULT)
        self.mock = mock
        self.dry_run = dry_run
        self.systemctl = systemctl_cmd
        self.systemd_run = systemd_run_cmd
        self.sudo = sudo
        self.state_reader = state_reader or OcStateReader(self.oc_dir)
        self.reader = reader
        self.engine_path = self.oc_dir / ENGINE_SCRIPT

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _cmd(self, argv: List[str], timeout: int = 30) -> "tuple[int, str, str]":
        if self.mock or self.dry_run:
            logger.info("[MOCK/DRY-RUN] %s", " ".join(argv))
            return 0, "", ""
        return run_command(argv, timeout=timeout, sudo=self.sudo)

    def _sysctl(self, args: List[str], timeout: int = 30):
        return self._cmd([self.systemctl] + args, timeout=timeout)

    def engine_ok(self) -> bool:
        return self.engine_path.exists() and os.access(self.engine_path,
                                                       os.X_OK)

    def process_active(self) -> Optional[int]:
        """PID engine attivo (pgrep pattern sicuro) o None."""
        if self.mock or self.dry_run:
            return None
        return self.state_reader.engine_process_active()

    # ------------------------------------------------------------------ #
    # status
    # ------------------------------------------------------------------ #

    def status(self) -> Dict[str, Any]:
        """Riepilogo completo (porting cmd_status di run-oc.sh)."""
        st = self.state_reader.read_state()
        proc = self.process_active()
        run_pid = self.state_reader.run_pid()
        gov = self._governor_active()
        return {
            "oc_dir": str(self.oc_dir),
            "system_state": state_dir() == SYSTEM_STATE_DIR,
            "engine": {
                "present": self.engine_path.exists(),
                "executable": os.access(self.engine_path, os.X_OK),
            },
            "process": {
                "active": proc is not None,
                "pid": proc or run_pid,
            },
            "state": self._state_dict(st),
            "governor": gov,
            "tctl_c": self._tctl(),
            "stress_ng_processes": self._stress_ng_count(),
            "apply": self._apply_state(),
            "log_tail": self.state_reader.log_tail(),
        }

    @staticmethod
    def _state_dict(st) -> Dict[str, Any]:
        return {
            "phase": st.phase,
            "phase_label": st.phase_label,
            "testing": None if st.testing is None else {
                "freq": st.testing.freq, "vid_cap": st.testing.vid_cap,
                "kind": st.testing.kind,
                "started_epoch": st.testing.started_epoch,
            },
            "l2": None if st.l2 is None else {
                "status": st.l2.status, "target_freq": st.l2.target_freq,
                "target_vid": st.l2.target_vid, "runs": st.l2.runs,
            },
            "winner": None if st.winner_clock is None else {
                "freq": st.winner_clock,
                "vid_cap": None if st.winner_vmin is None
                else st.winner_vmin.cap,
                "scale": None if st.winner_vmin is None
                else st.winner_vmin.scale,
            },
            "best_known_good": None if st.best_known_good is None else {
                "freq": st.best_known_good.freq,
                "vid_cap": st.best_known_good.vid_cap,
                "scale": st.best_known_good.scale,
            },
            "persisted": st.persisted,
        }

    def _governor_active(self) -> str:
        """is-active ESPLICITO (read-only, sicuro) — mai la cache TTL per le
        azioni che scrivono l'SMU (regola: check esplicito, non reader)."""
        rc, out, _ = self._sysctl(["is-active", GOVERNOR_SERVICE])
        if self.mock or self.dry_run:
            return "unknown"
        return out.strip() if rc == 0 else "inactive"

    def _tctl(self) -> Optional[float]:
        if self.reader is None:
            return None
        try:
            return self.reader.get_cpu_temp()
        except Exception:
            return None

    def _stress_ng_count(self) -> int:
        if self.mock or self.dry_run:
            return 0
        try:
            import subprocess
            r = subprocess.run(["pgrep", "-cx", "stress-ng"],
                               capture_output=True, text=True, timeout=10)
            out = r.stdout.strip()
            return int(out) if out.isdigit() else 0
        except Exception:
            return 0

    def _apply_state(self) -> Dict[str, Any]:
        path = self.oc_dir / APPLY_MARKER
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            state = raw.get("state", "unknown")
        except (OSError, ValueError, json.JSONDecodeError):
            return {"state": "none", "profile": None, "persisted": False}
        return {
            "state": state,
            "profile": raw.get("profile"),
            "persisted": bool(raw.get("persisted", False)),
        }

    # ------------------------------------------------------------------ #
    # start / stop / reset / watch
    # ------------------------------------------------------------------ #

    def start(self, flags: List[str]) -> None:
        """Lancia la run engine come unità systemd TRANSIENTE (spec
        run-oc.sh cmd_start path REALE: fuori dal cgroup della sessione ssh
        → la morte del launcher NON uccide la run). Resume implicito se
        state.json presente."""
        if self.process_active() is not None:
            raise RuntimeError("un run engine è GIÀ attivo — stop prima di "
                               "rilanciare")
        if not self.engine_ok():
            raise RuntimeError(f"engine non presente/eseguibile: "
                               f"{self.engine_path}")

        console = self.oc_dir / CONSOLE_LOG
        argv = [
            self.systemd_run,
            "--unit", UNIT_NAME,
            "--collect",
            "--working-directory", str(self.oc_dir),
            "--setenv", f"OC_DIR={self.oc_dir}",
            "--setenv", "OC_CWD=/tmp/oc",
            "--property", f"StandardOutput=append:{console}",
            "--property", f"StandardError=append:{console}",
            "bash", str(self.engine_path),
        ] + list(flags)

        if self.mock or self.dry_run:
            logger.info("[MOCK/DRY-RUN] systemd-run %s", " ".join(argv[1:]))
            return

        rc, out, err = self._cmd(argv, timeout=60)
        if rc != 0:
            raise RuntimeError(f"systemd-run fallito (rc={rc}): {err or out}")

        # launch.pid = MainPID dell'unità (spec run-oc.sh)
        rc2, pid, _ = self._sysctl(
            ["show", "-p", "MainPID", "--value", UNIT_NAME])
        if rc2 == 0 and pid.strip().isdigit():
            self.oc_dir.mkdir(parents=True, exist_ok=True)
            (self.oc_dir / LAUNCH_PID).write_text(pid.strip() + "\n",
                                                  encoding="utf-8")

    def stop(self) -> None:
        """systemctl stop buo-oc → trap engine → exit 40 riprendibile.
        Unità assente → niente da fermare."""
        rc, out, _ = self._sysctl(["is-active", UNIT_NAME])
        if self.mock or self.dry_run:
            logger.info("[MOCK/DRY-RUN] stop %s", UNIT_NAME)
            return
        active = out.strip()
        if active not in ("active", "activating"):
            logger.info("unità %s non attiva (%s) — niente da fermare",
                        UNIT_NAME, active)
            return
        rc2, _o, err = self._sysctl(["stop", UNIT_NAME])
        if rc2 != 0:
            raise RuntimeError(f"systemctl stop {UNIT_NAME} fallito: {err}")

    def reset(self, confirm: bool) -> None:
        """Azzera il checkpoint (state.json + run.pid + launch.pid). MAI /etc,
        MAI i log. Default richiede conferma esplicita."""
        if not confirm:
            raise RuntimeError("reset richiede conferma (--yes)")
        if self.mock or self.dry_run:
            # M2: le modalità simulate NON toccano MAI lo stato reale
            # (un --dry-run --yes non deve cancellare i file veri)
            logger.info("[MOCK/DRY-RUN] reset saltato (nessuna scrittura)")
            return
        if self.process_active() is not None:
            raise RuntimeError("run attiva — stop prima del reset")
        for name in (STATE_FILE, RUN_PID, LAUNCH_PID):
            try:
                (self.oc_dir / name).unlink()
            except FileNotFoundError:
                pass

    def watch(self, every: int = 10) -> None:
        """Vista live CLI ogni N secondi (porting cmd_watch)."""
        while True:
            st = self.status()
            phase = st["state"].get("phase_label", "?")
            testing = st["state"].get("testing")
            t = ""
            if testing and testing.get("freq"):
                t = f" · testing {testing['freq']}@{testing['vid_cap']}"
            proc = "ATTIVO" if st["process"]["active"] else "fermo"
            print(f"[{time.strftime('%H:%M:%S')}] fase: {phase}{t} · "
                  f"processo: {proc} · governor: {st['governor']}",
                  flush=True)
            try:
                time.sleep(every)
            except KeyboardInterrupt:
                break

    # ------------------------------------------------------------------ #
    # heal (delega all'ApplyManager per la sequenza D)
    # ------------------------------------------------------------------ #

    def heal(self) -> Dict[str, Any]:
        from .apply import ApplyManager
        mgr = ApplyManager(controller=self, mock=self.mock,
                           dry_run=self.dry_run, sudo=self.sudo)
        outcome = mgr.heal()
        return {"result": outcome.result, "cause": outcome.cause,
                "details": outcome.details}
