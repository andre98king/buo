#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
GPU 40-CU Unlock — via bc250-enable-40cu.sh (patch amdgpu di duggasco).

Analisi dallo studio:
    • registri: mmCC_GC_SHADER_ARRAY_CONFIG e
      mmSPI_PG_ENABLE_STATIC_WGP_MASK (entrambi necessari)
    • bc250_cc_write_mode=3 (clear tutti i SE/SH) è la modalità consigliata
    • persistente dopo reboot (modulo installato + modprobe conf)
    • rischio: chip B-grade con CU difettose → serve il health test
"""

from typing import Any, Dict, Optional

from ..utils.logging import LoggerMixin
from .wrappers.bc250_40cu import BC25040CUWrapper


class GPU40CUUnlock(LoggerMixin):
    """Sblocco delle 40 CU GPU."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 use_wrapper: bool = True):
        self.mock = mock
        self.mock_hw = mock_hardware
        self.wrapper = BC25040CUWrapper() if use_wrapper and not mock else None

    def is_enabled(self) -> bool:
        """True se le 40 CU sono già attive."""
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.get_cu_count() >= 40
        if self.wrapper is not None and self.wrapper.available:
            st = self.wrapper.status().get("parsed_output", {})
            return bool(st.get("full_die", False))
        return False

    def apply(self) -> Dict[str, Any]:
        """Esegue build + enable del modulo patchato."""
        if self.mock and self.mock_hw is not None:
            ok = self.mock_hw.enable_40cu()
            return {
                "applied": ok,
                "cu_count": self.mock_hw.get_cu_count(),
                "needs_reboot": True,
            }

        if self.wrapper is None or not self.wrapper.available:
            return {"applied": False, "error": "bc250-enable-40cu.sh non trovato"}

        self.logger.info("Build del modulo amdgpu patchato...")
        build = self.wrapper.build()
        if build["returncode"] != 0:
            return {"applied": False, "error": build["stderr"] or "build fallita"}

        self.logger.info("Enable 40-CU...")
        enable = self.wrapper.enable()
        if enable["returncode"] != 0:
            return {"applied": False, "error": enable["stderr"] or "enable fallito"}

        return {"applied": True, "cu_count": 40, "needs_reboot": True}

    def rollback(self) -> bool:
        """Torna a 24 CU (restore del modulo originale)."""
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.disable_40cu()
        if self.wrapper is not None and self.wrapper.available:
            result = self.wrapper.restore()
            return result["returncode"] == 0
        return False
