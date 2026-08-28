#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Recovery Manager — ripresa dopo crash o reboot inaspettato.

`buo recover`:
  1. legge l'ultimo checkpoint (/var/lib/buo/state.json)
  2. identifica la fase interrotta
  3. verifica lo stato reale del sistema (via probe)
  4. se possibile riprende dalla fase interrotta, altrimenti avvia rollback
"""

from typing import Any, Dict, Optional

from ..utils.logging import LoggerMixin
from .checkpoint import CheckpointManager


class RecoveryManager(LoggerMixin):
    """Riprende l'orchestrazione dopo un'interruzione."""

    def __init__(self, checkpoint: Optional[CheckpointManager] = None,
                 verify_callback=None):
        """
        Args:
            checkpoint: gestore dello stato
            verify_callback: callable(phase) -> bool — verifica se la fase
                             è realmente completata sul sistema (probe)
        """
        self.checkpoint = checkpoint or CheckpointManager()
        self.verify_callback = verify_callback

    def get_recovery_plan(self) -> Dict[str, Any]:
        """Analizza lo stato e restituisce il piano di ripresa."""
        state = self.checkpoint.full_state()
        current = state.get("current_phase", "init")
        reboot_count = state.get("reboot_count", 0)
        phases = state.get("phases", {})

        # Fase interrotta: la corrente non è completata
        interrupted = current
        if phases.get(current, {}).get("completed", False):
            # tutte completate fino a qui? trova la prima non completata
            for name, data in phases.items():
                if not data.get("completed", False):
                    interrupted = name
                    break

        # Verifica dello stato reale (se callback disponibile)
        verified = None
        if self.verify_callback is not None:
            try:
                verified = self.verify_callback(interrupted)
            except Exception as e:
                self.logger.warning("Verifica fase %s fallita: %s", interrupted, e)
                verified = False  # fail-closed: verifica fallita -> rollback

        return {
            "current_phase": current,
            "interrupted_phase": interrupted,
            "reboot_count": reboot_count,
            "phases_completed": [n for n, d in phases.items()
                                 if d.get("completed", False)],
            "phases_pending": [n for n, d in phases.items()
                               if not d.get("completed", False)],
            "verification": verified,
            "action": (
                "resume" if verified in (None, True)
                else "rollback"
            ),
        }

    def recommend(self) -> str:
        """Testo leggibile del piano di ripresa."""
        plan = self.get_recovery_plan()
        if plan["action"] == "resume":
            return (
                f"Ripresa dalla fase '{plan['interrupted_phase']}' "
                f"(reboot eseguiti: {plan['reboot_count']})"
            )
        return (
            f"La fase '{plan['interrupted_phase']}' risulta NON verificata: "
            f"avvia il rollback per tornare a uno stato sicuro."
        )
