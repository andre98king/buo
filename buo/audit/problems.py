#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Problem Detection — identifica automaticamente tutti i problemi noti
della BC-250 (dall'analisi approfondita della chat, messaggi 90-94):

    • kernel < 6.11 o versioni con regressioni note
    • Mesa < 25.1
    • IOMMU disattivato (iommu=off rompe USB/rete — docs/BUGS.md #2)
    • tabelle ACPI C-State mancanti
    • governor non installato/attivo
    • modulo amdgpu non patchato (40-CU)
    • TLB fault non patchato
    • compute queue (ACE) non fixata
    • GPU frequency range limitato
    • GTT limitato a ~7.4 GiB
    • sensori SuperIO (NCT6686) non attivi
"""

from typing import Any, Dict, List

from ..utils.logging import LoggerMixin


class ProblemDetector(LoggerMixin):
    """Rileva i problemi noti a partire dall'audit hardware."""

    # Problemi noti con id stabile (usato anche per fix e report)
    KNOWN_PROBLEMS = [
        "kernel_old",
        "kernel_regression",
        "mesa_old",
        "iommu_disabled",
        "acpi_cst_missing",
        "governor_missing",
        "amdgpu_not_patched",
        "tlb_fault",
        "ace_compute_broken",
        "gpu_freq_limited",
        "gtt_limited",
        "superio_missing",
    ]

    def __init__(self, mock: bool = False, mock_hardware=None):
        self.mock = mock
        self.mock_hw = mock_hardware

    # ------------------------------------------------------------------ #

    def detect(self, audit: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Analizza l'audit e restituisce la lista dei problemi trovati."""
        problems: List[Dict[str, Any]] = []

        kernel = audit.get("kernel", {})
        if not kernel.get("meets_minimum", True):
            problems.append({
                "id": "kernel_old",
                "severity": "alta",
                "title": "Kernel troppo vecchio",
                "detail": (f"Kernel {kernel.get('release')} < 6.11 — "
                           "la BC-250 richiede kernel ≥ 6.11"),
                "fix": "kernel_upgrade",
            })
        release = kernel.get("release", "")
        if "6.15" in release:
            problems.append({
                "id": "kernel_regression",
                "severity": "alta",
                "title": "Kernel 6.15-rc1 con regressione nota",
                "detail": "Broadcast TLB invalidation rompe il dispositivo",
                "fix": "kernel_upgrade",
            })

        mesa = audit.get("mesa", {})
        # Solo se la versione è LEGGIBILE: in sessione headless (SSH senza
        # display) glxinfo fallisce e version=None → non è "vecchia", è
        # solo indecifrabile (bug #13). Allineato al preflight, che già
        # usa `mesa.get("version")`.
        if mesa.get("version") and not mesa.get("meets_minimum", True):
            problems.append({
                "id": "mesa_old",
                "severity": "alta",
                "title": "Mesa troppo vecchia",
                "detail": f"Mesa {mesa.get('version')} < 25.1 — richiesta ≥ 25.1",
                "fix": "mesa_upgrade",
            })

        iommu = audit.get("iommu", {})
        if not iommu.get("enabled", True):
            problems.append({
                "id": "iommu_disabled",
                "severity": "alta",
                "title": "iommu=off nel kernel — rompe USB/rete su BC-250",
                "detail": "Rimuovere il parametro kernel iommu=off "
                          "(rpm-ostree kargs --delete=iommu=off). "
                          "La fix per i crash GPU è il BIOS, non il kernel "
                          "(docs/BUGS.md #2).",
                "fix": "iommu",
            })

        acpi = audit.get("acpi", {})
        if not acpi.get("cst_present", True):
            problems.append({
                "id": "acpi_cst_missing",
                "severity": "media",
                "title": "C-State ACPI mancanti",
                "detail": "Installare SSDT-CST (risparmio energetico in idle)",
                "fix": "acpi",
            })

        governor = audit.get("governor", {})
        if not governor.get("active", True):
            problems.append({
                "id": "governor_missing",
                "severity": "media",
                "title": "Governor GPU non attivo",
                "detail": "Installare cyan-skillfish-governor-smu",
                "fix": "governor",
            })

        amdgpu = audit.get("amdgpu", {})
        if not amdgpu.get("patched_for_40cu", True):
            problems.append({
                "id": "amdgpu_not_patched",
                "severity": "media",
                "title": "Modulo amdgpu non patchato (40 CU bloccate)",
                "detail": "Patch di duggasco non applicata",
                "fix": "gpu_40cu",
            })

        gpu = audit.get("gpu", {})
        if gpu.get("cu_count") is not None and gpu.get("cu_count") < 40:
            problems.append({
                "id": "gpu_cu_limited",
                "severity": "media",
                "title": f"Solo {gpu.get('cu_count')}/40 CU attive",
                "detail": "Applicare la patch 40-CU e il health test",
                "fix": "gpu_40cu",
            })

        # Problemi sempre presenti su una BC-250 stock (dallo studio)
        problems.append({
            "id": "tlb_fault",
            "severity": "alta",
            "title": "TLB fault non patchato",
            "detail": "Carichi compute pesanti (AI/ML) causano GPU fault e crash",
            "fix": "tlb",
        })
        problems.append({
            "id": "ace_compute_broken",
            "severity": "alta",
            "title": "Compute queue (ACE) rotta",
            "detail": "Async compute corrompe i frame — serve bc250-gfx1013-fix",
            "fix": "ace",
        })
        problems.append({
            "id": "gtt_limited",
            "severity": "media",
            "title": "GTT limitato a ~7.4 GiB",
            "detail": "Aumentare ttm.pages_limit per usare più memoria",
            "fix": "gtt",
        })
        problems.append({
            "id": "superio_missing",
            "severity": "bassa",
            "title": "Sensori SuperIO (NCT6686) non attivi",
            "detail": "modprobe nct6683 force=true (o driver nct6687)",
            "fix": "fan",
        })

        if self.mock and self.mock_hw is not None:
            problems = self._mock_filter(problems)

        return problems

    def _mock_filter(self, problems: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """In modalità mock, rimuove i problemi già risolti nello stato."""
        hw = self.mock_hw
        out = []
        for p in problems:
            pid = p["id"]
            if pid == "iommu_disabled" and not hw.state.iommu_off:
                continue
            if pid == "acpi_cst_missing" and hw.state.is_acpi_fixed:
                continue
            if pid == "tlb_fault" and hw.state.is_tlb_fixed:
                continue
            if pid == "ace_compute_broken" and hw.state.is_ace_fixed:
                continue
            if pid in ("amdgpu_not_patched", "gpu_cu_limited") \
                    and hw.state.is_40cu_enabled:
                continue
            out.append(p)
        return out

    def summary(self, problems: List[Dict[str, Any]]) -> str:
        """Riepilogo leggibile dei problemi trovati."""
        if not problems:
            return "✅ Nessun problema noto rilevato"
        lines = [f"⚠️ {len(problems)} problemi rilevati:"]
        for p in problems:
            lines.append(f"  • [{p['severity'].upper()}] {p['title']} — {p['detail']}")
        return "\n".join(lines)
