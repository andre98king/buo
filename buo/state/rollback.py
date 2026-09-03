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
                 reason: str = "",
                 applied: Optional[set] = None) -> bool:
        """
        Esegue il rollback a cascata.

        Args:
            from_phase: nome del livello da cui partire (incluso); se None,
                        fa rollback di TUTTO.
            reason: motivo del rollback (per il log).
            applied: insieme dei livelli realmente applicati (ledger).
                     Se fornito, esegue SOLO quelli: niente rumore su
                     livelli mai toccati (es. rollback automatico dopo un
                     errore). Se None → cascata completa (rollback manuale).

        Returns:
            True se tutti i passi richiesti sono riusciti.
        """
        if applied:
            self.logger.info("🔄 Rollback dei livelli applicati (%d): %s",
                             len(applied), ", ".join(sorted(applied)))
        else:
            self.logger.warning("🔄 Rollback avviato (motivo: %s)",
                                reason or "generico")

        # Determina il sottoinsieme dell'ordine da eseguire
        to_rollback = list(ROLLBACK_ORDER)
        if from_phase and from_phase in ROLLBACK_ORDER:
            idx = ROLLBACK_ORDER.index(from_phase)
            to_rollback = ROLLBACK_ORDER[:idx + 1]
        if applied is not None:
            to_rollback = [l for l in to_rollback if l in applied]
            if not to_rollback:
                self.logger.info("Nessun livello applicato da ripristinare "
                                 "(ledger vuoto)")
                return True

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
                    # False = livello legittimamente 'non necessario'
                    # (niente da ripristinare: es. governor già fermo,
                    # file mai scritto, modulo mai caricato). NON è un
                    # fallimento dell'insieme: `buo rollback` su una
                    # macchina senza modifiche deve uscire 0. Gli errori
                    # veri emergono come eccezioni (sotto).
                    self.logger.info("   — Rollback non necessario: %s",
                                     level)
            except Exception as e:
                self.logger.warning("   ⚠️ Rollback in errore per %s: %s",
                                    level, e)
                success = False

        if executed == 0:
            self.logger.info("Nessun livello di rollback registrato — nulla da fare")

        if success:
            self.logger.info("✅ Rollback a cascata completato")
        else:
            self.logger.warning(
                "⚠️ Alcuni livelli di rollback non sono stati completati. "
                "Consulta /var/log/buo/buo.log per i dettagli."
            )
        return success

    def registered_levels(self) -> list:
        return [l for l in ROLLBACK_ORDER if l in self._handlers]
