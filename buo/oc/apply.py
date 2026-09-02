#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
ApplyManager — applicazione profili con sequenza di sicurezza completa.

Sequenza A (apply volatile di default): precondizioni → backup → marcatore
apply.json → stop governor + VERIFICA esplicita (mai cache TTL) → apply conf
→ smoke 30s → persist opzionale (--persist + conferma) → riavvio governor
(SEMPRE, finally-style) → finalize.

Sequenza R (rollback automatico, fail-closed): trigger = bc250-apply rc≠0 |
smoke fail | critical → ripristino backup → re-apply → governor su →
apply.json rolled_back.

Sequenza D (interruzione client / hang): marcatore apply.json con
stale-detection → heal() ripristina e riavvia il governor.

Invarianti I1-I5: mai SMU con governor attivo; governor SEMPRE riavviato;
anti-zona via validator; mai stress-ng --timeout 0; OC opt-in (volatile,
--persist con conferma).
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..constants import SMU_OC_SERVICE
from ..utils.shell import run_command
from .constants import (
    APPLY_LOG,
    APPLY_MARKER,
    BACKUP_SUFFIX,
    OC_DIR_DEFAULT,
    SMOKE_STRESS_S,
    SMU_OC_CONF,
    TEMP_CRITICAL,
    TEMP_GATE,
    UNIT_NAME,
)
from .profiles import Profile, ProfileStore, ProfileValidator, SiliconView
from .smoke import CpuSmoke, boot_epoch

logger = logging.getLogger("buo.oc.apply")


