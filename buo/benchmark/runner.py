#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Benchmark Runner — benchmark standard, leggeri e riproducibili.

Scelta del design (messaggio 98): niente giochi come benchmark —
strumenti standard, veloci e riproducibili:

    • GPU stress:  furmark (fallback: glmark2)
    • CPU stress:  stress-ng (fallback: stress)
    • CPU bench:   sysbench
    • Compute:     vkmark (verifica anche il fix ACE)
    • AI inference: onnxruntime (opzionale)

Se un tool non è installato → fallback o skip (mai errore bloccante).
"""

import json
import re
import time
from typing import Any, Dict, Optional

from ..exceptions import BenchmarkError
from ..utils.logging import LoggerMixin
from ..utils.shell import run_command, which


class BenchmarkRunner(LoggerMixin):
    """Esegue i benchmark before/after."""

    def __init__(self, mock: bool = False, mock_hardware=None):
        self.mock = mock
        self.mock_hw = mock_hardware

    # ------------------------------------------------------------------ #

    def run_all(self, gpu_duration: int = 60, cpu_duration: int = 60,
                compute_duration: int = 30) -> Dict[str, Any]:
        """Esegue tutti i benchmark disponibili."""
        results: Dict[str, Any] = {}
        for name, fn in [
            ("gpu_stress", lambda: self.run_gpu_stress(gpu_duration)),
            ("cpu_stress", lambda: self.run_cpu_stress(cpu_duration)),
            ("cpu_bench", self.run_cpu_benchmark),
            ("compute_bench", lambda: self.run_compute_benchmark(compute_duration)),
            ("ai_inference", self.run_ai_inference),
        ]:
            try:
                results[name] = fn()
            except Exception as e:
                self.logger.warning("Benchmark %s saltato: %s", name, e)
                results[name] = {"available": False}
        results["timestamp"] = time.time()
        return results

    # --------------------------- GPU ---------------------------------- #

    def run_gpu_stress(self, duration: int = 60) -> Dict[str, Any]:
        if self.mock and self.mock_hw is not None:
            return {"available": True, "fps": 72.0, "temperature": 67.0,
                    "power": self.mock_hw.get_total_power()}

        if which("glmark2"):
            rc, out, _ = run_command(
                ["glmark2", "--run-forever", "--seconds", str(duration)],
                timeout=duration + 30)
            fps = self._parse_float(r"FPS:\s*([\d.]+)", out)
            return {"available": rc == 0, "fps": fps, "tool": "glmark2"}

        if which("furmark"):
            rc, out, _ = run_command(
                ["furmark", "--benchmark", "--duration", str(duration)],
                timeout=duration + 30)
            return {"available": rc == 0, "fps": self._parse_float(r"([\d.]+)\s*FPS", out),
                    "tool": "furmark"}

        return {"available": False, "note": "glmark2/furmark non installati"}

    # --------------------------- CPU ---------------------------------- #

    def run_cpu_stress(self, duration: int = 60) -> Dict[str, Any]:
        if self.mock and self.mock_hw is not None:
            return {"available": True, "cpu_temp_max": self.mock_hw.get_cpu_temp(),
                    "errors": 0}

        if which("stress-ng"):
            rc, out, _ = run_command(
                ["stress-ng", "--cpu", "0", "--timeout", str(duration),
                 "--metrics-brief"], timeout=duration + 30)
            errors = 0 if rc == 0 else 1
            return {"available": True, "errors": errors, "tool": "stress-ng",
                    "bogo_ops": self._parse_float(r"Bogo ops/s\s+([\d.]+)", out)}
        if which("stress"):
            rc, _, _ = run_command(
                ["stress", "--cpu", "0", "--timeout", str(duration)],
                timeout=duration + 30)
            return {"available": True, "errors": 0 if rc == 0 else 1, "tool": "stress"}
        return {"available": False}

    def run_cpu_benchmark(self, duration: int = 30) -> Dict[str, Any]:
        if self.mock:
            return {"available": True, "events_per_sec": 125000.0}

        if which("sysbench"):
            rc, out, _ = run_command(
                ["sysbench", "cpu", "run", "--threads=8", f"--time={duration}"],
                timeout=duration + 30)
            eps = self._parse_float(r"events per second:\s*([\d.]+)", out)
            return {"available": rc == 0, "events_per_sec": eps}
        return {"available": False}

    # -------------------------- COMPUTE ------------------------------- #

    def run_compute_benchmark(self, duration: int = 30) -> Dict[str, Any]:
        if self.mock:
            return {"available": True, "fps": 158.0, "score": 580.0}

        if which("vkmark"):
            rc, out, _ = run_command(
                ["vkmark", "--benchmark", "triangle", "--duration", str(duration)],
                timeout=duration + 30)
            score = self._parse_float(r"Score:\s*([\d.]+)", out)
            fps = self._parse_float(r"([\d.]+)\s*fps", out)
            return {"available": rc == 0, "score": score, "fps": fps}
        return {"available": False, "note": "vkmark non installato"}

    # ------------------------- AI/ML ---------------------------------- #

    def run_ai_inference(self) -> Dict[str, Any]:
        if self.mock:
            return {"available": True, "avg_ms": 28.0}

        if not which("python3"):
            return {"available": False}
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            return {"available": False, "note": "onnxruntime non installato"}

        # Mini-benchmark sintetico (senza modello esterno)
        code = (
            "import onnxruntime, numpy as np, time\n"
            "s = onnxruntime.InferenceSession('')\n"  # placeholder
        )
        rc, out, _ = run_command(["python3", "-c", code], timeout=30)
        return {"available": False, "note": "richiede un modello ONNX (es. ResNet-18)"}

    # -------------------------- helper -------------------------------- #

    @staticmethod
    def _parse_float(pattern: str, text: str) -> Optional[float]:
        m = re.search(pattern, text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        return None

    @staticmethod
    def to_json(results: Dict[str, Any]) -> str:
        return json.dumps(results, indent=2, ensure_ascii=False)
