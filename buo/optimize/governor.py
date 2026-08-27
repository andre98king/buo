#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Governor — configurazione di cyan-skillfish-governor-smu.

INTEGRAZIONE UPSTREAM (scelta di sicurezza, 27/08/2026):
    BUO NON rigenera lo schema config.toml a mano: legge il template
    ufficiale vendored in `buo/data/governor-default.toml` (copia verbatim
    del `default-config.toml` del repo filippor/cyan-skillfish-governor,
    branch `smu`) e ne adatta SOLO:
        • [frequency-range] min/max
        • [temperature] throttling / recovery
        • la curva [[safe-points]]
    Tutte le altre sezioni (timing, gpu-usage, load-target, dbus...) sono
    lasciate intatte. Se l'upstream cambia schema, basta aggiornare il
    template, senza toccare il codice.

Dallo studio:
    • set-method = "smu" è OBBLIGATORIO (kernel sysfs bypassa i limiti)
    • config: /etc/cyan-skillfish-governor-smu/config.toml
    • safe-points: coppie frequency/voltage
    • thermal throttling: 85°C / recovery 75°C (configurabile)
    • il governor deve essere FERMO durante undervolt/overclock
"""

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..constants import GOVERNOR_CONFIG, GOVERNOR_SERVICE
from ..utils.logging import LoggerMixin

# Template vendored da upstream (filippor/cyan-skillfish-governor, smu).
_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "governor-default.toml"
)


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
                     min_freq: int = 1000, max_freq: int = 2230,
                     throttling: int = 85, recovery: int = 75) -> bool:
        """
        Scrive config.toml partendo dal template upstream e adattando solo
        min/max, temperature e la curva [[safe-points]].

        Args:
            safe_points: [{freq, voltage}] in mV
            min_freq/max_freq: range frequenza (MHz)
            throttling/recovery: soglie termiche °C
        """
        if self.mock:
            self.logger.info("Governor config: simulato (mock) — "
                             "%d safe-points", len(safe_points))
            return True

        try:
            template = _TEMPLATE_PATH.read_text(encoding="utf-8")
        except OSError as e:
            self.logger.error("Template governor non leggibile (%s): %s",
                              _TEMPLATE_PATH, e)
            return False

        # 1. range frequenza
        text = re.sub(r"(?m)^min\s*=.*$", f"min = {min_freq}",
                      template, count=1)
        text = re.sub(r"(?m)^max\s*=.*$", f"max = {max_freq}",
                      text, count=1)

        # 2. soglie termiche (recovery prima di throttling per evitare
        #    match parziali)
        text = re.sub(r"(?m)^throttling_recovery\s*=.*$",
                      f"throttling_recovery = {recovery}", text, count=1)
        text = re.sub(r"(?m)^throttling\s*=.*$",
                      f"throttling = {throttling}", text, count=1)

        # 3. curva safe-points: sostituisci tutto dal primo [[safe-points]]
        #    in poi con i punti calcolati da BUO (il resto del template
        #    resta verbatim).
        marker = "[[safe-points]]"
        if marker not in text:
            self.logger.error("Template senza sezione [[safe-points]]")
            return False
        head = text[:text.index(marker)].rstrip("\n")
        points_block = "\n\n".join(
            f'[[safe-points]]\nfrequency = {p["freq"]}\n'
            f'voltage = {p["voltage"]}'
            for p in safe_points
        )
        final = head + "\n\n" + points_block + "\n"

        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(final, encoding="utf-8")
            self.logger.info("✅ config.toml scritto da template upstream "
                             "(%d safe-points)", len(safe_points))
            return True
        except Exception as e:
            self.logger.error("Scrittura config.toml fallita: %s", e)
            return False

    def write_default_config(self) -> bool:
        """Configurazione di default sicura (community 2026, flat 1000mV)."""
        defaults = [
            {"freq": 1000, "voltage": 800},
            {"freq": 1500, "voltage": 900},
            {"freq": 2000, "voltage": 1000},
        ]
        return self.write_config(defaults)