@dataclass
class ApplyOutcome:
    result: str            # ok | rolled_back | aborted | stale
    profile: Optional[str] = None
    persisted: bool = False
    cause: Optional[str] = None
    details: List[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class ApplyManager:
    def __init__(self, controller, store: Optional[ProfileStore] = None,
                 validator: Optional[ProfileValidator] = None,
                 smoke: Optional[CpuSmoke] = None,
                 reader=None,
                 bc250_apply_cmd: str = "/usr/local/bin/bc250-apply",
                 mock: bool = False, dry_run: bool = False,
                 systemctl_cmd: str = "systemctl",
                 oc_dir: Optional[Path] = None,
                 sudo: bool = True,
                 smu_conf: Optional[str] = None):
        self.controller = controller
        self.oc_dir = Path(oc_dir) if oc_dir else Path(OC_DIR_DEFAULT)
        self.store = store or ProfileStore(self.oc_dir)
        self.validator = validator or ProfileValidator()
        self.silicon = SiliconView(self.oc_dir)
        self.reader = reader
        self.bc250_apply = bc250_apply_cmd
        self.systemctl = systemctl_cmd
        self.smu_conf = smu_conf or SMU_OC_CONF
        self.mock = mock
        self.dry_run = dry_run
        self.sudo = sudo
        self.smoke = smoke or CpuSmoke(
            reader, mock=mock, oc_dir=self.oc_dir, sudo=sudo)
        self._marker_path = self.oc_dir / APPLY_MARKER

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _log(self, details: List[str], msg: str) -> None:
        details.append(msg)
        logger.info(msg)

    def _cmd(self, argv: List[str], timeout: int = 60
             ) -> "tuple[int, str, str]":
        if self.mock or self.dry_run:
            logger.info("[MOCK/DRY-RUN] %s", " ".join(argv))
            return 0, "", ""
        return run_command(argv, timeout=timeout, sudo=self.sudo)

    def _sysctl(self, args: List[str], timeout: int = 30):
        return self._cmd([self.systemctl] + args, timeout=timeout)

    def _governor_active(self) -> str:
        """is-active ESPLICITO (mai la cache TTL del reader: regola assoluta
        per le azioni che scrivono l'SMU)."""
        if self.mock or self.dry_run:
            return "inactive"
        rc, out, _ = self._sysctl(["is-active",
                                   "cyan-skillfish-governor-smu"])
        return out.strip() if rc == 0 else "inactive"

    def _write_marker(self, state: str, profile: Optional[str],
                      persisted: bool = False,
                      cause: Optional[str] = None,
                      pid: Optional[int] = None) -> None:
        data = {
            "state": state,
            "profile": profile,
            "persisted": persisted,
            "cause": cause,
            "started_epoch": int(time.time()),
            "pid": pid if pid is not None else os.getpid(),
        }
        _write_json_atomic(self._marker_path, data)

    def _marker_stale(self) -> Optional[Dict[str, Any]]:
        """True se apply.json è 'applying' e il processo tool è morto o il
        boot è successivo all'avvio dell'apply (hang)."""
        try:
            raw = json.loads(self._marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if raw.get("state") != "applying":
            return None
        pid = raw.get("pid")
        if pid:
            try:
                os.kill(int(pid), 0)
                alive = True
            except (OSError, ProcessLookupError):
                alive = False
            except PermissionError:
                alive = True
            if alive:
                # processo vivo → apply in corso, non stale
                started = raw.get("started_epoch")
                boot = boot_epoch()
                if started and boot and int(started) >= boot:
                    return None
        return raw

    def _backup_path(self) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return Path(self.smu_conf + BACKUP_SUFFIX + "-" + ts)

    def _backup(self, details: List[str]) -> Optional[Path]:
        """cp conf → backup (PRIMA di qualsiasi modifica); fail → abort."""
        dest = self._backup_path()
        rc, _o, err = self._cmd(["cp", self.smu_conf, str(dest)], timeout=15)
        if rc != 0:
            self._log(details, f"backup FALLITO ({err.strip()}) — abort, "
                              "nulla toccato")
            return None
        self._log(details, f"backup: {self.smu_conf} → {dest}")
        return dest

    def _write_conf(self, profile: Profile) -> Path:
        """conf [overclock] dal profilo; max_temperature dal silicio se noto
        (clamp [85,90]), altrimenti 85."""
        mt = self.silicon.thermal_max_temperature()
        if mt is None:
            mt = TEMP_GATE
        mt = max(TEMP_GATE, min(TEMP_CRITICAL, int(mt)))
        conf = self.oc_dir / f"apply-{profile.id}.conf"
        conf.write_text(
            f"[overclock]\nfrequency = {profile.freq}\n"
            f"scale = {profile.scale}\nmax_temperature = {mt}\n",
            encoding="utf-8")
        return conf

    def _restore_backup(self, backup: Path, details: List[str]) -> bool:
        """Ripristina il backup e ri-applica (governor GIÀ fermo)."""
        rc, _o, err = self._cmd(["cp", str(backup), self.smu_conf], timeout=15)
        if rc != 0:
            self._log(details, f"ripristino backup FALLITO ({err.strip()})")
            return False
        self._log(details, f"conf ripristinato: {backup} → {self.smu_conf}")
        rc2, _o2, err2 = self._cmd(
            [self.bc250_apply, "--apply", self.smu_conf], timeout=60)
        if rc2 != 0:
            self._log(details, f"re-apply backup FALLITO ({err2.strip()}) — "
                               "il governor verrà comunque riavviato")
        return True

    def _governor_stop_verified(self, details: List[str]) -> bool:
        """Stop + VERIFICA esplicita is-active → inactive (retry 1×)."""
        if self.mock or self.dry_run:
            self._log(details, "[MOCK] governor fermo e VERIFICATO")
            return True
        self._cmd([self.systemctl, "stop", "cyan-skillfish-governor-smu"],
                  timeout=30)
        if self._governor_active() == "inactive":
            self._log(details, "governor fermo e VERIFICATO (inactive)")
            return True
        time.sleep(2)
        self._cmd([self.systemctl, "stop", "cyan-skillfish-governor-smu"],
                  timeout=30)
        if self._governor_active() == "inactive":
            self._log(details, "governor fermo e VERIFICATO (inactive, retry)")
            return True
        self._log(details, "governor NON fermabile — abort, nulla toccato")
        return False

    def _governor_start_verified(self, details: List[str]) -> bool:
        """Start + verifica is-active (retry 1×); fail → alert esplicito."""
        if self.mock or self.dry_run:
            self._log(details, "[MOCK] governor attivo (VERIFICATO)")
            return True
        self._cmd([self.systemctl, "start", "cyan-skillfish-governor-smu"],
                  timeout=30)
        if self._governor_active() == "active":
            self._log(details, "governor attivo (VERIFICATO)")
            return True
        time.sleep(2)
        self._cmd([self.systemctl, "start", "cyan-skillfish-governor-smu"],
                  timeout=30)
        if self._governor_active() == "active":
            self._log(details, "governor attivo (VERIFICATO, retry)")
            return True
        self._log(details, "🚨 governor NON ripartito — la macchina gira ma "
                           "senza governor GPU (curve assente: +7°C sotto "
                           "carico). Avviare: systemctl start "
                           "cyan-skillfish-governor-smu")
        return False

    # ------------------------------------------------------------------ #
    # Sequenza A + R
    # ------------------------------------------------------------------ #

    def apply(self, profile: Profile, persist: bool = False,
              yes: bool = False,
              on_progress: Optional[Callable[[str], None]] = None
              ) -> ApplyOutcome:
        details: List[str] = []
        if on_progress:
            on_progress(f"apply {profile.name}…")

        # 1. Precondizioni
        if self.controller.process_active() is not None:
            return ApplyOutcome("aborted", profile.id, False,
                                "run engine attiva (l'engine possiede "
                                "l'SMU) — REFUSE", details)
        ok, reason = self.validator.zone_ok(profile)
        if not ok:
            return ApplyOutcome("aborted", profile.id, False, reason, details)
        if not os.path.exists(self.bc250_apply) and not (self.mock
                                                         or self.dry_run):
            return ApplyOutcome("aborted", profile.id, False,
                                f"bc250-apply non presente: {self.bc250_apply}",
                                details)
        if (not self.mock and not self.dry_run
                and not os.path.exists(self.smu_conf)):
            return ApplyOutcome("aborted", profile.id, False,
                                f"conf assente: {self.smu_conf}", details)

        # 2. Backup (prima di QUALSIASI modifica)
        backup = self._backup(details)
        if backup is None:
            return ApplyOutcome("aborted", profile.id, False,
                                "backup fallito", details)

        # 3. Marcatore apply
        self._write_marker("applying", profile.id)

        # 4. Stop governor + verifica esplicita
        if not self._governor_stop_verified(details):
            self._restart_after_abort(details)
            return ApplyOutcome("aborted", profile.id, False,
                                "governor non fermato", details)

        # 5. Apply conf (volatile)
        conf = self._write_conf(profile)
        rc, _o, err = self._cmd([self.bc250_apply, "--apply", str(conf)],
                                timeout=90)
        if rc != 0:
            return self._rollback(profile, backup, details,
                                  f"bc250-apply rc={rc}: {err.strip()}")

        # 6. Smoke 30s
        if on_progress:
            on_progress(f"smoke {SMOKE_STRESS_S}s…")
        result = self.smoke.run(profile.freq, profile.vid_cap)
        if not result.ok:
            return self._rollback(profile, backup, details,
                                  f"smoke fail: {result.cause}")

        # 7. Persist opzionale (SOLO --persist + conferma)
        persisted = False
        if persist:
            if not yes:
                self._restart_after_abort(details)
                return ApplyOutcome("aborted", profile.id, False,
                                    "persist richiede conferma esplicita "
                                    "(--yes)", details)
            rc2, _o2, err2 = self._cmd(
                [self.bc250_apply, "--install", str(conf)], timeout=90)
            if rc2 != 0:
                self._log(details, f"persist_error: --install fallito "
                                   f"({err2.strip()}) — NON bloccante")
            else:
                rc3, _o3, err3 = self._sysctl(
                    ["enable", SMU_OC_SERVICE], timeout=30)
                if rc3 != 0:
                    self._log(details, f"persist_error: enable {SMU_OC_SERVICE} "
                                       f"fallito ({err3.strip()}) — NON "
                                       "bloccante")
                else:
                    persisted = True
                    self._log(details, f"persist OK: --install + enable "
                                       f"{SMU_OC_SERVICE} (riapplica al boot)")

        # 8. Riavvio governor (SEMPRE, finally-style)
        self._governor_start_verified(details)

        # 9. Finalize
        self._write_marker("ok", profile.id, persisted)
        self._update_store(profile, persisted)
        self._append_apply_log(details, "ok", profile.id, persisted)
        if on_progress:
            on_progress(f"apply {profile.name}: OK")
        return ApplyOutcome("ok", profile.id, persisted, None, details)

    def _rollback(self, profile: Profile, backup: Path, details: List[str],
                  cause: str) -> ApplyOutcome:
        """Sequenza R — governor GIÀ fermo: ripristino → re-apply → governor
        su → marcatore rolled_back. MAI persistire un punto fallito."""
        self._log(details, f"⚠️ ROLLBACK ({cause})")
        self._restore_backup(backup, details)
        self._governor_start_verified(details)
        self._write_marker("rolled_back", profile.id, cause=cause)
        self._append_apply_log(details, "rolled_back", profile.id, False,
                               cause)
        return ApplyOutcome("rolled_back", profile.id, False, cause, details)

    def _restart_after_abort(self, details: List[str]) -> None:
        self._write_marker("aborted", None)
        self._governor_start_verified(details)

    # ------------------------------------------------------------------ #
    # restore-stock
    # ------------------------------------------------------------------ #

    def restore_stock(self, persist: bool = False,
                      yes: bool = False) -> ApplyOutcome:
        stock = self.store.get("stock")
        if stock is None:
            return ApplyOutcome("aborted", "stock", False,
                                "profilo stock assente")
        outcome = self.apply(stock, persist=False, yes=yes)
        if persist and outcome.result == "ok":
            if not yes:
                return ApplyOutcome("aborted", "stock", False,
                                    "disable del servizio richiede "
                                    "conferma (--yes)")
            rc, _o, err = self._sysctl(["disable", SMU_OC_SERVICE],
                                       timeout=30)
            if rc != 0:
                outcome.details.append(
                    f"persist_error: disable {SMU_OC_SERVICE} fallito "
                    f"({err.strip()})")
            else:
                outcome.persisted = False
                outcome.details.append(
                    f"servizio {SMU_OC_SERVICE} DISABILITATO — boot stock-safe")
            self._write_marker("ok", "stock", False)
        return outcome

    # ------------------------------------------------------------------ #
    # Sequenza D — heal
    # ------------------------------------------------------------------ #

    def heal(self) -> ApplyOutcome:
        """Sanifica un apply interrotto: marcatore stale (processo tool morto
        o boot successivo) + governor inattivo → ripristino backup + governor
        su + marcatore rolled_back."""
        details: List[str] = []
        stale = self._marker_stale()
        if stale is None:
            # niente da sanificare: solo eventuale governor fermo → riavvio
            if self._governor_active() != "active":
                self._governor_start_verified(details)
                return ApplyOutcome("ok", stale.get("profile") if stale
                                    else None, False,
                                    "governor riavviato (nessun apply "
                                    "interrotto)", details)
            return ApplyOutcome("ok", None, False, "nessuna sanificazione "
                               "necessaria", details)

        profile = stale.get("profile")
        self._log(details, "apply INTERROTTO (marcatore stale) — sequenza D")
        backups = sorted(
            Path(self.smu_conf).parent.glob(
                Path(self.smu_conf).name + ".buo-rollback-*"))
        restored = False
        if backups:
            latest = backups[-1]
            restored = self._restore_backup(latest, details)
        else:
            self._log(details, "nessun backup trovato — il governor verrà "
                               "comunque riavviato (fail-closed)")
        self._governor_start_verified(details)
        self._write_marker("rolled_back", profile, cause="stale")
        self._append_apply_log(details, "rolled_back", profile, False,
                               "stale")
        return ApplyOutcome("rolled_back", profile, False,
                            "stale" if not restored else None, details)

    # ------------------------------------------------------------------ #
    # finalize
    # ------------------------------------------------------------------ #

    def _update_store(self, profile: Profile, persisted: bool) -> None:
        profiles = self.store.load()
        for p in profiles:
            if p.id == profile.id:
                p.last_applied = _now()
                p.validated = True
        self.store.save(
            profiles, active=profile.id,
            last_apply={"profile": profile.id, "ts": _now(),
                        "result": "ok", "persisted": persisted,
                        "cause": None})

    def _append_apply_log(self, details: List[str], result: str,
                          profile: Optional[str], persisted: bool,
                          cause: Optional[str] = None) -> None:
        line = (f"[{_now()}] result={result} profile={profile or '-'} "
                f"persisted={persisted} cause={cause or '-'}")
        details.append(line)
        try:
            self.oc_dir.mkdir(parents=True, exist_ok=True)
            with open(self.oc_dir / APPLY_LOG, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
