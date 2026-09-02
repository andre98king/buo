#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Contratto engine ⇄ tool BUO OC + costanti di sicurezza OC.

QUESTO MODULO È IL PUNTO UNICO DI ACCOPPIAMENTO col motore bash
`oc3600.sh` (tmp-bench/oc3600.sh, deployato su /var/lib/buo/oc). Ogni
modifica futura del motore che tocchi questi contratti (state.json schema 3,
exit code, flag CLI, nomi file, unità systemd) DEVE aggiornare questo modulo
(test di contratto in tests/test_oc_state.py).

Le costanti di sicurezza sono MIRROR dell'engine: modifiche qui = modifica
al motore (regola anti-zona, muro frequenza, gate termici, bounds scale).
"""

from pathlib import Path

from ..constants import GOVERNOR_SERVICE, SMU_OC_SERVICE

# ---------------------------------------------------------------------------
# Contratto engine (default = /var/lib/buo/oc; env OC_DIR per i test)
# ---------------------------------------------------------------------------
OC_DIR_DEFAULT = "/var/lib/buo/oc"

ENGINE_SCRIPT = "oc3600.sh"
STATE_FILE = "state.json"
LOG_FILE = "oc.log"
CONSOLE_LOG = "console.log"
RUN_PID = "run.pid"
LAUNCH_PID = "launch.pid"

# File PROPRIETÀ del tool BUO (il motore NON li legge → contratto engine
# intatto): profili utente, marcatore apply, log apply, marcatore smoke.
PROFILES_FILE = "profiles.json"
APPLY_MARKER = "apply.json"
APPLY_LOG = "apply.log"
SMOKE_MARKER = "smoke.marker.json"

# silicon-profile.json è PROPRIETÀ del motore: qui SOLO lettura (SiliconView).
SILICON_PROFILE = "silicon-profile.json"

UNIT_NAME = "buo-oc"            # unità systemd TRANSIENTE della run engine
APPLY_UNIT_NAME = "buo-oc-apply"  # riservato (future estensioni)

SMU_OC_CONF = "/etc/bc250-smu-oc.conf"
BACKUP_SUFFIX = ".buo-rollback"  # es. bc250-smu-oc.conf.buo-rollback-<ts>

SMOKE_STRESS_S = 30              # smoke test all'apply (spec p3_smoke engine)
SMOKE_TIMEOUT_S = SMOKE_STRESS_S + 30
SMOKE_FREQ_MARGIN = 50           # freq_min >= freq - 50 (clock stretching)

# ---------------------------------------------------------------------------
# Costanti di sicurezza (MIRROR dell'engine — mai divergere)
# ---------------------------------------------------------------------------
TEMP_GATE = 85                   # gate termico di ricerca/apply (°C)
TEMP_CRITICAL = 90               # abort critical (°C)
VID_CAP_HARD = 1325              # hard limit assoluto del tool SMU (mV)
SCALE_MIN = -50
SCALE_MAX = 0
F_SEARCH_MAX = 3850              # tetto di ricerca del motore
WALL_FREQ = 3860                 # muro: MAI freq >= 3860
HANG_ZONE_MIN_FREQ = 3725        # anti-zona utente (dati campo 31/08)
HANG_ZONE_MIN_VID = 1000         # mai VID < 1000 su clock >= 3725
FREQ_MIN_OC = 3500               # sotto = downclock (profilo "cool", ammesso)

# ---------------------------------------------------------------------------
# Fasi motore → etichette (tabella di run-oc.sh cmd_status)
# ---------------------------------------------------------------------------
PHASE_LABELS = {
    "P0": "P0 preflight",
    "P1a": "P1a floor 3500",
    "P1b": "P1b ascesa",
    "P1c": "P1c fine 5MHz",
    "P1d": "P1d Vmin finale",
    "P1e": "P1e progresso",
    "P2": "P2 L2-validazione",
    "P3": "P3 persist",
    "P4": "P4 post",
    "P5": "P5 calibrazione efficienza",
    "done": "done (completata)",
}

# Exit code engine: 0 ok · 1 preflight/abort · 2 no-progress · 4/5/6
# persist/post · 40 SIGTERM/wallclock (riprendibile) · 130 SIGINT
ENGINE_EXIT_RESUME = {40, 130}

# Stato apply.json
APPLY_STATES = ("applying", "ok", "rolled_back", "aborted", "stale")
