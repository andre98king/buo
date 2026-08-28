#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Report Generator — report Before/After completo in Markdown e JSON.

Contenuto (dal design, messaggio 104):
    • riepilogo hardware prima/dopo
    • problemi rilevati e fix applicati (con verifica ✅/❌)
    • benchmark comparativi (GPU, CPU, compute, AI)
    • temperature e consumi prima/dopo
    • performance gain in %
    • raccomandazioni
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..utils.logging import LoggerMixin
from ..utils.paths import (report_file_json as _default_json,
                           report_file_md as _default_md)


class ReportGenerator(LoggerMixin):
    """Genera il report finale di BUO."""

    def __init__(self, output_md: Optional[str] = None,
                 output_json: Optional[str] = None):
        self.output_md = Path(output_md) if output_md else _default_md()
        self.output_json = Path(output_json) if output_json else _default_json()

    # ------------------------------------------------------------------ #

    def generate(self, before: Dict[str, Any], after: Dict[str, Any],
                 problems: Optional[list] = None,
                 fixes: Optional[Dict[str, Dict[str, Any]]] = None,
                 benchmarks: Optional[Dict[str, Any]] = None,
                 applied_fixes: Optional[list] = None,
                 notes: Optional[list] = None,
                 fix_summary: Optional[Dict[str, Any]] = None,
                 fix_results: Optional[Dict[str, Dict[str, Any]]] = None) -> Path:
        """Genera e salva il report; restituisce il percorso .md."""
        report = {
            "generated_at": datetime.now().isoformat(),
            "before": before,
            "after": after,
            "problems_found": problems or [],
            "fixes_verification": fixes or {},
            "fix_summary": fix_summary or {},
            "fix_results": fix_results or {},
            "applied_fixes": applied_fixes or [],
            "benchmarks": benchmarks or {},
            "performance_gain": self._compute_gains(before, after, benchmarks),
            "notes": notes or [],
        }

        # JSON
        self.output_json.parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        # Markdown
        self.output_md.parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_md, "w", encoding="utf-8") as f:
            f.write(self._render_markdown(report))

        self.logger.info("📄 Report generato: %s", self.output_md)
        return self.output_md

    # ------------------------------------------------------------------ #

    def _compute_gains(self, before: Dict[str, Any], after: Dict[str, Any],
                       benchmarks: Dict[str, Any]) -> Dict[str, Any]:
        gains: Dict[str, Any] = {}

        b_cpu = before.get("cpu", {}).get("cores", 6)
        a_cpu = after.get("cpu", {}).get("cores", 6)
        if b_cpu and a_cpu:
            gains["cpu_cores"] = f"+{round((a_cpu - b_cpu) / b_cpu * 100)}%" \
                if a_cpu > b_cpu else "0%"

        b_cu = before.get("gpu", {}).get("cu_count", 24)
        a_cu = after.get("gpu", {}).get("cu_count", 24)
        if b_cu and a_cu and a_cu > b_cu:
            gains["gpu_cu"] = f"+{round((a_cu - b_cu) / b_cu * 100)}%"

        b_temp = before.get("temps", {}).get("gpu_temp")
        a_temp = after.get("temps", {}).get("gpu_temp")
        if b_temp and a_temp:
            gains["gpu_temp_delta"] = f"{a_temp - b_temp:+.1f}°C"

        b_fps = benchmarks.get("before", {}).get("gpu_stress", {}).get("fps")
        a_fps = benchmarks.get("after", {}).get("gpu_stress", {}).get("fps")
        if b_fps and a_fps:
            gains["gpu_fps"] = f"+{round((a_fps - b_fps) / b_fps * 100)}%"

        return gains

    # ------------------------------------------------------------------ #

    def _render_markdown(self, report: Dict[str, Any]) -> str:
        lines = [
            "# 🚀 BC-250 Ultimate Orchestrator — Report",
            "",
            f"**Generato:** {report['generated_at']}",
            "",
            "---",
            "",
            "## 📋 Riepilogo Generale",
            "",
            "| Componente | Prima | Dopo |",
            "|:---|:---|:---|",
        ]

        b, a = report["before"], report["after"]
        b_cpu, a_cpu = b.get("cpu", {}), a.get("cpu", {})
        b_gpu, a_gpu = b.get("gpu", {}), a.get("gpu", {})

        lines.append(f"| CPU Core | {b_cpu.get('cores', '?')} | "
                     f"{a_cpu.get('cores', '?')} |")
        lines.append(f"| GPU CU | {b_gpu.get('cu_count', '?')} | "
                     f"{a_gpu.get('cu_count', '?')} |")
        b_temp, a_temp = b.get("temps", {}), a.get("temps", {})
        lines.append(f"| Temp GPU | {b_temp.get('gpu_temp', '—')}°C | "
                     f"{a_temp.get('gpu_temp', '—')}°C |")

        lines += ["", "## 🔍 Problemi Rilevati", ""]
        if report["problems_found"]:
            for p in report["problems_found"]:
                lines.append(f"- [{p.get('severity', '?').upper()}] "
                             f"{p.get('title', p.get('id'))}")
        else:
            lines.append("- ✅ Nessun problema noto rilevato")

        lines += ["", "## 🔧 Esito Fix (applicazione)", ""]
        fix_results = report.get("fix_results") or {}
        status_icon = {
            "applied": "✅ applicato",
            "manual": "⚠️ manuale",
            "failed": "❌ fallito",
        }
        if fix_results:
            lines.append("| Fix | Stato | Dettaglio |")
            lines.append("|:---|:---|:---|")
            for name, v in fix_results.items():
                icon = status_icon.get(v.get("status"), "❓ sconosciuto")
                detail = (v.get("note") or v.get("detail")
                          or v.get("warning") or v.get("error") or "")
                lines.append(f"| {name} | {icon} | {detail} |")
        else:
            lines.append("- Nessun fix eseguito in questa fase")

        lines += ["", "## ✅ Verifica Fix", ""]
        if report["fixes_verification"]:
            lines.append("| Fix | Stato | Dettaglio |")
            lines.append("|:---|:---|:---|")
            for name, v in report["fixes_verification"].items():
                ok = v.get("ok")
                icon = "✅" if ok else ("❌" if ok is False else "⚠️")
                lines.append(f"| {name} | {icon} | {v.get('detail', '')} |")
        else:
            lines.append("- Nessun fix verificato")

        lines += ["", "## 📊 Benchmark", ""]
        bench = report.get("benchmarks", {})
        for phase in ("before", "after"):
            lines.append(f"### {phase.capitalize()}")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(bench.get(phase, {}), indent=2, ensure_ascii=False))
            lines.append("```")
            lines.append("")

        lines += ["## 📈 Performance Gain", ""]
        gains = report.get("performance_gain", {})
        if gains:
            for k, v in gains.items():
                lines.append(f"- **{k}:** {v}")
        else:
            lines.append("- Nessun dato comparativo")

        if report["notes"]:
            lines += ["", "## 📝 Note", ""]
            for n in report["notes"]:
                lines.append(f"- {n}")

        lines += [
            "",
            "---",
            "",
            "*Report generato da BC-250 Ultimate Orchestrator v1.0.0*",
            "",
        ]
        return "\n".join(lines)
