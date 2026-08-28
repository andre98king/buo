#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Wrapper per bc250-cu-live-manager.sh — 40 CU via RUNTIME UMR.

METODO CORRETTO SU BAZZITE/OSTREE (docs/COMMUNITY_NOTES.md §2b):
    il kernel patch (bc250-enable-40cu.sh) NON funziona su ostree perché
    /usr è read-only (il build fallisce scrivendo amdgpu_trace.h nei
    kernel headers). Su ostree si usa il runtime UMR: scrive i registri
    CC/SPI/RLC da userspace via `umr`, senza rebuild del modulo, senza
    reboot, reversibile.

Comandi supportati (non-interattivi, -y per la conferma):
    status             dashboard read-only
    enable all         instrada tutte le 40 CU (runtime, volatile)
    stock-dispatch     ripristina le 24 CU stock (runtime)
    install-service    persistenza al boot (validata, richiede reboot)
    write-service-table  salva la tabella corrente come profilo di boot
    apply-service      applica la tabella salvata

Riferimento: github.com/WinnieLV/bc250-cu-live-manager
"""

import re
from typing import Any, Dict

from .base import BaseWrapper


class BC250LiveManagerWrapper(BaseWrapper):
    """Wrapper per lo script di runtime UMR (40 CU su ostree)."""

    def __init__(self, script_path: str = "/usr/local/bin/bc250-cu-live-manager"):
        super().__init__(script_path, timeout=60)

    # --------------------------- comandi ---------------------------- #

    def status(self) -> Dict[str, Any]:
        """Dashboard read-only dello stato CU (non fallisce)."""
        return self.run_with_output(["status"], check_returncode=False)

    def enable_all(self) -> Dict[str, Any]:
        """Instrada tutte le 40 CU (runtime, volatile, nessun reboot)."""
        return self.run_with_output(["-y", "enable", "all"])

    def stock_dispatch(self) -> Dict[str, Any]:
        """Ripristina le 24 CU stock (runtime, nessun reboot)."""
        return self.run_with_output(["-y", "stock-dispatch"])

    def install_service(self) -> Dict[str, Any]:
        """Persistenza al boot (validata sul campo; richiede reboot)."""
        return self.run_with_output(["-y", "install-service"])

    def write_service_table(self) -> Dict[str, Any]:
        """Salva la tabella WGP corrente come profilo di boot."""
        return self.run_with_output(["-y", "write-service-table"])

    def apply_service(self) -> Dict[str, Any]:
        """Applica la tabella salvata (usata dal servizio al boot)."""
        return self.run_with_output(["-y", "apply-service"])

    # --------------------------- parsing ---------------------------- #

    def parse_output(self, stdout: str, stderr: str) -> Dict[str, Any]:
        parsed: Dict[str, Any] = {
            "detected": "BC-250" in stdout,
            "cu_routed": 24,
            "cu_total": 40,
            "cu_target": None,
            "full_die": False,
            "write_mode": None,
        }
        m = re.search(r"CUs active & routed\s*:\s*(\d+)/(\d+)", stdout)
        if m:
            parsed["cu_routed"] = int(m.group(1))
            parsed["cu_total"] = int(m.group(2))
        m = re.search(r"\((\d+)/(\d+) CUs target\)", stdout)
        if m:
            parsed["cu_target"] = int(m.group(1))
            parsed["cu_total"] = int(m.group(2))
        m = re.search(r"active_cu_number\s*=\s*(\d+)", stdout)
        if m:
            parsed["active_cu_number"] = int(m.group(1))
        # full_die = tutte le CU instradate (40/40), sia via status
        # (cu_routed) sia via target di "enable all" (cu_target).
        routed_all = parsed["cu_routed"] == parsed["cu_total"]
        target_all = (
            parsed["cu_target"] is not None
            and parsed["cu_target"] == parsed["cu_total"]
        )
        parsed["full_die"] = parsed["cu_total"] >= 40 and (routed_all or target_all)
        return parsed
