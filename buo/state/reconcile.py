#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Riconciliazione di boot della BC-250 (agente `buo boot-reconcile`).

PERCHÉ ESISTE (campo 11/09/2026): dopo un **cold boot** la macchina perde stati
che il ledger di BUO considera ancora validi, perché lo stato "intellettuale"
sta in `/var/lib/buo` (condiviso e persistente) mentre lo stato "operativo" è
volatile o vive altrove:

- la **maschera core** è un registro SMN volatile: un power-off la azzera →
  12 thread, mentre il ledger dice `cpu_core_unlock` fatto;
- il **governor GPU** può non partire al boot (niente curva 800 mV, GPU a
  stock) e nessuna fase di BUO se ne accorgeva;
- le **tabelle ACPI** vivono nella entry BLS bootata: ogni transazione ostree
  rigenera le entry e le tabelle spariscono in silenzio.

Questo modulo verifica l'EFFETTO REALE (thread online, governor attivo, entry
bootata) e ripara solo ciò che manca, con un tetto di tentativi persistente e
un kill-switch da cmdline: **mai un ciclo di reboot**.

Regole di sicurezza rispettate qui:
- MAI accessi SMU con il governor attivo → prima di `cpu.unlock()` il governor
  viene fermato con stato CONFERMATO (`systemctl show -p ActiveState`: solo
  `inactive`/`failed` sono "fermo"; `activating`/`deactivating` sono fail-closed);
- mai un reboot con una sessione di gioco attiva (gate largo: `.exe`, steam,
  proton/pressure-vessel, gamescope);
- reset forzato **warm**: la maschera sopravvive al warm reset, non a un cold
  reset (validato sul campo: warm reboot → 16 thread).
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..constants import GOVERNOR_SERVICE, SMU_OC_SERVICE
from ..utils.logging import LoggerMixin
from ..utils.paths import state_dir
from ..utils.shell import run_command

# Unità systemd dell'agente di boot.
BOOT_UNIT = "buo-boot-reconcile.service"
UNIT_PATH = Path("/etc/systemd/system") / BOOT_UNIT
# Kill-switch da cmdline (convenzione community: bc250.nocoreunlock).
KILL_SWITCH = "bc250.nocoreunlock"
# Tetto tentativi: 2 = al massimo un reboot automatico in più dopo un cold
# boot; esauriti i tentativi la maschera si riscrive ma NIENTE reboot.
MAX_ATTEMPTS = 2
THREADS_EXPECTED = 16
# Servizio 40-CU e OC CPU: devono essere abilitati al boot (i loro conf in
# /etc possono perdersi cambiando deployment ostree).
BOOT_SERVICES = (SMU_OC_SERVICE, "bc250-cu-live-manager")
# Stati systemd che significano "davvero fermo" (fail-closed su activating).
INACTIVE_STATES = frozenset({"inactive", "failed"})
# Gate: un GIOCO in esecuzione blocca sempre (un .exe Proton/launcher o il
# runtime pressure-vessel). Steam/gamescope da soli = sessione desktop, non un
# gioco: bloccano solo le run MANUALI (--boot le consente, perché su questa
# macchina Steam È la sessione e al boot sarebbe sempre "attiva").
GAME_PATTERNS = (r"\.exe", "pressure-vessel")
SESSION_PATTERNS = ("steam", "gamescope")


