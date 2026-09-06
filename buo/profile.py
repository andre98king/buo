#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Profilo macchina — export/import per il ripristino dopo un format (G2).

Il profilo è un JSON portabile con:
    - i dati di ottimizzazione (undervolt CPU/GPU, overclock) riapplicabili
      SENZA rilanciare l'auto-tuning (bc250-detect),
    - l'elenco dei fix applicati,
    - metadati (versione profilo, data).

Dopo un format: `sudo buo restore` riporta la macchina allo stato
salvato (fix ACPI, unlock CPU/40CU, undervolt persistente, governor).
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("buo.profile")

PROFILE_VERSION = 2
SUPPORTED_VERSIONS = (1, 2)

# ---------------------------------------------------------------------- #


def default_profile_path() -> Path:
    """Percorso del profilo auto-salvato (stessa dir dello stato)."""
    from .utils.paths import state_dir
    return state_dir() / "profile.json"


def _read_checkpoint_optimize() -> Dict[str, Any]:
    """Dati fase optimize dal checkpoint (stato persistente)."""
    try:
        from .state.checkpoint import CheckpointManager
        cm = CheckpointManager()
        return cm.get_phase("optimize").get("data", {})
    except Exception:
        return {}


def _read_undervolt_log_optimize() -> Dict[str, Any]:
    """Fallback: log undervolt dedicato → struttura fase optimize."""
    from .utils.paths import undervolt_log_file
    path = undervolt_log_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {
        "undervolt_cpu": data.get("cpu", {}),
        "undervolt_gpu": data.get("gpu", {}),
    }


def export_profile(path: Optional[Path] = None,
                   oc_dir: Optional[Path] = None,
                   smu_conf: Optional[str] = None,
                   governor_config: Optional[str] = None) -> Dict[str, Any]:
    """Esporta il profilo macchina corrente (checkpoint + log).

    G2/T5: se lo stato OC (`/var/lib/buo/oc` + conf persistiti) è
    leggibile, il blocco top-level `oc_state` viene incluso (schema v2) —
    con UN solo artefatto il restore post-format riporta il daily completo.
    Fail-soft: stato OC assente/illeggibile (es. export non-root) → blocco
    OMESSO con nota, mai crash. Path iniettabili (test).
    """
    optimize = _read_checkpoint_optimize()
    if not optimize:
        optimize = _read_undervolt_log_optimize()

    applied = []
    try:
        from .state.checkpoint import CheckpointManager
        applied = list(CheckpointManager().get("applied_steps", []) or [])
    except Exception:
        pass

    oc_state = None
    try:
        from .oc.profiles import export_oc_state
        oc_state = export_oc_state(oc_dir=oc_dir, smu_conf=smu_conf,
                                   governor_config=governor_config)
    except Exception as e:  # pragma: no cover — fail-soft
        logger.warning("Stato OC non incluso nel profilo: %s", e)
    profile = {
        "profile_version": PROFILE_VERSION,
        "created": datetime.now().isoformat(),
        "applied_fixes": applied,
        "optimize": optimize,
    }
    if oc_state is not None:
        profile["oc_state"] = oc_state
    else:
        # Distingue i PERMESSI dallo stato semplicemente assente: il
        # warning "serve root" vale solo se l'OC_DIR non è leggibile
        # (oc_dir root-only); stato OC mai generato → silenzio (normale).
        from .oc.constants import OC_DIR_DEFAULT
        oc_path = Path(oc_dir) if oc_dir else Path(OC_DIR_DEFAULT)
        try:
            os.listdir(oc_path)
        except PermissionError:
            logger.warning("Stato OC non incluso nel profilo: %s non "
                           "leggibile (serve root)", oc_path)
        except OSError:
            pass
    target = Path(path) if path else default_profile_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(profile, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return profile


def load_profile(path: Optional[Path] = None) -> Dict[str, Any]:
    """Carica e VALIDA un profilo. Solleva ValueError se non valido.

    Accetta v1 e v2 (retro-compatibilità G2/T5): un file v1 (o v2 senza
    blocco oc_state) carica identico a prima — stessi controlli (JSON
    dict, sezione `optimize` presente).
    """
    target = Path(path) if path else default_profile_path()
    if not target.exists():
        raise ValueError(f"Profilo non trovato: {target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"Profilo non leggibile ({target}): {e}") from e
    if not isinstance(data, dict):
        raise ValueError("Profilo non valido: non è un oggetto JSON")
    if data.get("profile_version") not in SUPPORTED_VERSIONS:
        raise ValueError(
            f"Versione profilo non supportata: {data.get('profile_version')} "
            f"(attese: {', '.join(map(str, SUPPORTED_VERSIONS))})")
    optimize = data.get("optimize")
    if not isinstance(optimize, dict):
        raise ValueError("Profilo non valido: manca la sezione 'optimize'")
    return data
