#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
VRAM Temperature Estimator — stima della temperatura della VRAM
posteriore (che NON ha sensori) a partire dai sensori esistenti.

Modello empirico (dalla community, messaggio 42-54):

    T_vram = T_amb + α * (T_gpu - T_amb) + β * P_gpu

    α = 0.45  (accoppiamento termico GPU→VRAM)
    β = 0.04  (°C per watt di potenza GPU)

Include:
    • smoothing con filtro esponenziale (τ = 5s)
    • calcolo dell'affidabilità (confidence)
    • calibrazione con regressione (numpy opzionale)
    • classe VRAMMLModel (Random Forest, sklearn opzionale) per il
      miglioramento con dati della community — mai bloccante se
      sklearn non è installato
"""

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class VRAMEstimate:
    """Risultato di una stima della temperatura VRAM."""
    temperature: float
    raw_temperature: float
    confidence: float
    is_stable: bool
    gpu_temp: float
    gpu_power: float
    ambient_temp: float
    alpha: float
    beta: float

    def __str__(self) -> str:
        status = "✅" if self.temperature < 80 else \
                 ("⚠️" if self.temperature < 90 else "🔴")
        return f"{status} {self.temperature:.1f}°C (conf: {self.confidence:.0%})"


class VRAMTemperatureEstimator:
    """Stima la temperatura della VRAM con modello empirico."""

    DEFAULT_ALPHA = 0.45
    DEFAULT_BETA = 0.04
    DEFAULT_AMB_TEMP = 22.0
    WARNING_THRESHOLD = 82.0
    CRITICAL_THRESHOLD = 92.0
    TAU = 5.0

    def __init__(self, alpha: Optional[float] = None,
                 beta: Optional[float] = None,
                 tau: Optional[float] = None,
                 warning_threshold: Optional[float] = None,
                 critical_threshold: Optional[float] = None):
        self.alpha = alpha if alpha is not None else self.DEFAULT_ALPHA
        self.beta = beta if beta is not None else self.DEFAULT_BETA
        self.tau = tau if tau is not None else self.TAU
        self.warning_threshold = (
            warning_threshold if warning_threshold is not None
            else self.WARNING_THRESHOLD)
        self.critical_threshold = (
            critical_threshold if critical_threshold is not None
            else self.CRITICAL_THRESHOLD)

        self._filtered_temp: Optional[float] = None
        self._last_time: Optional[float] = None
        self._history: deque = deque(maxlen=60)

        self._estimates_count = 0
        self._warning_count = 0
        self._critical_count = 0

        logger.info("VRAMEstimator: α=%.2f, β=%.2f, τ=%.1fs",
                    self.alpha, self.beta, self.tau)

    # ------------------------------------------------------------------ #

    def estimate(self, gpu_temp: float, gpu_power: float,
                 ambient_temp: Optional[float] = None,
                 force_update: bool = False) -> VRAMEstimate:
        """Stima la temperatura VRAM per una lettura."""
        if ambient_temp is None:
            ambient_temp = self.DEFAULT_AMB_TEMP
        if not (-10 <= ambient_temp <= 50):
            ambient_temp = self.DEFAULT_AMB_TEMP

        raw = (ambient_temp
               + self.alpha * (gpu_temp - ambient_temp)
               + self.beta * gpu_power)
        raw = max(ambient_temp, min(120.0, raw))

        now = time.time()
        if self._filtered_temp is None or force_update:
            filtered = raw
        else:
            dt = (now - self._last_time) if self._last_time else 0.1
            dt = max(0.01, min(1.0, dt))
            k = dt / (dt + self.tau)
            filtered = self._filtered_temp + k * (raw - self._filtered_temp)

        self._filtered_temp = filtered
        self._last_time = now

        self._history.append(filtered)
        confidence = self._calculate_confidence()
        is_stable = len(self._history) > 30 and self._calculate_std() < 2.0

        self._estimates_count += 1
        if filtered > self.critical_threshold:
            self._critical_count += 1
        elif filtered > self.warning_threshold:
            self._warning_count += 1

        return VRAMEstimate(
            temperature=filtered,
            raw_temperature=raw,
            confidence=confidence,
            is_stable=is_stable,
            gpu_temp=gpu_temp,
            gpu_power=gpu_power,
            ambient_temp=ambient_temp,
            alpha=self.alpha,
            beta=self.beta,
        )

    # ------------------------------------------------------------------ #

    def _calculate_confidence(self) -> float:
        if len(self._history) < 10:
            return 0.5
        data_factor = min(1.0, len(self._history) / 60.0)
        std = self._calculate_std()
        stability_factor = max(0.0, 1.0 - (std / 5.0))
        change = self._calculate_recent_change()
        change_factor = max(0.0, 1.0 - (change / 10.0))
        return 0.3 * data_factor + 0.4 * stability_factor + 0.3 * change_factor

    def _calculate_std(self) -> float:
        if len(self._history) < 2:
            return 0.0
        mean = sum(self._history) / len(self._history)
        variance = sum((x - mean) ** 2 for x in self._history) / len(self._history)
        return variance ** 0.5

    def _calculate_recent_change(self) -> float:
        if len(self._history) < 5:
            return 0.0
        recent = list(self._history)[-5:]
        return abs(recent[-1] - recent[0])

    # ------------------------------------------------------------------ #

    def calibrate(self, data_points: List[Dict[str, float]]) -> Dict[str, float]:
        """
        Calibra α e β con regressione lineare (numpy opzionale).

        Args:
            data_points: [{"gpu_temp", "gpu_power", "vram_temp_real"}]
        """
        if len(data_points) < 3:
            logger.warning("Calibrazione richiede almeno 3 punti")
            return {"alpha": self.alpha, "beta": self.beta}

        try:
            import numpy as np
        except ImportError:
            logger.warning("numpy non installato — calibrazione saltata")
            return {"alpha": self.alpha, "beta": self.beta}

        X = np.array([[p["gpu_temp"], p["gpu_power"]] for p in data_points])
        y = np.array([p["vram_temp_real"] for p in data_points])
        X = np.column_stack([np.ones(len(X)), X])

        try:
            coeffs, *_ = np.linalg.lstsq(X, y, rcond=None)
            _, alpha, beta = coeffs
            alpha = max(0.1, min(0.9, float(alpha)))
            beta = max(0.01, min(0.1, float(beta)))
            self.alpha, self.beta = alpha, beta
            logger.info("Calibrazione: α=%.3f, β=%.3f", alpha, beta)
            return {"alpha": alpha, "beta": beta}
        except Exception as e:
            logger.error("Calibrazione fallita: %s", e)
            return {"alpha": self.alpha, "beta": self.beta}

    def get_stats(self) -> Dict[str, Any]:
        return {
            "total_estimates": self._estimates_count,
            "warnings": self._warning_count,
            "criticals": self._critical_count,
            "current_temperature": self._filtered_temp,
            "confidence": (self._calculate_confidence()
                           if self._filtered_temp is not None else 0.0),
            "coefficients": {"alpha": self.alpha, "beta": self.beta},
            "history_samples": len(self._history),
        }


class VRAMMLModel:
    """
    Modello ML opzionale (Random Forest) per la stima VRAM.

    Richiede: numpy, pandas, scikit-learn. Se mancano, il modello
    non è utilizzabile ma BUO continua con il modello empirico.
    """

    FEATURES = ["gpu_temp", "gpu_power", "cpu_temp", "gpu_freq",
                "gpu_utilization", "fan_speed", "ambient_temp"]

    def __init__(self, model_path: Optional[str] = None):
        self._available = False
        self.model = None
        self.scaler = None
        try:
            from sklearn.ensemble import RandomForestRegressor  # noqa: F401
            from sklearn.preprocessing import StandardScaler  # noqa: F401
            self._available = True
        except ImportError:
            logger.info("scikit-learn non installato — modello ML disabilitato")

        if model_path and self._available:
            self._load(model_path)

    @property
    def available(self) -> bool:
        return self._available and self.model is not None

    def train(self, data: List[Dict[str, float]]) -> Dict[str, float]:
        """Addestra il modello su dati raccolti (con ground truth)."""
        if not self._available:
            return {"error": "scikit-learn non installato"}
        try:
            import numpy as np
            from sklearn.ensemble import RandomForestRegressor
            from sklearn.preprocessing import StandardScaler

            X = np.array([[d[f] for f in self.FEATURES] for d in data])
            y = np.array([d["vram_temp_real"] for d in data])

            self.scaler = StandardScaler().fit(X)
            Xs = self.scaler.transform(X)
            self.model = RandomForestRegressor(n_estimators=100, max_depth=15,
                                               random_state=42, n_jobs=-1)
            self.model.fit(Xs, y)
            return {"samples": len(data), "mae_approx": None}
        except Exception as e:
            return {"error": str(e)}

    def predict(self, features: Dict[str, float]) -> Optional[float]:
        if not self.available:
            return None
        try:
            import numpy as np
            x = np.array([[features.get(f, 0.0) for f in self.FEATURES]])
            return float(self.model.predict(self.scaler.transform(x))[0])
        except Exception:
            return None

    def _load(self, path: str) -> None:
        try:
            import joblib
            data = joblib.load(path)
            self.model = data.get("model")
            self.scaler = data.get("scaler")
        except Exception as e:
            logger.warning("Caricamento modello ML fallito: %s", e)

    def save(self, path: str) -> bool:
        if not self.available:
            return False
        try:
            import joblib
            joblib.dump({"model": self.model, "scaler": self.scaler}, path)
            return True
        except Exception:
            return False
