#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Rollback Manager — rollback a cascata.

Ogni modifica fatta da BUO è reversibile. Il rollback avviene in ordine
INVERSO rispetto all'applicazione (dal più recente al più vecchio), così
che ogni livello ripristini le precondizioni del precedente.

Ordine (dalla chat, design finale — 12 livelli):
    cpu_overclock, gpu_governor, gpu_40cu, gpu_mask, cpu_core_unlock,
    acpi_fix, tlb_fix, ace_fix, iommu, vram_config, gtt_tuning, fan_control

Se un passo fallisce: log e si continua con i successivi. Se tutti
falliscono: avviso di recupero manuale.
"""

from typing import Callable, Dict, Optional

from ..constants import ROLLBACK_ORDER
from ..exceptions import RollbackError
from ..utils.logging import LoggerMixin


class RollbackManager(LoggerMixin):
    """Esegue il rollback a cascata di tutte le modifiche BUO."""

    def __init__(self, mock: bool = False, hardware=None):
        self.mock = mock
        self.hardware = hardware

        # Handlers: ogni livello sa come annullare la propria modifica.
        # Vengono iniettati dall'orchestratore (wrapper/moduli reali o mock).
        self._handlers: Dict[str, Callable[[], bool]] = {}

    def register(self, name: str, handler: Callable[[], bool]) -> None:
        """Registra il rollback di un livello (solo nomi noti)."""
        if name in ROLLBACK_ORDER:
            self._handlers[name] = handler

    # ------------------------------------------------------------------ #

    def rollback(self, from_phase: Optional[str] = None,
                 reason: str = "") -> bool:
        """
        Esegue il rollback a cascata.

        Args:
            from_phase: nome del livello da cui partire (incluso); se None,
                        fa rollback di TUTTO.
            reason: motivo del rollback (per il log).

        Returns:
            True se tutti i passi richiesti sono riusciti.
        """
        self.logger.warning("🔄 Rollback avviato (motivo: %s)", reason or "generico")

        # Determina il sottoinsieme dell'ordine da eseguire
        to_rollback = list(ROLLBACK_ORDER)
        if from_phase and from_phase in ROLLBACK_ORDER:
            idx = ROLLBACK_ORDER.index(from_phase)
            to_rollback = ROLLBACK_ORDER[:idx + 1]

        success = True
        executed = 0

        for level in to_rollback:
            handler = self._handlers.get(level)
            if handler is None:
                self.logger.debug("Nessun handler per %s — salto", level)
                continue
            executed += 1
            try:
                if handler():
                    self.logger.info("   ✅ Rollback completato: %s", level)
                else:
                    self.logger.error("   ❌ Rollback fallito: %s", level)
                    success = False
            except Exception as e:
                self.logger.error("   ❌ Rollback in errore per %s: %s", level, e)
                success = False

        if executed == 0:
            self.logger.info("Nessun livello di rollback registrato — nulla da fare")

        if success:
            self.logger.info("✅ Rollback a cascata completato")
        else:
            self.logger.error(
                "⚠️ Alcuni livelli di rollback sono falliti. "
                "Consulta /var/log/buo/buo.log e la guida di recupero manuale."
            )
        return success

    def registered_levels(self) -> list:
        return [l for l in ROLLBACK_ORDER if l in self._handlers]
