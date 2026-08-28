#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Checkpoint Manager — salvataggio e ripristino dello stato.

Salva lo stato su /var/lib/buo/state.json PRIMA di ogni modifica, con
backup automatico del file precedente. Permette di riprendere dopo un
reboot inaspettato e di tornare a una fase precedente.
"""

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..constants import STATE_DIR
from ..exceptions import CheckpointError
from ..utils.logging import LoggerMixin
from ..utils.paths import state_dir as _default_state_dir


class CheckpointManager(LoggerMixin):
    """Gestisce il salvataggio e ripristino dei checkpoint."""

    def __init__(self, state_dir: Optional[Path] = None):
        self.state_dir = Path(state_dir) if state_dir else _default_state_dir()
        self.state_file = self.state_dir / "state.json"
        self.backup_dir = self.state_dir / "backups"

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        self._state: Dict[str, Any] = {}
        self._load()

    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("lo stato non è un oggetto JSON")
                self._state = data
                self.logger.debug("Stato caricato da %s", self.state_file)
            except Exception as e:
                # Fail-closed (A2): stato corrotto → NIENTE reset silenzioso.
                # Il file resta intatto e si blocca con intervento manuale:
                # un reset qui renderebbe il rollback fail-open (si
                # perderebbe il ledger applied_steps → hardware modificato
                # senza possibilità di annullamento).
                raise CheckpointError(
                    f"Checkpoint corrotto ({self.state_file}): {e}. "
                    f"Ripristinare da {self.backup_dir} oppure rimuovere "
                    f"il file SOLO accettando di perdere lo stato salvato."
                ) from e
        else:
            self._state = self._empty_state()

    @staticmethod
    def _empty_state() -> Dict[str, Any]:
        return {
            "version": "1.0.0",
            "current_phase": "init",
            "phases": {},
            "reboot_count": 0,
            "last_reboot": None,
            "hardware": {},
            "config": {},
        }

    def save(self) -> None:
        """Salva lo stato in modo ATOMICO (temp + fsync + replace).

        Il backup del precedente ha timestamp con microsecondi (evita
        sovrascritture nello stesso secondo).
        """
        try:
            if self.state_file.exists():
                backup_path = self.backup_dir / (
                    f"state_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
                )
                shutil.copy2(self.state_file, backup_path)
            tmp = self.state_file.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_file)
            self.logger.debug("Stato salvato in %s", self.state_file)
        except Exception as e:
            raise CheckpointError(f"Errore salvataggio checkpoint: {e}")

    # ------------------------------------------------------------------ #

    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._state[key] = value
        self.save()

    def set_phase(self, phase: str, data: Dict[str, Any],
                  completed: bool = False) -> None:
        self._state.setdefault("phases", {})[phase] = {
            "completed": completed,
            "timestamp": datetime.now().isoformat(),
            "data": data,
        }
        self._state["current_phase"] = phase
        self.save()

    def seed_phase(self, phase: str, data: Dict[str, Any]) -> None:
        """Inietta i dati di una fase SENZA salvare su disco (G2 restore).

        Usato da `buo restore` per riapplicare il profilo salvato senza
        toccare lo stato persistente (e senza violare il dry-run).
        """
        self._state.setdefault("phases", {})[phase] = {
            "completed": True,
            "timestamp": datetime.now().isoformat(),
            "data": data,
        }

    def get_phase(self, phase: str) -> Dict[str, Any]:
        return self._state.get("phases", {}).get(phase, {})

    def is_phase_completed(self, phase: str) -> bool:
        return bool(self.get_phase(phase).get("completed", False))

    def get_current_phase(self) -> str:
        return self._state.get("current_phase", "init")

    def set_current_phase(self, phase: str) -> None:
        self._state["current_phase"] = phase
        self.save()

    def get_reboot_count(self) -> int:
        return int(self._state.get("reboot_count", 0))

    def increment_reboot_count(self) -> None:
        self._state["reboot_count"] = self.get_reboot_count() + 1
        self._state["last_reboot"] = datetime.now().isoformat()
        self.save()

    def clear(self) -> None:
        self._state = self._empty_state()
        self.save()

    def rollback_to_phase(self, phase: str) -> None:
        """Rimuove tutte le fasi successive a `phase` e vi torna."""
        phases = list(self._state.get("phases", {}).keys())
        if phase in phases:
            idx = phases.index(phase)
            for p in phases[idx + 1:]:
                del self._state["phases"][p]
        self._state["current_phase"] = phase
        self.save()
        self.logger.info("Rollback alla fase: %s", phase)

    def full_state(self) -> Dict[str, Any]:
        """Copia dello stato completo (per report/rollback)."""
        return json.loads(json.dumps(self._state))
