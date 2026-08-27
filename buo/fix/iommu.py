#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
IOMMU — nessuna modifica a livello kernel (fix BIOS manuale).

⚠️ CORREZIONE DA EVIDENZA DI CAMPO (docs/BUGS.md #2):
    la community (elektricM/amd-bc250-docs) consiglia di DISABILITARE
    L'IOMMU NEL BIOS per curare crash/black-screen della GPU
    ("IOMMU is broken - causes display failures and crashes").

    NOI avevamo applicato `iommu=off` come PARAMETRO KERNEL, che è un
    metodo DIVERSO e SBAGLIATO su questa scheda: l'IOMMU viene
    inizializzato dal BIOS e poi spento dal kernel, rompendo la interrupt
    remapping → USB e scheda di rete morte (partial hang: schermo con
    animazioni ma input/rete/console morti).

    Distinzione fondamentale:
      • disabilitare nel BIOS  → cura i crash GPU, NON tocca USB/rete
      • iommu=off kernel param → rompe USB/rete (NON va mai usato)

    BUO quindi NON tocca l'IOMMU a livello kernel. Se in futuro
    comparissero crash/black-screen GPU, il rimedio corretto è il toggle
    BIOS manuale (Advanced → AMD CBS → NBIO → IOMMU → Disabled).
    Questo fix è un no-op: verifica che l'IOMMU sia attivo a livello
    kernel e segnala se `iommu=off` è indebitamente presente.
"""

from typing import Any, Dict

from ..utils.logging import LoggerMixin


class IOMMUFix(LoggerMixin):
    """Verifica che l'IOMMU sia attivo; non applica alcuna modifica."""

    def __init__(self, mock: bool = False, mock_hardware=None):
        self.mock = mock
        self.mock_hw = mock_hardware

    # ------------------------------------------------------------------ #

    def verify(self) -> bool:
        """True se l'IOMMU è nello stato corretto: ATTIVO (iommu=off assente)."""
        if self.mock and self.mock_hw is not None:
            return not self.mock_hw.state.iommu_off
        try:
            with open("/proc/cmdline") as f:
                cmd = f.read()
            return "iommu=off" not in cmd and "iommu=pt" not in cmd
        except Exception:
            return False

    def apply(self) -> Dict[str, Any]:
        """Nessuna modifica: l'IOMMU va lasciato attivo (docs/BUGS.md #2)."""
        if self.mock and self.mock_hw is not None:
            return {
                "applied": False,
                "needs_reboot": False,
                "warning": (
                    "IOMMU: nessuna modifica (mock). La fix per i crash GPU "
                    "è il BIOS, non iommu=off (docs/BUGS.md #2)."
                ),
            }
        return {
            "applied": False,
            "needs_reboot": False,
            "warning": (
                "IOMMU: nessuna modifica a livello kernel. La community "
                "consiglia di disabilitare l'IOMMU NEL BIOS per i crash GPU "
                "(elektricM/amd-bc250-docs), ma il parametro kernel "
                "`iommu=off` rompe USB e rete su BC-250 (docs/BUGS.md #2): "
                "NON va usato. Se `iommu=off` è già presente, rimuovilo: "
                "sudo rpm-ostree kargs --delete=iommu=off && reboot. "
                "Per i crash GPU usa il toggle BIOS manuale "
                "(Advanced → AMD CBS → NBIO → IOMMU → Disabled)."
            ),
        }

    def rollback(self) -> bool:
        """Nessuna modifica da ripristinare."""
        return True
