#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
BC-250 Ultimate Orchestrator (BUO)
==================================

Ottimizzazione automatica per ASRock BC-250 — un solo comando, il massimo
per la tua scheda. Analizza, sblocca, ottimizza e risolve tutti i problemi
noti della BC-250 in modo automatico e sicuro.

Comando principale:
    sudo buo unleash
"""

__version__ = "1.0.0"
__author__ = "BC-250 Community"

# Import LAZY (PEP 562): `import buo` funziona anche senza le dipendenze
# della CLI (click/rich) — il core (orchestratore, safety, stato, modelli)
# è utilizzabile come libreria senza dipendenze opzionali.

__all__ = ["Orchestrator", "cli", "__version__"]


def __getattr__(name):
    if name == "Orchestrator":
        from .orchestrator import Orchestrator
        value = Orchestrator
    elif name == "cli":
        from .cli import cli
        value = cli
    else:
        raise AttributeError(f"module 'buo' has no attribute {name!r}")
    # Fissa l'attributo nel package: l'import del sottomodulo (.cli)
    # non deve ombreggiare il valore (gruppo click) con il modulo
    globals()[name] = value
    return value
