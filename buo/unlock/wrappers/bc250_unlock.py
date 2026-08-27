#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Wrapper per bc250-unlock-cores.py — sblocco CPU 6→8 core.

Analisi dal codice sorgente (confermata nello studio):
    • standalone, NON usa la libreria bc250_smu
    • NON esiste il flag --probe: legge SEMPRE la maschera all'avvio
    • output: "core presence mask: 0x%08X" / "after write: 0x%08X"
    • se mask != 0x77 e manca -f → sys.exit (stop)
    • se Q3 0x98 != 0x01 → "is the governor stopped?"
    • volatile: un cold boot ripristina 6 core
"""

import re
from typing import Any, Dict

from .base import BaseWrapper


class BC250UnlockWrapper(BaseWrapper):
    """Wrapper per lo script di unlock core CPU."""

    def __init__(self, script_path: str = "/usr/local/bin/bc250-unlock-cores.py"):
        super().__init__(script_path, timeout=30)

    def unlock(self, force: bool = False) -> Dict[str, Any]:
        """Esegue l'unlock. `force` = flag -f (rischio: core difettosi)."""
        args = ["-f"] if force else []
        return self.run_with_output(args)

    def parse_output(self, stdout: str, stderr: str) -> Dict[str, Any]:
        parsed: Dict[str, Any] = {
            "before_mask": None,
            "after_mask": None,
            "needs_reboot": False,
            "success": False,
        }

        m = re.search(r"core presence mask:\s*(0x[0-9A-Fa-f]+)", stdout)
        if m:
            parsed["before_mask"] = m.group(1)

        m = re.search(r"after write\s*:\s*(0x[0-9A-Fa-f]+)", stdout)
        if m:
            parsed["after_mask"] = m.group(1)

        if "reboot to bring up all 8 cores" in stdout:
            parsed["needs_reboot"] = True

        if "OK." in stdout or parsed["after_mask"] == "0x000000FF":
            parsed["success"] = True

        return parsed

    def needs_reboot(self, stdout: str) -> bool:
        """True se l'output indica che serve un reboot."""
        return "reboot to bring up all 8 cores" in stdout
