#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Selezione del tool di stress GPU — UNICA fonte di verità.

La usano il probe dello sweep per-silicio (`buo/optimize/gpu.py`) e la
validate (`buo/validate/stress.py`). La DIVERGENZA fra i due percorsi ha
prodotto un bug di campo (10/09/2026): la validate tentava
`glmark2 --run-forever --seconds N` (opzione INESISTENTE in glmark2
2023.01 di Fedora) e, sulla macchina senza glmark2/furmark ma con vkmark,
non provava alcun tool → `gpu_rc != 0` → validate fallita SEMPRE con
`scope=both` → il rollback automatico disinstallava una config CPU BUONA
(`bc250-apply --uninstall`) a ogni run.

Regole (verificate sul campo):
    • vkmark → `-b desktop:duration=N --size 1920x1080` (rc=0 a fine run):
      carico GPU REALISTICO e con durata controllata → tool PRIMARIO
      (decisione utente 10/09: FurMark è troppo aggressivo/sintetico per
      fare da bench — 250-320 W e 110 °C a 2000 MHz sono scenari che nel
      gioco non esistono, quindi non certificano nulla di reale);
    • furmark (CLI FurMark 2) → `--demo furmark-gl --max-time N` — solo
      ULTIMA RISORSA se vkmark non è installato (nessun altro tool con
      durata reale);
    • glmark2 ESCLUSO: nessuna opzione di durata ⇒ nessun probe a durata
      fissa con rc=0 (fail-closed verso la tabella community);
    • nessun tool disponibile → None: il componente NON è verificabile
      (i chiamanti NON devono trattarlo come fallimento del test).
"""

from typing import List, Optional

from .shell import which


def gpu_stress_tool() -> Optional[str]:
    """Nome del tool utilizzabile ('vkmark' | 'furmark') o None."""
    if which("vkmark"):
        return "vkmark"
    if which("furmark"):
        return "furmark"
    return None


def gpu_stress_cmd(seconds: int) -> Optional[List[str]]:
    """Comando di stress GPU per `seconds` secondi (None se nessun tool).

    Entrambe le sintassi tornano rc=0 alla fine della durata richiesta.
    """
    tool = gpu_stress_tool()
    if tool == "vkmark":
        return ["vkmark", "--size", "1920x1080",
                "-b", f"desktop:duration={seconds}"]
    if tool == "furmark":
        return ["furmark", "--demo", "furmark-gl",
                "--width", "1920", "--height", "1080",
                "--max-time", str(seconds),
                "--vsync", "0", "--no-gpumon"]
    return None
