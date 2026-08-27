#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Wrapper per bc250-detect e bc250-apply (bc250_smu_oc).

Analisi dallo studio (messaggio 2 e 22):
    • bc250-detect: -f <MHz> -v <mV> [-t <°C>] [-k]
      auto-tuning a step di 100 MHz da 3500, test con `stress --cpu 12`
    • bc250-apply: --apply <conf> | --install <conf> | --uninstall
    • overclock.conf: sezione [overclock] con frequency/scale/max_temperature
"""

import re
from typing import Any, Dict, Optional

from .base import BaseWrapper


class BC250DetectWrapper(BaseWrapper):
    """Wrapper per bc250-detect (auto-tuning CPU)."""

    def __init__(self, script_path: str = "/usr/local/bin/bc250-detect"):
        super().__init__(script_path, timeout=600)  # il test è lungo

    def detect(self, target_freq: int, max_vid: int,
               max_temp: int = 90, keep: bool = False) -> Dict[str, Any]:
        """
        Esegue l'auto-tuning CPU via bc250-detect.

        Args:
            target_freq: frequenza target (MHz)
            max_vid: VID massimo consentito (mV)
            max_temp: temperatura massima (°C)
            keep: mantiene la configurazione dopo il test (-k)
        """
        args = ["-f", str(target_freq), "-v", str(max_vid),
                "-t", str(max_temp)]
        if keep:
            args.append("-k")
        return self.run_with_output(args, check_returncode=False)

    def parse_output(self, stdout: str, stderr: str) -> Dict[str, Any]:
        parsed: Dict[str, Any] = {
            "frequency": None,
            "scale": None,
            "vid": None,
            "max_temperature": None,
            "success": False,
        }
        # Formato reale osservato sull'hardware (bc250-detect 2026):
        #   "Final Result: 3500 MHz @ 1087 mV using scale 0"
        # Formato storico (documentazione):
        #   "Safe: 3500 MHz @ 0"
        m = re.search(r"(?:Final Result|Safe):\s*(\d+)\s*MHz\s*@\s*(-?\d+)"
                      r"(?:\s*mV)?(?: using scale (-?\d+))?", stdout)
        if m:
            parsed["frequency"] = int(m.group(1))
            parsed["vid"] = int(m.group(2))
            if m.group(3) is not None:
                parsed["scale"] = int(m.group(3))
            parsed["success"] = True
        m = re.search(r"max_temperature:\s*(\d+)", stdout)
        if m:
            parsed["max_temperature"] = int(m.group(1))
        return parsed


class BC250ApplyWrapper(BaseWrapper):
    """Wrapper per bc250-apply (applicazione/persistenza config CPU)."""

    def __init__(self, script_path: str = "/usr/local/bin/bc250-apply"):
        super().__init__(script_path, timeout=30)

    def apply(self, config_file: str) -> Dict[str, Any]:
        """Applica una configurazione (volatile)."""
        return self.run_with_output(["--apply", config_file])

    def install(self, config_file: str) -> Dict[str, Any]:
        """Installa come servizio systemd (persistente)."""
        return self.run_with_output(["--install", config_file])

    def uninstall(self) -> Dict[str, Any]:
        """Rimuove servizio e configurazione (rollback)."""
        return self.run_with_output(["--uninstall"], check_returncode=False)
