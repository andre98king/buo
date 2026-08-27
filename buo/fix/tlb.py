#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
TLB Fix — patch del kernel per il TLB fault della GPU.

Dallo studio (messaggio 94):
    • il bug: la GPU risolve un indirizzo virtuale in memoria fisica
      sbagliata; hipFree non esegue l'invalidazione TLB
    • la patch: ricostruire la runlist su unmap per forzare
      l'invalidazione del firmware (bc250-flush-tlb-by-runlist.patch)
    • stato: disponibile (2026-08-07) ma "non definitiva" — carichi
      pesanti e preemption non testati
    • risultati empirici: "13 of 18 dirty runs become 0 of 18"
"""

from pathlib import Path
from typing import Any, Dict, Optional

from ..exceptions import SafetyViolation
from ..utils.logging import LoggerMixin

TLB_PATCH_NAME = "bc250-flush-tlb-by-runlist.patch"
TLB_PATCH_REPO = "https://github.com/elektricM/amd-bc250-docs"


class TLBKernelFix(LoggerMixin):
    """Applica la patch TLB al kernel."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 patch_path: Optional[str] = None):
        self.mock = mock
        self.mock_hw = mock_hardware
        self.patch_path = patch_path  # percorso della patch già scaricata

    # ------------------------------------------------------------------ #

    def verify(self) -> bool:
        """True se la patch risulta applicata (o mock risolto)."""
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.state.is_tlb_fixed
        # Verifica: presenza di un segno della patch nel kernel build
        # (in produzione: controllare il file .config o i sorgenti kernel)
        return False

    def apply(self) -> Dict[str, Any]:
        """
        Applica la patch TLB al kernel.

        NOTA: come per il design finale, questa è un'operazione ad alto
        rischio (patch del kernel) — in produzione richiede i sorgenti
        del kernel e il rebuild. BUO la segnala come operazione critica
        con verifica dei prerequisiti.
        """
        if self.mock and self.mock_hw is not None:
            ok = self.mock_hw.apply_tlb_fix()
            return {"applied": ok, "needs_reboot": True}

        if not self.patch_path or not Path(self.patch_path).exists():
            return {
                "applied": False,
                "needs_reboot": False,
                "warning": (
                    "Patch TLB non trovata. Scaricala da "
                    f"{TLB_PATCH_REPO} e specifica --patch-tlb <percorso>. "
                    "In produzione: applica con `patch -p1`, ricompila il "
                    "kernel e reinstallalo."
                ),
            }

        # In produzione qui: git apply / patch -p1 sul source tree del kernel
        self.logger.warning(
            "Applicazione reale della patch TLB non eseguita "
            "(richiede sorgenti kernel). Verificare manualmente."
        )
        return {
            "applied": False,
            "needs_reboot": True,
            "warning": "Applicare manualmente: patch -p1 < bc250-flush-tlb-by-runlist.patch, poi rebuild",
        }

    def rollback(self) -> bool:
        """Rimuove la patch (in produzione: revert + rebuild)."""
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.remove_tlb_fix()
        self.logger.warning("Rollback TLB: rimuovere la patch e ricompilare")
        return True
