#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Reboot Manager — reboot automatici con ripresa.

Crea il servizio systemd buo-resume.service (che riesegue `buo resume`
al boot) e poi esegue il reboot. Il checkpoint viene salvato
dall'orchestratore PRIMA di chiamare schedule(). Alla creazione del
servizio installa anche il watch-log KDE (konsole sul log live al login),
così l'utente vede la run riprendere dopo il reboot.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from ..constants import EXIT_REBOOT
from ..utils.logging import LoggerMixin


class RebootManager(LoggerMixin):
    """Gestisce i reboot automatici con ripresa."""

    SERVICE_NAME = "buo-resume.service"
    SERVICE_PATH = Path("/etc/systemd/system") / SERVICE_NAME
    # UX watch-log: konsole sul log live al login successivo (solo reboot
    # con ripresa). Percorsi/utente come attributi di classe per i test.
    WATCH_SCRIPT = Path("/usr/local/bin/buo-watch-log.sh")
    WATCH_DESKTOP = "buo-watch.desktop"
    WATCH_LOG = Path("/var/log/buo/buo.log")
    DESKTOP_UID = 1000
    PLASMA_SESSION_FILES = (
        Path("/usr/share/wayland-sessions/plasma.desktop"),
        Path("/usr/share/xsessions/plasma.desktop"),
    )

    def __init__(self, resume_command: str = "buo resume"):
        # Fail-closed: niente newline/control char (iniezione unit systemd).
        if any(c in resume_command for c in "\r\n\x00"):
            raise ValueError("resume_command non valido (newline non ammesso)")
        self.resume_command = resume_command

    def schedule(self, reason: str = "reboot required",
                 delay: int = 5) -> None:
        """Crea il servizio di ripresa e riavvia (exit code 50)."""
        self.logger.info("♻️ Reboot programmato: %s", reason)
        self._create_resume_service()

        self.logger.info("🔄 Riavvio in %d secondi... (Ctrl+C per annullare)",
                         delay)
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            self.logger.info("Reboot annullato dall'utente")
            self.cleanup()
            return

        # reboot effettivo se possibile, altrimenti exit code dedicato
        rc, _, _ = self._run(["systemctl", "reboot"], check=False)
        if rc != 0:
            self.logger.warning("systemctl reboot non disponibile — "
                                "chiusura con exit code %d", EXIT_REBOOT)
        sys.exit(EXIT_REBOOT)

    def _create_resume_service(self) -> bool:
        """Scrive e abilita buo-resume.service."""
        content = f"""# BUO Resume Service — generato automaticamente
[Unit]
Description=BUO Resume Service (BC-250 Ultimate Orchestrator)
After=multi-user.target

[Service]
Type=oneshot
# La cwd di un servizio systemd è "/" (read-only su ostree):
# gli script che scrivono file relativi (bc250-detect → overclock.conf)
# devono girare da una directory scrivibile.
WorkingDirectory=/tmp
ExecStart=/usr/local/bin/{self.resume_command}
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""
        try:
            self.SERVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.SERVICE_PATH.write_text(content, encoding="utf-8")
            self._run(["systemctl", "enable", self.SERVICE_NAME])
        except Exception as e:
            self.logger.error("Creazione servizio di ripresa fallita: %s", e)
            return False
        self.logger.info("Servizio di ripresa creato: %s", self.SERVICE_NAME)
        # UX: al login la konsole si riapre da sola sul log della run
        # (no-op senza utente desktop KDE; mai bloccante per il reboot).
        self._install_watch_log()
        return True

    def _install_watch_log(self) -> None:
        """Installa lo script + autostart KDE per il watch del log.

        Idempotente (sovrascrive gli stessi file). Best effort: un errore
        qui non deve mai bloccare la schedulazione del reboot.
        """
        try:
            home = self._desktop_user_home()
            if not home:
                self.logger.info("Watch-log: nessun utente desktop "
                                 "(uid %d) — skip", self.DESKTOP_UID)
                return
            if not any(p.exists() for p in self.PLASMA_SESSION_FILES):
                self.logger.info("Watch-log: nessuna sessione KDE — skip")
                return
            self.WATCH_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
            self.WATCH_SCRIPT.write_text(self._watch_script(),
                                         encoding="utf-8")
            os.chmod(self.WATCH_SCRIPT, 0o755)
            autostart = Path(home) / ".config" / "autostart"
            autostart.mkdir(parents=True, exist_ok=True)
            (autostart / self.WATCH_DESKTOP).write_text(
                self._watch_desktop(), encoding="utf-8")
            self._make_log_readable()
            self.logger.info("👁️ Watch-log installato: konsole sul log al "
                             "prossimo login di %s", home)
        except Exception as e:
            self.logger.warning("Watch-log: installazione fallita (%s) — "
                                "continuo", e)

    def _desktop_user_home(self):
        """Home dell'utente desktop (uid DESKTOP_UID) o None (headless)."""
        rc, out, _ = self._run(["getent", "passwd", str(self.DESKTOP_UID)])
        if rc != 0:
            return None
        fields = out.strip().split(":")
        if len(fields) < 7 or fields[2] != str(self.DESKTOP_UID):
            return None
        return fields[5] or None

    def _watch_script(self) -> str:
        """Script autostart: konsole sul log solo se una run è attiva."""
        return f"""#!/bin/sh
# BUO watch-log — se una run buo (resume/unleash) è attiva al login,
# riapre la konsole sul log live; altrimenti esce in silenzio.
pgrep -f "[b]uo (resume|unleash)" >/dev/null 2>&1 || exit 0
exec konsole --hold -e tail -f {self.WATCH_LOG}
"""

    def _watch_desktop(self) -> str:
        """Entry autostart KDE che lancia lo script a ogni login."""
        return f"""[Desktop Entry]
Type=Application
Name=BUO Watch Log
Exec={self.WATCH_SCRIPT}
"""

    def _make_log_readable(self) -> None:
        """tail da utente richiede log 644 e dir 755 (best effort)."""
        try:
            os.chmod(self.WATCH_LOG.parent, 0o755)
        except OSError:
            pass
        try:
            os.chmod(self.WATCH_LOG, 0o644)
        except OSError:
            pass

    def cleanup(self) -> None:
        """Rimuove il servizio di ripresa."""
        if self.SERVICE_PATH.exists():
            self._run(["systemctl", "disable", self.SERVICE_NAME])
            try:
                self.SERVICE_PATH.unlink()
            except Exception:
                pass
            self.logger.info("🧹 Servizio di ripresa rimosso")

    @staticmethod
    def _run(cmd, check: bool = False):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return r.returncode, r.stdout, r.stderr
        except Exception as e:
            return 127, "", str(e)
