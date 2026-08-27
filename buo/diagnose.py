#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Doctor — raccolta diagnostica completa in un solo comando.

`buo doctor` produce un riepilogo unico (testo o JSON) di TUTTO ciò che
serve per diagnosticare un problema su hardware reale:
    • versione BUO e ambiente (Python, distro, kernel, Mesa)
    • hardware (core CPU, maschera, CU GPU, temperature)
    • problemi noti rilevati
    • stato tool della community (install-deps --check)
    • configurazione attiva
    • ultimo report e coda dei log

Sola lettura: non modifica nulla. Pensato per il supporto: un comando,
un copia-incolla.
"""

import json
import platform
import sys
from typing import Any, Dict, List

from .audit.hardware import HardwareAudit
from .audit.problems import ProblemDetector
from .config import BUOConfig
from .utils.logging import get_logger

logger = get_logger("doctor")


class Doctor:
    """Raccoglie la diagnostica completa di BUO."""

    def __init__(self, mock: bool = False, mock_hardware=None):
        self.mock = mock
        self.mock_hw = mock_hardware

    # ------------------------------------------------------------------ #

    def diagnose(self) -> Dict[str, Any]:
        """Raccoglie e restituisce il quadro diagnostico completo."""
        report: Dict[str, Any] = {}

        # Ambiente
        report["environment"] = {
            "buo_version": self._buo_version(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "mock": self.mock,
        }

        # Distro
        try:
            from .utils.distro import detect_distro
            d = detect_distro()
            report["distro"] = {
                "id": d.id, "name": d.name,
                "initramfs": d.initramfs_tool,
                "pkg_manager": d.pkg_manager,
            }
        except Exception as e:
            report["distro"] = {"error": str(e)}

        # Audit hardware + problemi
        audit = HardwareAudit(mock=self.mock, mock_hardware=self.mock_hw).run()
        report["hardware"] = audit
        detector = ProblemDetector(mock=self.mock, mock_hardware=self.mock_hw)
        report["problems"] = detector.detect(audit)

        # Tool della community
        try:
            from .install.deps import DependencyManager
            report["deps"] = DependencyManager().check()
        except Exception as e:
            report["deps"] = {"error": str(e)}

        # Configurazione attiva
        try:
            cfg = BUOConfig.load()
            d = cfg.to_dict()
            report["config"] = {
                "mode": d["mode"],
                "psu_wattage": d["psu_wattage"],
                "cooling_type": d["cooling_type"],
                "power_budget": d["safety"]["power_budget"],
                "auto_install_deps": d["deps"]["auto_install"],
                "vram_estimation": d["vram_estimation"]["enabled"],
            }
        except Exception as e:
            report["config"] = {"error": str(e)}

        # Stato dati (checkpoint, report)
        report["data"] = self._data_state()

        # Coda log
        report["log_tail"] = self._log_tail(30)

        return report

    # ------------------------------------------------------------------ #

    @staticmethod
    def _buo_version() -> str:
        try:
            import buo
            return buo.__version__
        except Exception:
            return "unknown"

    @staticmethod
    def _data_state() -> Dict[str, Any]:
        try:
            from .utils.paths import (report_file_json, report_file_md,
                                       state_dir)
            return {
                "state_dir": str(state_dir()),
                "report_md": report_file_md().exists(),
                "report_json": report_file_json().exists(),
            }
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _log_tail(lines: int = 30) -> str:
        try:
            from .utils.paths import log_file
            path = log_file()
            if not path.exists():
                return "(nessun log)"
            content = path.read_text(encoding="utf-8", errors="replace")
            return "\n".join(content.strip().splitlines()[-lines:])
        except Exception:
            return "(log non leggibile)"

    # ------------------------------------------------------------------ #

    def to_text(self, report: Dict[str, Any]) -> str:
        """Formato leggibile per il copia-incolla."""
        out: List[str] = []
        out.append("=" * 60)
        out.append("BUO DOCTOR — diagnostica completa")
        out.append("=" * 60)

        env = report.get("environment", {})
        out.append(f"BUO v{env.get('buo_version')} | Python "
                   f"{env.get('python')} | {env.get('platform', '')[:60]}")
        out.append(f"Mock: {env.get('mock')}")

        distro = report.get("distro", {})
        out.append(f"Distro: {distro.get('name', '?')} "
                   f"(initramfs: {distro.get('initramfs', '?')})")

        hw = report.get("hardware", {})
        kernel = hw.get("kernel", {})
        mesa = hw.get("mesa", {})
        cpu = hw.get("cpu", {})
        gpu = hw.get("gpu", {})
        temps = hw.get("temps", {})
        out.append("")
        out.append("--- HARDWARE ---")
        out.append(f"Kernel: {kernel.get('release', '?')} "
                   f"(ok: {kernel.get('meets_minimum', '?')})")
        out.append(f"Mesa: {mesa.get('version') or 'non rilevata'} "
                   f"(ok: {mesa.get('meets_minimum', '?')})")
        out.append(f"CPU: {cpu.get('cores', '?')} core "
                   f"(mask: {cpu.get('core_mask', '?')})")
        out.append(f"GPU: {gpu.get('cu_count', '?')} CU "
                   f"(mask WGP: {gpu.get('wgp_mask', '-')})")
        out.append(f"Temp: CPU {temps.get('cpu_temp')}°C | "
                   f"GPU {temps.get('gpu_temp')}°C | "
                   f"amb {temps.get('ambient')}°C")
        iommu = hw.get("iommu", {})
        out.append(f"IOMMU: {'attivo ⚠️' if iommu.get('enabled') else 'off ✓'}")
        gov = hw.get("governor", {})
        out.append(f"Governor: {'attivo' if gov.get('active') else 'non attivo'}")

        problems = report.get("problems", [])
        out.append("")
        out.append(f"--- PROBLEMI ({len(problems)}) ---")
        for p in problems:
            out.append(f"  [{p.get('severity', '?').upper()}] {p.get('title')}")

        deps = report.get("deps", {})
        out.append("")
        out.append("--- TOOL COMMUNITY ---")
        for name, st in deps.items():
            if name == "_error":
                out.append(f"  ❌ {st}")
            elif st.get("present"):
                out.append(f"  ✅ {name}")
            else:
                out.append(f"  ⚠️ {name} — manca "
                           f"({', '.join(st.get('missing', []))})")

        cfg = report.get("config", {})
        out.append("")
        out.append("--- CONFIG ---")
        for k, v in cfg.items():
            out.append(f"  {k}: {v}")

        data = report.get("data", {})
        out.append("")
        out.append(f"--- DATI --- state_dir: {data.get('state_dir')} | "
                   f"report: {data.get('report_md')}/{data.get('report_json')}")

        out.append("")
        out.append("--- LOG (ultime 30 righe) ---")
        out.append(report.get("log_tail", ""))
        out.append("=" * 60)
        return "\n".join(out)

    @staticmethod
    def to_json(report: Dict[str, Any]) -> str:
        return json.dumps(report, indent=2, ensure_ascii=False, default=str)
