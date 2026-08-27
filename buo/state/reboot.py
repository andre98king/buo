#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Reboot Manager — reboot automatici con ripresa.

Crea il servizio systemd buo-resume.service (che riesegue `buo resume`
al boot) e poi esegue il reboot. Il checkpoint viene salvato
dall'orchestratore PRIMA di chiamare schedule().
"""

import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from ..constants import EXIT_REBOOT
from ..utils.logging import LoggerMixin


class RebootManager(LoggerMixin):
    """Gestisce i reboot automatici con ripresa."""

    SERVICE_NAME = "buo-resume.service"
    SERVICE_PATH = Path("/etc/systemd/system") / SERVICE_NAME

    def __init__(self, resume_command: str = "buo resume"):
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
ExecStart=/usr/local/bin/{self.resume_command}
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""
        try:
            self.SERVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.SERVICE_PATH.write_text(content, encoding="utf-8")
            self._run(["systemctl", "enable", self.SERVICE_NAME])
            self.logger.info("Servizio di ripresa creato: %s", self.SERVICE_NAME)
            return True
        except Exception as e:
            self.logger.error("Creazione servizio di ripresa fallita: %s", e)
            return False

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
