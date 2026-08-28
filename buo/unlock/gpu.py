#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
GPU 40-CU Unlock — metodo per distro (kernel patch o runtime UMR).

METODO CORRETTO PER DISTRO (docs/COMMUNITY_NOTES.md §2b):
    • NON-ostree (Fedora/Arch standard): kernel patch amdgpu via
      `bc250-enable-40cu.sh` (build + enable, richiede reboot).
    • OSTREE (Bazzite/SteamOS): /usr è READ-ONLY → il kernel patch NON
      funziona (build fallisce scrivendo amdgpu_trace.h). Si usa il
      **runtime UMR** via `bc250-cu-live-manager.sh` (scrive CC/SPI/RLC
      da userspace, VOLATILE, nessun reboot, reversibile).

Analisi dallo studio:
    • registri: mmCC_GC_SHADER_ARRAY_CONFIG e
      mmSPI_PG_ENABLE_STATIC_WGP_MASK (entrambi necessari)
    • bc250_cc_write_mode=3 (clear tutti i SE/SH) è la modalità consigliata
    • rischio: chip B-grade con CU difettose → serve il health test
"""

import os
from typing import Any, Dict, Optional

from ..utils.logging import LoggerMixin
from .wrappers.bc250_40cu import BC25040CUWrapper
from .wrappers.bc250_live_manager import BC250LiveManagerWrapper


class GPU40CUUnlock(LoggerMixin):
    """Sblocco delle 40 CU GPU (metodo per distro)."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 use_wrapper: bool = True):
        self.mock = mock
        self.mock_hw = mock_hardware
        self.is_ostree = os.path.exists("/run/ostree-booted")
        if use_wrapper and not mock:
            if self.is_ostree:
                # Runtime UMR (unico metodo funzionante su ostree)
                self.wrapper = BC250LiveManagerWrapper()
            else:
                # Kernel patch (Fedora/Arch standard)
                self.wrapper = BC25040CUWrapper()
        else:
            self.wrapper = None

    # ------------------------------------------------------------------ #

    def is_enabled(self) -> bool:
        """True se le 40 CU sono già attive (routed)."""
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.get_cu_count() >= 40
        if self.wrapper is not None and self.wrapper.available:
            st = self.wrapper.status().get("parsed_output", {})
            return bool(st.get("full_die", False))
        return False

    def apply(self) -> Dict[str, Any]:
        """Abilita le 40 CU col metodo corretto per la distro."""
        if self.mock and self.mock_hw is not None:
            ok = self.mock_hw.enable_40cu()
            return {
                "applied": ok,
                "cu_count": self.mock_hw.get_cu_count(),
                "needs_reboot": True,
            }

        if self.wrapper is None or not self.wrapper.available:
            return {
                "applied": False,
                "error": "script 40-CU non trovato — esegui: sudo buo install-deps",
            }

        if self.is_ostree:
            return self._apply_runtime_umr()

        return self._apply_kernel_patch()

    def _apply_runtime_umr(self) -> Dict[str, Any]:
        """Ostree: runtime UMR, volatile, nessun reboot, reversibile."""
        self.logger.info("40-CU via runtime UMR (ostree) — enable all...")
        result = self.wrapper.enable_all()
        parsed = result.get("parsed_output", {})
        if result["returncode"] != 0:
            return {
                "applied": False,
                "error": result.get("stderr") or "enable all fallito",
            }
        ok = bool(parsed.get("full_die", False))
        return {
            "applied": ok,
            "cu_count": parsed.get("cu_routed", 40) if ok else 24,
            "needs_reboot": False,  # volatile, nessun reboot
            "method": "runtime_umr",
            "warning": (
                "40 CU attive (volatili, runtime UMR). Per la persistenza "
                "al boot: eseguire la persistenza manuale (install-service + "
                "write-service-table), validata sul campo."
            ),
        }

    def _apply_kernel_patch(self) -> Dict[str, Any]:
        """Non-ostree: build + enable del modulo amdgpu patchato."""
        self.logger.info("Build del modulo amdgpu patchato...")
        build = self.wrapper.build()
        if build["returncode"] != 0:
            return {"applied": False, "error": build["stderr"] or "build fallita"}

        self.logger.info("Enable 40-CU...")
        enable = self.wrapper.enable()
        if enable["returncode"] != 0:
            return {"applied": False, "error": enable["stderr"] or "enable fallito"}

        return {"applied": True, "cu_count": 40, "needs_reboot": True}

    def persist(self) -> Dict[str, Any]:
        """Persistenza 40 CU al boot (SOLO ostree/runtime UMR, opt-in).

        Validata sul campo (28/08/2026): install-service + tabella salvata,
        stabile al reboot. Richiede un reboot per la verifica.
        """
        if not self.is_ostree:
            return {
                "persisted": False,
                "error": "persistenza runtime UMR solo su ostree",
            }
        if self.wrapper is None or not self.wrapper.available:
            return {"persisted": False, "error": "live-manager non installato"}
        svc = self.wrapper.install_service()
        if svc["returncode"] != 0:
            return {"persisted": False, "error": svc["stderr"] or "install-service fallito"}
        tbl = self.wrapper.write_service_table()
        if tbl["returncode"] != 0:
            return {"persisted": False, "error": tbl["stderr"] or "write-service-table fallito"}
        return {
            "persisted": True,
            "note": "40 CU persistite al boot (richiede reboot per l'attivazione)",
        }

    def rollback(self) -> bool:
        """Torna a 24 CU (metodo per distro)."""
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.disable_40cu()
        if self.wrapper is not None and self.wrapper.available:
            if self.is_ostree:
                # Runtime UMR: stock-dispatch, nessun reboot
                result = self.wrapper.stock_dispatch()
                return result["returncode"] == 0
            result = self.wrapper.restore()
            return result["returncode"] == 0
        return False
