#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Eccezioni personalizzate per BUO.

Gerarchia:
    BUOException (base)
    ├── SafetyViolation        — violazione di un hard limit
    ├── HardwareNotFound       — hardware/sysfs non accessibile
    ├── ScriptError            — errore di uno script esterno
    ├── CheckpointError        — errore salvataggio/ripristino checkpoint
    ├── RollbackError          — errore durante il rollback
    ├── TimeoutError           — timeout durante l'esecuzione
    ├── ConfigurationError     — configurazione non valida
    ├── UnsupportedDistro      — distribuzione non supportata
    └── BenchmarkError         — errore durante un benchmark
"""

from typing import Optional


class BUOException(Exception):
    """Eccezione base per BUO."""


class SafetyViolation(BUOException):
    """Violazione di un safety gate (hard limit)."""

    def __init__(self, message: str, reading: float = 0.0, limit: float = 0.0):
        super().__init__(message)
        self.reading = reading
        self.limit = limit

    def __str__(self) -> str:
        base = super().__str__()
        if self.reading and self.limit:
            return f"{base} (rilevato: {self.reading}, limite: {self.limit})"
        return base


class HardwareNotFound(BUOException):
    """Hardware o percorso sysfs non trovato / non accessibile."""


class ScriptError(BUOException):
    """Errore nell'esecuzione di uno script esterno della community."""

    def __init__(self, script: str, returncode: int,
                 stdout: str = "", stderr: str = ""):
        super().__init__(f"Script {script} fallito con codice {returncode}")
        self.script = script
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class CheckpointError(BUOException):
    """Errore nel salvataggio/ripristino di un checkpoint."""


class RollbackError(BUOException):
    """Errore durante il rollback."""


class TimeoutError(BUOException):
    """Timeout durante l'esecuzione di un comando o test."""


class ConfigurationError(BUOException):
    """Configurazione non valida o fuori dai limiti."""


class UnsupportedDistro(BUOException):
    """Distribuzione Linux non supportata per questa operazione."""


class BenchmarkError(BUOException):
    """Errore durante l'esecuzione di un benchmark."""

    def __init__(self, tool: str, message: str = ""):
        super().__init__(f"Benchmark '{tool}' fallito: {message}")
        self.tool = tool
