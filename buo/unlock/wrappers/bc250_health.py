#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Wrapper per bc250-cu-health-test.sh — test di salute per-WGP.

Analisi dal codice sorgente (confermata nello studio):
    • NON ha un limite fisso di 20 reboot: isola una WGP alla volta,
      riavvia, testa, e continua finché tutte le 20 WGP sono testate
    • stato in /var/lib/bc250-cu-health-test/
    • formato results.tsv: #idx se sh wgp status rc active_cu started finished
    • comandi: start | resume | quick | status | reset
"""

from pathlib import Path
from typing import Any, Dict, List

from ...constants import HEALTH_RESULTS_FILE
from .base import BaseWrapper

# results.tsv COMPLETO = una riga per ognuna delle 20 WGP testate dallo
# script (target 0..19, una per reboot — verificato nel sorgente
# bc250-cu-health-test.sh: `next=$((target+1)); if [ "$next" -lt 20 ]`).
# Il riuso "smart" (design DESIGN_PORTABILITY_DEFAULTS 3.4) decide su
# questo conteggio: completo → nessun nuovo protocollo per-WGP.
HEALTH_WGP_TOTAL = 20


class BC250HealthWrapper(BaseWrapper):
    """Wrapper per il CU health test."""

    def __init__(self, script_path: str = "/usr/local/bin/bc250-cu-health-test.sh"):
        super().__init__(script_path, timeout=30)

    def start(self) -> Dict[str, Any]:
        """Avvia il test dalla WGP 0 (con reboot automatici)."""
        return self.run_with_output(["start"])

    def resume(self) -> Dict[str, Any]:
        """Riprende il test dopo un reboot."""
        return self.run_with_output(["resume"])

    def quick(self) -> Dict[str, Any]:
        """Test singolo senza reboot."""
        return self.run_with_output(["quick"], timeout=600)

    def status(self) -> Dict[str, Any]:
        return self.run_with_output(["status"], check_returncode=False)

    def reset(self) -> Dict[str, Any]:
        """Rimuove stato, servizio e configurazione (rollback)."""
        return self.run_with_output(["reset"])

    # ------------------------------------------------------------------ #

    def read_results(self, results_file: str = HEALTH_RESULTS_FILE) -> Dict[str, Any]:
        """
        Legge results.tsv.

        Returns:
            {"stable": [cu_index...], "defective": [cu_index...],
             "total": n, "complete": bool,
             "present": bool, "rows": int}
            complete = tutte le HEALTH_WGP_TOTAL WGP hanno una riga
            PASS/FAIL (riuso "smart": design PORTABILITY_DEFAULTS 3.4).
            present/rows distinguono results.tsv ASSENTE (present=False)
            da PARZIALE (present=True, rows < HEALTH_WGP_TOTAL) — la
            decisione D8 della validazione post-unlock (design
            POSTUNLOCK_VALIDATION) legge SOLO questi due campi nuovi;
            la semantica `complete` è invariata.
        """
        stable: List[int] = []
        defective: List[int] = []

        path = Path(results_file)
        if not path.exists():
            return {"stable": [], "defective": [], "total": 0,
                    "complete": False, "present": False, "rows": 0,
                    "error": "results.tsv non trovato"}

        rows = 0
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) < 5:
                        continue
                    idx, se, sh, wgp, status = parts[:5]
                    # BUG DI CAMPO (audit 11/09): `se*8 + sh*4 + wgp` è un
                    # indice di WGP che COLLIDE fra gli SH (es. 4 = (0,0,4) e
                    # (0,1,0)) e non è un indice CU → la mappatura CU→WGP dei
                    # consumatori era sbagliata (spesso fail-closed, a volte
                    # WGP extra inesistente). Un WGP copre DUE CU: indice base
                    # 2*(SA*5 + WGP), con SA = SE*2 + SH.
                    rows += 1
                    sa = int(se) * 2 + int(sh)
                    cus = (2 * (sa * 5 + int(wgp)), 2 * (sa * 5 + int(wgp)) + 1)
                    if status == "PASS":
                        stable.extend(cus)
                    else:
                        defective.extend(cus)
            # `total`/`rows` = RIGHE (WGP testate) — la completezza si misura
            # in WGP, non in CU: `stable`/`defective` contengono gli INDICI CU
            # (2 per WGP) perché è quello che serve ai consumatori.
            return {
                "stable": sorted(stable),
                "defective": sorted(defective),
                "total": rows,
                "complete": rows >= HEALTH_WGP_TOTAL,
                "present": True,
                "rows": rows,
            }
        except Exception as e:
            return {"stable": [], "defective": [], "total": 0,
                    "complete": False, "present": False, "rows": 0,
                    "error": str(e)}
