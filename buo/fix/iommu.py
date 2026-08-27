#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
IOMMU Disable — aggiunge `iommu=off` ai parametri kernel.

Dallo studio: IOMMU instabile sulla BC-250 → crash e problemi di boot.
La disabilitazione avviene nei parametri di GRUB (o EFI equivalenti).
"""

import os
import re
from typing import Any, Dict, Optional

from ..utils.logging import LoggerMixin
from ..utils.shell import run_command


class IOMMUFix(LoggerMixin):
    """Disabilita l'IOMMU."""

    def __init__(self, mock: bool = False, mock_hardware=None):
        self.mock = mock
        self.mock_hw = mock_hardware

    # ------------------------------------------------------------------ #

    def verify(self) -> bool:
        """True se iommu=off è già presente in /proc/cmdline."""
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.state.iommu_off
        try:
            with open("/proc/cmdline") as f:
                return "iommu=off" in f.read()
        except Exception:
            return False

    def apply(self) -> Dict[str, Any]:
        """Aggiunge iommu=off ai parametri kernel (GRUB o ostree kargs)."""
        if self.mock and self.mock_hw is not None:
            ok = self.mock_hw.disable_iommu()
            return {"applied": ok, "needs_reboot": True}

        # Bazzite/SteamOS (ostree): i parametri kernel si impostano con
        # rpm-ostree kargs. ATTENZIONE (bug sul campo): se il demone
        # rpm-ostree è occupato/bloccato (es. auto-update in corso), la
        # chiamata resta appesa in D-state e non risponde ai timeout.
        # Per non bloccare il pipeline, su ostree il fix è MANUALE con
        # istruzioni precise (il reboot dell'ACPI lo riavvierà comunque).
        if os.path.exists("/run/ostree-booted"):
            return {
                "applied": False,
                "needs_reboot": False,
                "warning": (
                    "IOMMU su Bazzite si disabilita manualmente: "
                    "sudo rpm-ostree kargs --append=iommu=off && reboot. "
                    "(BUO evita rpm-ostree in automatico: il demone può "
                    "bloccarsi durante gli aggiornamenti.)"
                ),
            }

        grub_paths = ["/etc/default/grub"]
        modified = False
        for path in grub_paths:
            if not os.path.exists(path):
                continue
            try:
                with open(path) as f:
                    content = f.read()
                if "iommu=off" not in content:
                    new_content = re.sub(
                        r'^(GRUB_CMDLINE_LINUX_DEFAULT="[^"]*)"$',
                        r'\1 iommu=off"',
                        content,
                        flags=re.M,
                    )
                    if new_content != content:
                        # backup + scrittura (richiede root)
                        rc, out, err = run_command(
                            ["sh", "-c",
                             f"cp {path} {path}.bak && printf '%s' "
                             f"{shlex_quote(new_content)} > {path}"],
                            sudo=True)
                        modified = rc == 0
            except Exception as e:
                self.logger.error("Errore modifica %s: %s", path, e)

        if modified:
            run_command(["update-grub"], sudo=True, timeout=120)
            return {"applied": True, "needs_reboot": True}
        return {"applied": False, "needs_reboot": False,
                "warning": "GRUB non modificato (serve root)"}

    def rollback(self) -> bool:
        """Rimuove iommu=off (ostree kargs o GRUB)."""
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.enable_iommu()
        if os.path.exists("/run/ostree-booted"):
            rc, _, _ = run_command(
                ["rpm-ostree", "kargs", "--delete", "iommu=off"],
                sudo=True, timeout=120, check=False)
            return rc == 0
        self.logger.warning("Rollback IOMMU: rimuovere iommu=off da GRUB e "
                            "rigenerare la configurazione")
        return True


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)
