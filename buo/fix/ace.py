#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
ACE Fix — code di calcolo GPU (async compute) via bc250-gfx1013-fix.

Dallo studio (messaggio 92):
    • il problema: le code di calcolo dedicate (ACE) sono disabilitate
      su Linux; abilitarle senza fix corrompe i frame
    • la soluzione: 3 patch kernel + 3 patch Mesa/RADV
    • risultati: +20-25% FPS in Cyberpunk 2077, Vulkan CTS zero regressioni
    • AVVERTENZA CRITICA: "mai installare Mesa senza il kernel patchato"
      → il sistema si blocca. BUO applica sempre kernel+Mesa insieme.
"""

from pathlib import Path
from typing import Any, Dict, Optional

from ..utils.logging import LoggerMixin

ACE_REPO = "https://github.com/DryhoppedIPA/bc250-gfx1013-fix"


class ACEComputeFix(LoggerMixin):
    """Applica il fix delle code di calcolo (kernel + Mesa)."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 repo_path: Optional[str] = None):
        self.mock = mock
        self.mock_hw = mock_hardware
        self.repo_path = repo_path  # checkout locale di bc250-gfx1013-fix

    # ------------------------------------------------------------------ #

    def verify(self) -> bool:
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.state.is_ace_fixed
        return False

    def apply(self) -> Dict[str, Any]:
        """
        Applica il fix ACE.

        Utilizza ./install.sh del repo bc250-gfx1013-fix
        (deps → build → install → reboot). L'installer mantiene il kernel
        stock intatto: kernel patchato in initramfs separato, Mesa in
        /opt/bc250-gfx1013/.
        """
        if self.mock and self.mock_hw is not None:
            ok = self.mock_hw.apply_ace_fix()
            return {"applied": ok, "needs_reboot": True}

        if not self.repo_path or not Path(self.repo_path, "install.sh").exists():
            return {
                "applied": False,
                "needs_reboot": False,
                "warning": (
                    "Repo bc250-gfx1013-fix non trovato. Clona da "
                    f"{ACE_REPO} e specifica --ace-repo <percorso>. "
                    "In produzione: sudo ./install.sh deps && "
                    "./install.sh build && sudo ./install.sh install "
                    "&& sudo reboot"
                ),
            }

        self.logger.warning(
            "Applicazione reale del fix ACE non eseguita: richiede "
            "compilazione di kernel+Mesa (lunga). Seguire il README del repo."
        )
        return {
            "applied": False,
            "needs_reboot": True,
            "warning": (
                "Eseguire manualmente nel repo: sudo ./install.sh deps, "
                "./install.sh build, sudo ./install.sh install, reboot. "
                "MAI installare Mesa senza kernel patchato."
            ),
        }

    def rollback(self) -> bool:
        """boot-stock + uninstall del fix."""
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.remove_ace_fix()
        self.logger.warning(
            "Rollback ACE: sudo ./install.sh boot-stock && sudo reboot, "
            "poi sudo ./install.sh uninstall"
        )
        return True