class BootReconciler(LoggerMixin):
    """Verifica e ripristina lo stato certificato a ogni accensione."""

    def __init__(self, cpu, governor, acpi, *, dry_run: bool = False,
                 expected_threads: int = THREADS_EXPECTED,
                 max_attempts: int = MAX_ATTEMPTS,
                 attempts_path: Optional[Path] = None,
                 present_path: Path = Path("/sys/devices/system/cpu/present"),
                 cmdline_path: Path = Path("/proc/cmdline"),
                 reboot_mode_path: Path = Path("/sys/kernel/reboot/mode"),
                 reboot_fn=None, game_check_fn=None, verdict=None,
                 boot_run: bool = False,
                 services: tuple = BOOT_SERVICES):
        # Dipendenze iniettate (testabilità: mai hardware reale nei test).
        self.cpu = cpu
        self.governor = governor
        self.acpi = acpi
        self.dry_run = dry_run
        self.expected_threads = expected_threads
        self.max_attempts = max_attempts
        self.present_path = Path(present_path)
        self.cmdline_path = Path(cmdline_path)
        self.reboot_mode_path = Path(reboot_mode_path)
        self._attempts_path = Path(attempts_path) if attempts_path else None
        self._reboot_fn = reboot_fn or self._reboot_real
        self._game_check_fn = game_check_fn or self._game_active_real
        # boot_run: invocazione dall'unità di boot (sessione non ancora avviata
        # o appena avviata) → la sola presenza della sessione Steam non blocca.
        self.boot_run = boot_run
        self._verdict = verdict
        self.services = tuple(services)

    # ------------------------------------------------------------------ #
    # Letture reali (nessuna modifica)
    # ------------------------------------------------------------------ #

    @property
    def attempts_path(self) -> Path:
        if self._attempts_path is None:
            self._attempts_path = state_dir() / "boot-reconcile-attempts"
        return self._attempts_path

    def _read_attempts(self) -> int:
        try:
            value = int(self.attempts_path.read_text(encoding="utf-8").strip())
            return value if value >= 0 else 0
        except (OSError, ValueError):
            return 0

    def _write_attempts(self, value: int) -> None:
        if self.dry_run:
            return
        try:
            self.attempts_path.parent.mkdir(parents=True, exist_ok=True)
            self.attempts_path.write_text(f"{value}\n", encoding="utf-8")
        except OSError as e:
            self.logger.warning("Contatore tentativi non scritto: %s", e)

    def threads_present(self) -> Optional[int]:
        """Thread CPU presenti secondo il kernel (None se illeggibile).

        È la verità che conta: la maschera SMN non è leggibile senza accesso
        SMU e il suo EFFETTO è esattamente quante CPU il kernel ha enumerato.
        """
        try:
            text = self.present_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        total = 0
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo, hi = part.split("-", 1)
                total += int(hi) - int(lo) + 1
            else:
                total += 1
        return total or None

    def cpu_condemned(self) -> bool:
        """True se BUO ha condannato il silicio (verdetto `never_unlock`).

        Guard indispensabile: senza di esso l'agente riattiverebbe i core
        extra di un die harvest DIFETTOSO a ogni accensione. Semantica di
        `UnlockVerdict`: solo il verdetto esplicito è un veto (file
        assente/corrotto = la macchina ri-sblocca e ri-valida).
        """
        try:
            if self._verdict is None:
                from ..unlock.validation import UnlockVerdict
                self._verdict = UnlockVerdict()
            return self._verdict.get("cpu") == "never_unlock"
        except Exception as e:  # pragma: no cover - difesa
            self.logger.warning("Verdetto silicio non leggibile: %s", e)
            return False

    def kill_switch(self) -> bool:
        """True se la cmdline contiene il kill-switch (nessun reboot mai)."""
        try:
            return KILL_SWITCH in self.cmdline_path.read_text(errors="replace")
        except OSError:
            return False

    def governor_state(self) -> Optional[str]:
        """ActiveState del governor (None se non determinabile).

        `systemctl is-active` NON basta: esce con rc=3 anche per
        `activating`/`deactivating` (stati in cui il governor sta scrivendo
        sull'SMU) → leggere ActiveState distingue "fermo" da "in transito".
        """
        rc, out, _ = run_command(
            ["systemctl", "show", "-p", "ActiveState", "--value",
             GOVERNOR_SERVICE], timeout=15)
        if rc != 0:
            return None
        return out.strip() or None

    def _game_active_real(self) -> bool:
        """True se un gioco è in esecuzione (gate largo, regola di campo)."""
        patterns = (GAME_PATTERNS if self.boot_run
                    else GAME_PATTERNS + SESSION_PATTERNS)
        for pattern in patterns:
            rc, out, _ = run_command(["pgrep", "-f", pattern], timeout=10)
            if rc == 0 and out.strip():
                return True
        return False

    def _kargs(self) -> Dict[str, Any]:
        try:
            cmdline = self.cmdline_path.read_text(errors="replace")
        except OSError:
            cmdline = ""
        return {
            "mitigations_off": "mitigations=off" in cmdline,
            "pages_limit": next((tok.split("=", 1)[1] for tok in cmdline.split()
                                 if tok.startswith("ttm.pages_limit=")), None),
        }

    def _service_enabled(self, name: str) -> Optional[bool]:
        rc, out, _ = run_command(["systemctl", "is-enabled", name], timeout=15)
        if rc == 0:
            return True
        if rc == 1 or (out and out.strip() in {"disabled", "masked"}):
            return False
        return None

    def _service_ran_this_boot(self, name: str) -> bool:
        """True se il servizio oneshot è già girato in questo boot."""
        rc, out, _ = run_command(
            ["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value",
             name], timeout=15)
        return bool(rc == 0 and out.strip())

    # ------------------------------------------------------------------ #
    # Verifica
    # ------------------------------------------------------------------ #

    def check(self) -> Dict[str, Any]:
        """Fotografia dello stato reale (sola lettura, nessun reboot)."""
        threads = self.threads_present()
        state = self.governor_state()
        try:
            acpi_ok = bool(self.acpi.verify())
        except Exception as e:  # pragma: no cover - difesa
            self.logger.warning("Verifica ACPI fallita: %s", e)
            acpi_ok = None
        services = {name: self._service_enabled(name) for name in self.services}
        ran = {name: self._service_ran_this_boot(name) for name in self.services}
        return {
            "threads": threads,
            "threads_ok": (threads is not None
                           and threads >= self.expected_threads),
            "governor_state": state,
            "governor_ok": state == "active",
            "acpi_booted": acpi_ok,
            "acpi_ok": acpi_ok is True,
            "services": services,
            "services_ran": ran,
            # Abilitato NON basta: un oneshot che non è girato in questo boot
            # (enablement perso, avvio fallito) lascia OC CPU / 40 CU non
            # applicati → va avviato ora.
            "services_ok": (all(v is True for v in services.values())
                            and all(ran.values())),
            "cpu_condemned": self.cpu_condemned(),
            "kargs": self._kargs(),
            "attempts": self._read_attempts(),
            "kill_switch": self.kill_switch(),
        }

    def degraded(self, state: Optional[Dict[str, Any]] = None) -> List[str]:
        """Elenco dei pezzi NON a posto (per log e report)."""
        st = state or self.check()
        problems = []
        # Silicio condannato: 12 thread È lo stato corretto (veto never_unlock).
        if not st["threads_ok"] and not st.get("cpu_condemned"):
            problems.append(f"thread CPU: {st['threads']}/{self.expected_threads}")
        if not st["governor_ok"]:
            problems.append(f"governor GPU: {st['governor_state']}")
        if not st["acpi_ok"]:
            problems.append("tabelle ACPI non caricate (entry bootata)")
        for name, ok in st["services"].items():
            if ok is None:
                problems.append(f"stato del servizio non determinabile: {name}")
            elif ok is False:
                problems.append(f"servizio non abilitato: {name}")
            elif not st.get("services_ran", {}).get(name, True):
                problems.append(f"servizio non eseguito in questo boot: {name}")
        return problems

    # ------------------------------------------------------------------ #
    # Riparazione
    # ------------------------------------------------------------------ #

    def reconcile(self) -> Dict[str, Any]:
        """Verifica e ripara. Ritorna il report (chi lo chiama lo stampa)."""
        check = self.check()
        report: Dict[str, Any] = {
            "checked": check,
            "repaired": [],
            "skipped": [],
            "needs_reboot": False,
            "rebooted": False,
            "dry_run": self.dry_run,
        }
        if check["kill_switch"]:
            report["skipped"].append(
                f"kill-switch {KILL_SWITCH} presente in cmdline — nessuna azione")
            return report
        if not self.degraded(check):
            # Stato sano: il contatore dei tentativi si azzera (un cold boot
            # futuro riparte con il budget pieno).
            self._write_attempts(0)
            report["healthy"] = True
            if check.get("cpu_condemned"):
                report["skipped"].append(
                    f"silicio marcato never_unlock: {check['threads']} thread "
                    "è lo stato atteso (nessun accesso SMU)")
            return report

        # 1) 8 core: la maschera è volatile, l'effetto è il numero di thread.
        if not check["threads_ok"]:
            report["unlock"] = self._reunlock(check)
            if report["unlock"].get("needs_reboot"):
                report["needs_reboot"] = True

        # 2) tabelle ACPI sull'entry BOOTATA (le entry si rigenerano a ogni
        #    transazione ostree: il booted-entry è l'unico gate onesto).
        if not check["acpi_ok"]:
            report["acpi"] = self._reapply_acpi()
            if report["acpi"].get("needs_reboot"):
                report["needs_reboot"] = True

        # 3) governor attivo SOLO dopo l'eventuale accesso SMU: prima lo si
        #    ferma, quindi va riavviato (lezione campo 11/09: lo stop cancella
        #    lo start job del boot → senza questo la GPU resta a stock).
        if not self.governor_state() == "active":
            report["governor"] = self._start_governor()

        # 4) servizi di boot abilitati (conf in /etc per-deployment).
        report["services"] = self._fix_services(check)

        # 5) reboot: uno solo, guardato (tetto tentativi + gate gioco + warm).
        if report["needs_reboot"]:
            report["reboot"] = self._reboot_guarded(check)
            report["rebooted"] = bool(report["reboot"].get("rebooted"))
        return report

    def _reunlock(self, check: Dict[str, Any]) -> Dict[str, Any]:
        """Riscrive la maschera core con il governor davvero fermo."""
        if check.get("cpu_condemned"):
            self.logger.warning(
                "Silicio marcato never_unlock: i core extra restano spenti "
                "(nessun accesso SMU)")
            return {"unlocked": False, "needs_reboot": False,
                    "blocked": "silicio_condannato"}
        state = self.governor_state()
        if state is None:
            return {"unlocked": False, "needs_reboot": False,
                    "error": "stato governor non determinabile — accesso SMU "
                             "annullato (mai SMU con governor attivo)"}
        if self.dry_run:
            # Solo simulazione: nessuna scrittura SMU (e nessun log di azione)
            self.logger.info("(simulazione) ri-sbloccherei 8 core: %s thread",
                             check["threads"])
            return {"unlocked": False, "needs_reboot": False, "dry_run": True}
        self.logger.warning("Solo %s thread: ri-sblocco 8 core",
                            check["threads"])
        if state not in INACTIVE_STATES:
            try:
                stopped = self.governor.stop()
            except Exception:
                stopped = False
            state = self.governor_state()
            if not stopped or state not in INACTIVE_STATES:
                return {"unlocked": False, "needs_reboot": False,
                        "error": "governor non confermato FERMO — accesso SMU "
                                 "annullato"}
        try:
            out = self.cpu.unlock()
        except Exception as e:
            return {"unlocked": False, "needs_reboot": False, "error": str(e)}
        finally:
            # Sempre: lo stop qui sopra ha cancellato lo start job del boot.
            self._start_governor()
        unlocked = bool(out.get("unlocked") or out.get("mask") == "0xFF")
        # Visibile SUBITO: se poi si riavvia, l'output del comando viene troncato
        self.logger.warning("8 core: maschera %s (dettaglio: %s)",
                            "riscritta" if unlocked else "NON confermata", out)
        return {"unlocked": unlocked,
                "needs_reboot": unlocked,
                "detail": out}

    def _reapply_acpi(self) -> Dict[str, Any]:
        if self.dry_run:
            self.logger.info("(simulazione) riapplicherei le tabelle ACPI "
                             "sulla entry bootata")
            return {"applied": False, "dry_run": True}
        self.logger.warning("Tabelle ACPI non caricate dalla entry bootata: "
                            "riapplico")
        try:
            out = self.acpi.apply(force=True)
        except Exception as e:
            return {"applied": False, "error": str(e)}
        return out

    def _start_governor(self) -> Dict[str, Any]:
        if self.dry_run:
            return {"started": False, "dry_run": True}
        try:
            started = bool(self.governor.start())
        except Exception as e:
            return {"started": False, "error": str(e)}
        state = self.governor_state()
        return {"started": started, "state": state,
                "ok": state == "active"}

    def _fix_services(self, check: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for name, enabled in check["services"].items():
            if enabled is True:
                # Abilitato: se l'oneshot non è mai girato in questo boot lo
                # avvio ora (applica OC CPU / 40 CU senza reboot).
                if not self._service_ran_this_boot(name):
                    out[name] = self._start_service(name)
                continue
            if enabled is None:
                out[name] = {"fixed": False, "error": "stato non determinabile"}
                continue
            out[name] = self._enable_service(name)
        return out

    def _enable_service(self, name: str) -> Dict[str, Any]:
        self.logger.warning("Servizio non abilitato: %s — lo abilito", name)
        if self.dry_run:
            return {"fixed": False, "dry_run": True}
        rc, _, err = run_command(["systemctl", "enable", name], timeout=60,
                                 sudo=os.geteuid() != 0)
        if rc != 0:
            return {"fixed": False, "error": err or f"rc={rc}"}
        return {"fixed": True, **self._start_service(name)}

    def _start_service(self, name: str) -> Dict[str, Any]:
        rc, _, err = run_command(["systemctl", "start", name], timeout=120,
                                 sudo=os.geteuid() != 0)
        return {"started": rc == 0, "error": None if rc == 0 else (err or f"rc={rc}")}

    def _reboot_guarded(self, check: Dict[str, Any]) -> Dict[str, Any]:
        """UN reboot warm, con tetto tentativi e gate gioco. Mai un ciclo."""
        attempts = self._read_attempts()
        if attempts >= self.max_attempts:
            self.logger.error(
                "Reboot NON eseguito: tentativi esauriti (%d/%d) — possibile "
                "reset che non preserva la maschera; intervento manuale",
                attempts, self.max_attempts)
            return {"rebooted": False, "blocked": "tentativi_esauriti",
                    "attempts": attempts}
        if self._game_check_fn():
            self.logger.warning(
                "Reboot NON eseguito: gioco o sessione Steam attiva "
                "(riavviare a sessione chiusa: `sudo buo boot-reconcile`)")
            return {"rebooted": False, "blocked": "sessione_gioco_attiva"}
        if self.dry_run:
            return {"rebooted": False, "dry_run": True}
        self._write_attempts(attempts + 1)
        self._force_warm_reset()
        self.logger.warning(
            "Riavvio WARM (tentativo %d/%d) per attivare la riparazione: %s",
            attempts + 1, self.max_attempts, "; ".join(self.degraded(check)))
        return self._reboot_fn()

    def _force_warm_reset(self) -> Optional[str]:
        """La maschera core sopravvive al WARM reset, non al cold.

        Punto unico: `state.reboot.force_warm_reset` (usato anche da
        `RebootManager.schedule`), con il path iniettabile per i test.
        """
        if self.dry_run:
            return None
        from .reboot import force_warm_reset
        if force_warm_reset(self.reboot_mode_path):
            return "warm"
        self.logger.warning(
            "Reset NON forzato a warm: se il reset è cold la maschera può "
            "andare persa (il tetto tentativi evita il ciclo)")
        return None

    def _reboot_real(self) -> Dict[str, Any]:
        self.logger.info("Reboot per attivare lo stato riparato")
        rc, _, err = run_command(["systemctl", "reboot"], timeout=60,
                                 sudo=os.geteuid() != 0)
        return {"rebooted": rc == 0, "error": None if rc == 0 else (err or f"rc={rc}")}


# ---------------------------------------------------------------------- #
# Unità systemd: l'agente deve girare da solo a ogni accensione.
# ---------------------------------------------------------------------- #

def unit_content(python: str) -> str:
    """Contenuto dell'unità: ExecStart col python dell'installazione.

    `python -m buo` (non un path fisso di `buo`): funziona anche quando BUO
    vive in un venv fuori dal PATH di systemd (caso della BC-250: venv in
    /var/opt/buo-venv).
    """
    return f"""# BUO boot reconcile — generato automaticamente da `buo boot-reconcile --install`
[Unit]
Description=BUO boot reconcile (BC-250: 16 thread, governor GPU, tabelle ACPI)
Documentation=man:buo(1)
After=bc250-cu-live-manager.service
Before=graphical.target
Wants=bc250-cu-live-manager.service

[Service]
Type=oneshot
# La cwd di un'unità systemd è "/" (read-only su ostree): servono directory
# scrivibili per i tool che usano la cwd (stress, OC).
WorkingDirectory=/tmp
# --boot: la sessione (Steam) non è ancora avviata → basta il gate sui giochi
ExecStart={python} -m buo boot-reconcile --boot
RemainAfterExit=yes
# Il reboot per attivare l'unlock è deciso dall'agente (tetto tentativi +
# gate gioco): un fallimento dell'agente NON deve bloccare il boot.
SuccessExitStatus=0 1 SIGTERM

[Install]
WantedBy=graphical.target
"""


def install_unit(python: Optional[str] = None,
                 unit_path: Path = UNIT_PATH) -> Dict[str, Any]:
    """Scrive e abilita l'unità dell'agente (idempotente, serve root)."""
    py = python or sys.executable
    try:
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(unit_content(py), encoding="utf-8")
    except OSError as e:
        return {"installed": False, "error": f"{e} (serve root?)"}
    rc, _, err = run_command(["systemctl", "daemon-reload"], timeout=60,
                             sudo=os.geteuid() != 0)
    if rc != 0:
        return {"installed": False, "error": err or "daemon-reload fallito"}
    rc, _, err = run_command(["systemctl", "enable", BOOT_UNIT], timeout=60,
                             sudo=os.geteuid() != 0)
    if rc != 0:
        return {"installed": False, "error": err or "enable fallito"}
    return {"installed": True, "unit": str(unit_path), "python": py}


def uninstall_unit(unit_path: Path = UNIT_PATH) -> Dict[str, Any]:
    """Disabilita e rimuove l'unità dell'agente."""
    sudo = os.geteuid() != 0
    run_command(["systemctl", "disable", "--now", BOOT_UNIT], timeout=60,
                sudo=sudo)
    try:
        unit_path.unlink(missing_ok=True)
    except OSError as e:
        return {"removed": False, "error": str(e)}
    rc, _, err = run_command(["systemctl", "daemon-reload"], timeout=60,
                             sudo=sudo)
    return {"removed": rc == 0, "error": None if rc == 0 else (err or "")}
