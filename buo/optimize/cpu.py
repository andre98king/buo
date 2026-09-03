#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
CPU Undervolt — ricerca del VID minimo stabile.

PRINCIPIO DI SICUREZZA (fail-closed):
    Sul hardware REALE l'ottimizzatore NON inventa valori: delega il
    test di stabilità a `bc250-detect` (che esegue il suo auto-tuning
    con stress test integrato, confermato nello studio del codice).
    Se `bc250-detect` non è disponibile → ConfigurationError: BUO si
    RIFIUTA di undervoltare piuttosto che procedere senza test.

    Il VID non può mai superare cpu_vid_recommended_max, e mai oltre
    l'absolute_max immutabile (1325 mV) di constants.LIMITS.
"""

from typing import Any, Dict, Optional

from ..constants import LIMITS
from ..exceptions import ConfigurationError
from ..unlock.wrappers.bc250_overclock import BC250DetectWrapper
from ..utils.logging import LoggerMixin

# ---- cpu_target_vid: auto (design DESIGN_PORTABILITY_DEFAULTS 3.1) ----
# Target "auto" derivato dalla misura LIVE del VID stock (SMU Q3/0x36):
# clamp(misura − 75, 900, 1250). Un default statico fallisce su chip
# con floor UV diverso (community: stock 1180 / UV 1031; nostro:
# stock 1074 / UV 999). Misura non disponibile → fallback statico 1000
# mV (stessa robustezza: il ladder del consumatore riprova +50).
AUTO_VID_OFFSET_MV = 75
AUTO_VID_MIN_MV = 900
AUTO_VID_MAX_MV = 1250
AUTO_VID_FALLBACK_MV = 1000


def resolve_cpu_target_vid(measure: Optional[int]) -> int:
    """Target VID (mV) per `cpu_target_vid: auto`.

    target = clamp(measure − 75, 900, 1250); misura assente (None,
    non leggibile) → fallback statico 1000 mV. Helper PURO (testabile):
    il ladder di retry e il fallback no-UV vivono nel consumatore
    (orchestrator._optimize_cpu_uv).
    """
    if measure is None:
        return AUTO_VID_FALLBACK_MV
    return max(AUTO_VID_MIN_MV, min(AUTO_VID_MAX_MV,
                                    measure - AUTO_VID_OFFSET_MV))


class CPUUndervoltOptimizer(LoggerMixin):
    """Trova il punto stabile (frequenza/VID) della CPU."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 use_wrapper: bool = True):
        self.mock = mock
        self.mock_hw = mock_hardware
        self.use_wrapper = use_wrapper
        self.detect_wrapper = BC250DetectWrapper() if use_wrapper and not mock \
            else None

    def optimize(self, max_freq: Optional[int] = None,
                 max_vid: Optional[int] = None) -> Dict[str, Any]:
        """
        Trova il punto stabile per la CPU.

        - mock: binary search simulato con MockHardware
        - reale: delega a bc250-detect (test di stabilità reale)

        Returns:
            {"v_f_points": [...], "best_efficiency": {...}, "source": ...}
        """
        max_freq = max_freq or LIMITS.cpu.freq_max
        max_vid = max_vid or LIMITS.cpu.vid_recommended_max
        # MAI oltre l'hard limit immutabile
        max_vid = min(max_vid, LIMITS.cpu.vid_absolute_max)
        max_freq = min(max_freq, LIMITS.cpu.freq_max)

        if self.mock:
            return self._mock_optimize()

        # ---- MODALITÀ REALE: fail-closed ----
        if self.detect_wrapper is None or not self.detect_wrapper.available:
            raise ConfigurationError(
                "bc250-detect non trovato (/usr/local/bin/bc250-detect). "
                "Impossibile undervoltare la CPU in sicurezza: BUO non "
                "applica tensioni senza un test di stabilità reale. "
                "Esegui: sudo buo install-deps"
            )

        self.logger.info(
            "Undervolt CPU reale: delega a bc250-detect "
            "(target %d MHz, VID max %d mV, temp max %d°C)",
            max_freq, max_vid, LIMITS.cpu.temp_max)

        result = self.detect_wrapper.detect(
            target_freq=max_freq,
            max_vid=max_vid,
            max_temp=LIMITS.cpu.temp_max,
            keep=False,
        )

        parsed = result.get("parsed_output", {})
        if not parsed.get("success"):
            stderr = (result.get("stderr", "") or "").strip()
            detail = stderr[-300:] if stderr else "(nessun output)"
            raise ConfigurationError(
                "bc250-detect non ha trovato una configurazione stabile. "
                f"Dettaglio: {detail}"
            )

        # VID: usa quello reale riportato da bc250-detect se disponibile,
        # altrimenti converti scale SMU → VID (formula della community)
        freq = parsed["frequency"]
        scale = parsed.get("scale")
        if parsed.get("vid") is not None:
            vid = parsed["vid"]
        else:
            vid = 1206 - (scale or 0) * 8
        vid = max(LIMITS.cpu.vid_min, min(max_vid, vid))

        point: Dict[str, Any] = {"freq": freq, "vid": vid}
        if scale is not None:
            point["scale"] = scale
        vf_points = [point]
        self.logger.info("Punto stabile trovato: %d MHz @ %d mV%s",
                         freq, vid,
                         f" (scale {scale})" if scale is not None else "")

        return {
            "v_f_points": vf_points,
            "best_efficiency": vf_points[0],
            "source": "bc250-detect",
        }

    # ------------------------------------------------------------------ #

    def _mock_optimize(self) -> Dict[str, Any]:
        if self.mock_hw is not None:
            self.mock_hw.set_cpu_vid(1012)
            self.mock_hw.set_cpu_freq(3700)
        return {
            "v_f_points": [
                {"freq": 3500, "vid": 1012},
                {"freq": 3600, "vid": 1030},
                {"freq": 3700, "vid": 1050},
            ],
            "best_efficiency": {"freq": 3700, "vid": 1012, "watt": 65},
            "source": "mock",
        }
