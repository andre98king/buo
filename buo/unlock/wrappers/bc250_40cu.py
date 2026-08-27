#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Wrapper per bc250-enable-40cu.sh — sblocco GPU 24→40 CU.

Analisi dal codice sorgente (confermata nello studio):
    • modalità: build | enable | disable | status | restore
    • patch scrive mmCC_GC_SHADER_ARRAY_CONFIG e
      mmSPI_PG_ENABLE_STATIC_WGP_MASK (nessuno dei due da solo basta)
    • bc250_cc_write_mode=3 è la modalità consigliata
    • backup automatico del modulo: amdgpu.ko.bc250-backup-YYYYMMDD
    • persistente (modulo installato + /etc/modprobe.d/bc250-40cu.conf)
"""

import re
from typing import Any, Dict

from .base import BaseWrapper


class BC25040CUWrapper(BaseWrapper):
    """Wrapper per lo script di unlock 40-CU."""

    def __init__(self, script_path: str = "/usr/local/bin/bc250-enable-40cu.sh"):
        super().__init__(script_path, timeout=120)

    def build(self) -> Dict[str, Any]:
        """Compila e installa il modulo amdgpu patchato."""
        return self.run_with_output(["build"], timeout=600)

    def enable(self) -> Dict[str, Any]:
        """Abilita le 40 CU (bc250_cc_write_mode=3) e riavvia."""
        return self.run_with_output(["enable"])

    def disable(self) -> Dict[str, Any]:
        """Torna a 24 CU stock e riavvia."""
        return self.run_with_output(["disable"])

    def restore(self) -> Dict[str, Any]:
        """Ripristina il modulo amdgpu originale (rollback)."""
        return self.run_with_output(["restore"], timeout=120)

    def status(self) -> Dict[str, Any]:
        """Legge lo stato corrente (senza fallire se non patchato)."""
        return self.run_with_output(["status"], check_returncode=False)

    def parse_output(self, stdout: str, stderr: str) -> Dict[str, Any]:
        parsed: Dict[str, Any] = {
            "detected": False,
            "patched": False,
            "write_mode": None,
            "active_cus": 24,
            "full_die": False,
        }
        if "BC-250 detected" in stdout:
            parsed["detected"] = True
        if "amdgpu module: patched" in stdout:
            parsed["patched"] = True
        m = re.search(r"write_mode:\s*(\d+)", stdout)
        if m:
            parsed["write_mode"] = int(m.group(1))
        m = re.search(r"active CUs:\s*(\d+)", stdout)
        if m:
            parsed["active_cus"] = int(m.group(1))
        if "full die" in stdout:
            parsed["full_die"] = True
        return parsed
