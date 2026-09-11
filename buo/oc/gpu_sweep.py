#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Sweep GPU per-silicio sotto `buo oc` (design research/DESIGN_T4_SWEEP_OC.md).

Helper sottile: `run(...)` chiama `GPUUndervoltOptimizer.optimize(sweep=...)`
— la STESSA entry di unleash (zero seconda implementazione dello sweep,
design §2). Esito: oc_dir/gpu-sweep.json (schema spec §3, scrittura atomica
tmp+fsync+mv come ProfileStore.save). gpu.py NON si tocca.

Guard: rifiuta se il motore OC (oc3600.sh) è attivo — criterio REFUSE di
apply (l'engine possiede l'SMU).

Crash-point T4a (spec §3): marcatore in_probe scritto PRIMA dello sweep e
recupero all'avvio (boot_epoch). Deviazione minima documentata: il
marcatore copre solo il PRIMO punto pianificato — la precisione per-probe
richiederebbe un hook in gpu.py (esplicito T4b nel design §3) — quindi un
reboot a metà sweep viene attribuito (conservativamente, fail-closed) a
quel punto e la sua frequenza è saltata al run successivo.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..constants import LIMITS
from .apply import _write_json_atomic
from .constants import SWEEP_FILE
from .profiles import machine_silicon_fingerprint

logger = logging.getLogger("buo.oc.gpu_sweep")

SWEEP_SCHEMA_VERSION = 1
ESITO_FILE = SWEEP_FILE
# Default spec §1 — identici ai default di config.py (gpu_sweep_*): il
# comando CLI li risolve da BUOConfig quando esiste, qui valgono da base.
DEFAULT_FREQS = [1200, 1500, 2000]
DEFAULT_STEP_MV = 25
DEFAULT_FLOOR_MV = 800
# Chiavi extra dello sweep richieste da _sweep_real di gpu.py (oltre a
# enabled/freqs/step_mv/floor_mv) — stessi default di config.py.
DEFAULT_SWEEP_OPTS = {
    "max_steps": 5,
    "test_seconds": 30,
    "confirm_seconds": 60,
    "max_minutes": 15,
}


def _default_engine_active(oc_dir: Path) -> Optional[int]:
    """PID engine OC attivo (pgrep [o]c3600[.]sh) o None — criterio REFUSE
    di apply (buo/oc/apply.py): l'engine possiede l'SMU, mai sweep in
    parallelo."""
    from .controller import OcController
    return OcController(oc_dir=oc_dir, mock=False, dry_run=False).process_active()


