#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
TUI — Cockpit interattivo di BUO (textual).

`buo tui` apre una dashboard a schermo intero con:
    • stato hardware live (CPU core/freq/VID/temp, GPU CU/freq/voltage/temp,
      potenza totale, ventole, ambiente)
    • flag di stato (undervolt / overclock / 40-CU / fix applicati)
    • log delle letture
    • aggiornamento automatico ogni secondo

`textual` è una dipendenza OPZIONALE: senza di essa `buo tui` mostra un
messaggio chiaro e la CLI classica (rich) resta pienamente funzionante.
"""

from typing import Any, Dict, Optional

from .utils.logging import get_logger

logger = get_logger("tui")


# ============================================================================
# Fonte delle letture live (mock o hardware reale)
# ============================================================================


class LiveReadings:
    """Fornisce letture aggiornate: MockHardware o audit reale."""

    def __init__(self, mock: bool = False, mock_hardware=None):
        self.mock = mock
        self.hardware = mock_hardware
        self.audit = None
        if not mock:
            from .audit.hardware import HardwareAudit
            self.audit = HardwareAudit(mock=False)

    def read(self) -> Dict[str, Any]:
        """Lettura corrente (dict piatto per la dashboard)."""
        if self.mock and self.hardware is not None:
            info = self.hardware.get_system_info()
            return {
                "cpu_cores": info.get("cpu_cores", 0),
                "cpu_freq": info.get("cpu_freq", 0),
                "cpu_vid": info.get("cpu_vid", 0),
                "cpu_temp": info.get("cpu_temp", 0),
                "gpu_cu": info.get("gpu_cu", 0),
                "gpu_freq": info.get("gpu_freq", 0),
                "gpu_voltage": info.get("gpu_voltage", 0),
                "gpu_temp": info.get("gpu_temp", 0),
                "gpu_power": info.get("gpu_power", 0),
                "total_power": info.get("total_power", 0),
                "fan_speed": info.get("fan_speed", 0),
                "ambient_temp": info.get("ambient_temp", 0),
                "undervolted": info.get("is_undervolted", False),
                "overclocked": info.get("is_overclocked", False),
                "cu40": info.get("is_40cu_enabled", False),
            }

        # Hardware reale: letture leggere via audit
        if self.audit is not None:
            try:
                temps = self.audit._audit_temps()
                cpu = self.audit._audit_cpu()
                gpu = self.audit._audit_gpu()
                return {
                    "cpu_cores": cpu.get("cores", 0),
                    "cpu_freq": 0,          # non esposta semplicemente
                    "cpu_vid": 0,
                    "cpu_temp": temps.get("cpu_temp") or 0,
                    "gpu_cu": gpu.get("cu_count") or 0,
                    "gpu_freq": 0,
                    "gpu_voltage": 0,
                    "gpu_temp": temps.get("gpu_temp") or 0,
                    "gpu_power": 0,
                    "total_power": 0,
                    "fan_speed": 0,
                    "ambient_temp": temps.get("ambient") or 0,
                    "undervolted": False,
                    "overclocked": False,
                    "cu40": (gpu.get("cu_count") or 0) >= 40,
                }
            except Exception as e:
                logger.debug("Lettura hardware fallita: %s", e)

        return {}


# ============================================================================
# Testo della dashboard (funzione pura, testabile senza terminale)
# ============================================================================


def dashboard_text(r: Dict[str, Any]) -> str:
    """Compone il testo della dashboard da un dict di letture."""
    W = 43  # larghezza interna del riquadro

    def row(content: str) -> str:
        return "│" + content.ljust(W) + "│"

    def sep(start: str, end: str) -> str:
        return start + "─" * W + end

    lines = []
    lines.append(sep("┌", "┐"))
    lines.append(row("🔍 STATO HARDWARE — LIVE"))
    lines.append(sep("├", "┤"))

    cpu_temp = float(r.get("cpu_temp", 0))
    cpu_ok = "✅" if cpu_temp < 90 else "🔴"
    uv = " 🔽 undervolt" if r.get("undervolted") else ""
    oc = " ⬆️ OC" if r.get("overclocked") else ""
    lines.append(row(f"🧠 CPU:  {r.get('cpu_cores', 0)} core  "
                     f"{r.get('cpu_freq', 0)} MHz  {cpu_temp:.1f}°C {cpu_ok}"))
    lines.append(row(f"          VID {r.get('cpu_vid', 0)} mV{uv}{oc}"))

    gpu_temp = float(r.get("gpu_temp", 0))
    gpu_ok = "✅" if gpu_temp < 85 else "🔴"
    cu_label = f"{r.get('gpu_cu', 0)} CU"
    if r.get("cu40"):
        cu_label += " (40)"
    lines.append(row(f"🎮 GPU:  {cu_label:<9} {r.get('gpu_freq', 0)} MHz  "
                     f"{gpu_temp:.1f}°C {gpu_ok}"))
    lines.append(row(f"          {r.get('gpu_voltage', 0)} mV  "
                     f"{r.get('gpu_power', 0)} W"))

    lines.append(sep("├", "┤"))
    lines.append(row(f"⚡ Potenza: {float(r.get('total_power', 0)):.1f} W    "
                     f"💨 {r.get('fan_speed', 0)} RPM"))
    lines.append(row(f"🌡 Ambiente: {float(r.get('ambient_temp', 0)):.1f}°C"))
    lines.append(sep("└", "┘"))
    return "\n".join(lines)


# ============================================================================
# App TUI (definita solo quando textual è disponibile)
# ============================================================================


def run_tui(mock: bool = False, mock_hardware=None) -> int:
    """
    Avvia il cockpit TUI.

    Raises:
        RuntimeError: se `textual` non è installato
    """
    import importlib.util
    if importlib.util.find_spec("textual") is None:
        raise RuntimeError(
            "TUI non disponibile: installa la dipendenza opzionale "
            "con: pip install textual   (o: pip install -e '.[tui]')"
        )

    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import Footer, Header, Static

    class BuoApp(App):
        """Cockpit live di BUO."""

        TITLE = "BC-250 Ultimate Orchestrator"
        SUB_TITLE = "Cockpit live — hw reale" if not mock else "Cockpit live — MOCK"
        CSS = """
        #dashboard {
            width: 1fr;
            padding: 1;
            border: round $primary;
        }
        #logbox {
            width: 1fr;
            padding: 1;
            border: round $secondary;
            height: 1fr;
        }
        #log {
            height: 1fr;
        }
        """

        BINDINGS = [
            ("q", "quit", "Esci"),
            ("space", "refresh_now", "Aggiorna"),
        ]

        def __init__(self, readings: LiveReadings):
            super().__init__()
            self.readings = readings
            self._timer = None

        def compose(self) -> ComposeResult:
            yield Header()
            with Horizontal():
                yield Static(dashboard_text(self.readings.read()),
                             id="dashboard")
                with VerticalScroll(id="logbox"):
                    yield Static("📋 Log letture:\n", id="log")
            yield Footer()

        def on_mount(self) -> None:
            self._timer = self.set_interval(1.0, self.refresh_now)

        def refresh_now(self) -> None:
            r = self.readings.read()
            dash = self.query_one("#dashboard", Static)
            dash.update(dashboard_text(r))
            log = self.query_one("#log", Static)
            now = r
            log.update(
                "📋 Log letture:\n\n"
                f"CPU {now.get('cpu_temp', 0):.1f}°C | "
                f"GPU {now.get('gpu_temp', 0):.1f}°C | "
                f"Power {float(now.get('total_power', 0)):.1f}W\n"
            )

        def on_unmount(self) -> None:
            if self._timer is not None:
                self._timer.stop()

    provider = LiveReadings(mock=mock, mock_hardware=mock_hardware)
    app = BuoApp(provider)
    app.run()
    return 0
