#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Governor — configurazione di cyan-skillfish-governor-smu.

Dallo studio:
    • set-method = "smu" è OBBLIGATORIO (kernel sysfs bypassa i limiti)
    • config: /etc/cyan-skillfish-governor-smu/config.toml
    • safe-points: coppie frequency/voltage
    • thermal throttling: 85°C / recovery 75°C (configurabile)
    • il governor deve essere FERMO durante undervolt/overclock
"""

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..constants import GOVERNOR_CONFIG, GOVERNOR_SERVICE
from ..utils.logging import LoggerMixin


class GovernorWrapper(LoggerMixin):
    """Gestisce servizio e configurazione del governor."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 config_path: str = GOVERNOR_CONFIG):
        self.mock = mock
        self.mock_hw = mock_hardware
        self.config_path = Path(config_path)

    # -------------------------- servizio ---------------------------- #

    def is_running(self) -> bool:
        if self.mock:
            return False
        try:
            r = subprocess.run(["systemctl", "is-active", GOVERNOR_SERVICE],
                               capture_output=True, text=True, timeout=10)
            return r.stdout.strip() == "active"
        except Exception:
            return False

    def stop(self) -> bool:
        """Ferma il governor (prerequisito per undervolt/overclock)."""
        if self.mock:
            return True
        r = subprocess.run(["systemctl", "stop", GOVERNOR_SERVICE],
                           capture_output=True, timeout=30)
        return r.returncode == 0

    def start(self) -> bool:
        if self.mock:
            return True
        r = subprocess.run(["systemctl", "start", GOVERNOR_SERVICE],
                           capture_output=True, timeout=30)
        return r.returncode == 0

    def restart(self) -> bool:
        if self.mock:
            return True
        r = subprocess.run(["systemctl", "restart", GOVERNOR_SERVICE],
                           capture_output=True, timeout=30)
        return r.returncode == 0

    # ------------------------ configurazione ------------------------ #

    def write_config(self, safe_points: List[Dict[str, Any]],
                     min_freq: int = 350, max_freq: int = 2230,
                     throttling: int = 85, recovery: int = 75) -> bool:
        """
        Scrive config.toml con set-method="smu" e i safe-points trovati.

        Args:
            safe_points: [{freq, voltage}] in mV
            min_freq/max_freq: range frequenza (MHz)
            throttling/recovery: soglie termiche °C
        """
        if self.mock:
            self.logger.info("Governor config: simulato (mock) — "
                             "%d safe-points", len(safe_points))
            return True

        lines = ['[gpu]', 'set-method = "smu"', "",
                 "[frequency-range]", f"min = {min_freq}",
                 f"max = {max_freq}", "", "[[safe-points]]"]
        for point in safe_points:
            lines.append(f'frequency = {point["freq"]}   '
                         f'voltage = {point["voltage"]}')
        lines += ["", "[temperature]",
                  f"throttling = {throttling}",
                  f"throttling_recovery = {recovery}",
                  "", "[dbus]", "enabled = true"]

        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.logger.info("✅ config.toml scritto (%d safe-points)",
                             len(safe_points))
            return True
        except Exception as e:
            self.logger.error("Scrittura config.toml fallita: %s", e)
            return False

    def write_default_config(self) -> bool:
        """Configurazione di default sicura (dal default-config.toml)."""
        defaults = [
            {"freq": 500, "voltage": 700},
            {"freq": 1000, "voltage": 800},
            {"freq": 1175, "voltage": 850},
            {"freq": 1500, "voltage": 900},
            {"freq": 1600, "voltage": 910},
            {"freq": 1700, "voltage": 920},
            {"freq": 1850, "voltage": 930},
            {"freq": 2000, "voltage": 960},
        ]
        return self.write_config(defaults)