def _make_optimizer(mock: bool):
    """GPUUndervoltOptimizer di default. C1: dry-run forza il mock (mai
    letture reali in dry-run), come eff_mock in _init_modules()."""
    from ..optimize.gpu import GPUUndervoltOptimizer
    return GPUUndervoltOptimizer(mock=mock)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _boot_epoch() -> Optional[int]:
    """Epoch del boot corrente (default: /proc/stat btime — riuso di
    buo/oc/smoke.boot_epoch)."""
    from .smoke import boot_epoch
    return boot_epoch()


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _best_point(points: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not points:
        return None
    return min(points, key=lambda p: p.get("voltage", 1000)
               / p.get("freq", 1000))


def run(*, oc_dir, freqs: Optional[List[int]] = None,
        step_mv: Optional[int] = None, floor_mv: Optional[int] = None,
        sweep_opts: Optional[Dict[str, Any]] = None,
        mock: bool = False, dry_run: bool = False, optimizer=None,
        engine_active: Optional[Callable[[], Optional[int]]] = None,
        boot_epoch: Optional[Callable[[], Optional[int]]] = None,
        fingerprint: Optional[str] = None) -> Dict[str, Any]:
    """Esegue lo sweep GPU per-silicio e ritorna il dict esito (schema §3).

    Guard engine (REFUSE di apply), recupero crash-point T4a
    (in_probe + boot_epoch), sweep via GPUUndervoltOptimizer.optimize(sweep=...),
    esito atomico su oc_dir/gpu-sweep.json. In --mock/--dry-run (sim):
    flusso simulato, NESSUNA scrittura reale (C1).
    """
    sim = mock or dry_run
    oc_dir = Path(oc_dir)
    path = oc_dir / ESITO_FILE
    freqs_l = list(freqs) if freqs else list(DEFAULT_FREQS)
    floor = int(floor_mv) if floor_mv is not None else DEFAULT_FLOOR_MV
    step = int(step_mv) if step_mv is not None else DEFAULT_STEP_MV
    # Clamp come config.py (gpu_sweep_floor_mv su LIMITS GPU [700, 1100];
    # gpu_sweep_step_mv [10, 50] multiplo di 5): un floor fuori range
    # produrrebbe punti oltre voltage_absolute_max (mai). Il RAISE
    # difensivo sotto resta comunque a <= voltage_recommended_max (1050),
    # il tetto che gpu.py applica a ogni punto.
    if not (LIMITS.gpu.voltage_min <= floor
            <= LIMITS.gpu.voltage_absolute_max):
        logger.warning("floor_mv=%d fuori range [%d, %d] → clampato", floor,
                       LIMITS.gpu.voltage_min,
                       LIMITS.gpu.voltage_absolute_max)
        floor = max(LIMITS.gpu.voltage_min,
                    min(floor, LIMITS.gpu.voltage_absolute_max))
    step_clamped = max(10, min(50, int(round(step / 5.0) * 5)))
    if step_clamped != step:
        logger.warning("step_mv=%d fuori range [10, 50] (multiplo di 5) "
                       "→ %d", step, step_clamped)
        step = step_clamped
    raise_floor = min(floor, LIMITS.gpu.voltage_recommended_max)

    if not sim:
        active = (engine_active() if engine_active
                  else _default_engine_active(oc_dir))
        if active is not None:
            raise RuntimeError(
                "run engine OC attiva (l'engine possiede l'SMU) — REFUSE: "
                "ferma la run (`buo oc status`) prima dello sweep GPU")

    opt = optimizer if optimizer is not None else _make_optimizer(sim)

    crash_entries: List[Dict[str, Any]] = []
    freqs_run = freqs_l
    if not sim:
        prev = _load_json(path) or {}
        in_probe = prev.get("in_probe")
        if isinstance(in_probe, dict) and in_probe.get("freq") is not None:
            boot = (boot_epoch() if boot_epoch else _boot_epoch())
            started = in_probe.get("started_epoch")
            if (started is not None and boot is not None
                    and int(started) < int(boot)):
                # reboot durante il probe precedente (crash/freeze):
                # punto marcato, mai riprovato (freq esclusa), sweep continua
                crash_entries.append({
                    "f": int(in_probe["freq"]),
                    "v": in_probe.get("voltage"),
                    "rc": 1, "status": "crash"})
                freqs_run = [f for f in freqs_l
                             if f != int(in_probe["freq"])]
                logger.warning(
                    "Punto crashato al boot precedente saltato: %s", in_probe)
            else:
                logger.info("Sweep interrotto nello stesso boot: il punto "
                            "in_probe viene ripreso")
        # Marcatore in_probe prima dello sweep — ponytail: la precisione
        # per-probe richiederebbe un hook in gpu.py (T4b); qui si marca
        # SOLO il primo punto pianificato, quindi un reboot a metà sweep
        # viene attribuito (conservativamente: fail-closed) a quel punto e
        # la sua frequenza è saltata al run successivo.
        if freqs_run:
            payload = dict(prev or {})
            payload["in_probe"] = {"freq": freqs_run[0], "voltage": None,
                                   "started_epoch": int(time.time())}
            payload["updated_at"] = _now()
            _write_json_atomic(path, payload)

    sweep: Dict[str, Any] = {"enabled": True, "freqs": freqs_run,
                             "step_mv": step, "floor_mv": floor}
    sweep.update(DEFAULT_SWEEP_OPTS)
    sweep.update(sweep_opts or {})
    start_freq = freqs_l[0] if freqs_l else DEFAULT_FREQS[0]
    result = opt.optimize(start_freq=start_freq, sweep=sweep)
    if not isinstance(result, dict):
        raise RuntimeError("esito dell'ottimizzatore non valido")

    esito = dict(result)
    # Clamp difensivo dei punti FINALI a >= floor (fail-closed, mai punti
    # sotto il floor in output): direzione sicura — alzare la tensione di
    # un punto validato non può renderlo instabile. Con l'ottimizzatore
    # reale è già garantito da gpu.py (che però segna il flag nel suo
    # sweep_meta, non in top-level): qui protegge il file esito ed eredita
    # il flag da entrambe le sedi.
    clamped = bool(esito.get("clamped_to_floor")
                   or (esito.get("sweep") or {}).get("clamped_to_floor"))
    points: List[Dict[str, Any]] = []
    for p in (result.get("safe_points") or []):
        q = dict(p)
        if q.get("voltage") is not None and q["voltage"] < raise_floor:
            q["voltage"] = raise_floor
            clamped = True
        points.append(q)
    esito["safe_points"] = points
    esito["clamped_to_floor"] = clamped
    best = _best_point(points)
    if best:
        esito["best_efficiency"] = best
    esito["winner"] = ({"freq": best["freq"], "voltage": best["voltage"]}
                       if best else None)
    # tested: crash del boot precedente + punti provati in questo run
    tested = list(crash_entries)
    for r in ((result.get("sweep") or {}).get("results") or []):
        tested.append({"f": r.get("freq"), "v": r.get("voltage"),
                       "rc": 0 if r.get("stable") else 1,
                       "status": "ok" if r.get("stable")
                       else (r.get("reason") or "failed")})
    esito["tested"] = tested
    esito["in_probe"] = None
    esito["schema_version"] = SWEEP_SCHEMA_VERSION
    esito["updated_at"] = _now()
    esito["floor_mv"] = floor
    esito["fingerprint"] = (fingerprint if fingerprint is not None
                            else (machine_silicon_fingerprint(sim=False)
                                  if not sim else None))
    # Sweep per-silicio REALE completo: gpu.py lascia il governor FERMO e
    # la curva precedente non è riapplicata → nota per l'utente (il
    # fallback community NON tocca il governor).
    esito["governor_stopped"] = bool(not sim
                                     and esito.get("source") == "per-silicon")
    esito["written"] = False
    if not sim:
        _write_json_atomic(path, esito)
        esito["written"] = True
    return esito
