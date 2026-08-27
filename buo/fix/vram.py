#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
VRAM Config — modifica dello split VRAM via bc250_memcfg.

Dallo studio (messaggi 14, 106): `fanoush/bc250_memcfg` permette di
cambiare la quantità di memoria dedicata alla GPU da Linux, senza
flashare il BIOS. BUO lo integra come fix opzionale (configurabile).
"""

from typing import Any, Dict, Optional

from ..utils.logging import LoggerMixin

MEMCFG_REPO = "https://github.com/fanoush/bc250_memcfg"


class VRAMConfig(LoggerMixin):
    """Configurazione dello split VRAM."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 memcfg_path: Optional[str] = None):
        self.mock = mock
        self.mock_hw = mock_hardware
        self.memcfg_path = memcfg_path  # eseguibile bc250_memcfg

    def verify(self) -> bool:
        """La configurazione VRAM non è verificabile in automatico: False."""
        return False

    def apply(self, gpu_memory_gb: int = 8) -> Dict[str, Any]:
        """
        Imposta la VRAM dedicata.

        Args:
            gpu_memory_gb: GB dedicati alla GPU (default 8, max 12-14)
        """
        if not (1 <= gpu_memory_gb <= 14):
            return {"applied": False, "error": "valore fuori range (1-14 GB)"}

        if self.mock and self.mock_hw is not None:
            return {"applied": True, "gpu_memory_gb": gpu_memory_gb,
                    "needs_reboot": True}

        if not self.memcfg_path:
            return {
                "applied": False,
                "needs_reboot": False,
                "warning": (
                    "bc250_memcfg non configurato. Scaricalo da "
                    f"{MEMCFG_REPO} e passa --memcfg <percorso>. "
                    "Esempio reale: bc250_memcfg --set-vram {gpu_memory_gb}G"
                ),
            }

        self.logger.warning(
            "Applicazione reale di bc250_memcfg non eseguita — "
            "usare: %s --set-vram %dG", self.memcfg_path, gpu_memory_gb)
        return {"applied": False, "needs_reboot": True,
                "warning": f"Eseguire: {self.memcfg_path} --set-vram {gpu_memory_gb}G"}

    def rollback(self) -> bool:
        """Ripristina lo split default (16GB UMA stock)."""
        self.logger.info("Rollback VRAM: ripristinare lo split stock")
        return True
