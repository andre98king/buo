#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
BaseWrapper — classe base per i wrapper degli script esterni.

Gestisce esecuzione con sudo, timeout, cattura dell'output e parsing.
Le sottoclassi implementano `parse_output` per il formato specifico
di ogni script (confermato durante lo studio del codice sorgente).
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...exceptions import ScriptError
from ...utils.logging import LoggerMixin
from ...utils.shell import run_command


class BaseWrapper(LoggerMixin):
    """Classe base per l'esecuzione di script esterni."""

    def __init__(self, script_path: str, timeout: int = 60):
        self.script_path = Path(script_path)
        self.timeout = timeout

    # ------------------------------------------------------------------ #

    @property
    def available(self) -> bool:
        """True se lo script esterno esiste ed è eseguibile."""
        return self.script_path.exists() and self.script_path.is_file()

    def run(self, args: Optional[List[str]] = None,
            timeout: Optional[int] = None,
            check_returncode: bool = True,
            sudo: bool = True) -> Tuple[int, str, str]:
        """
        Esegue lo script esterno.

        Returns:
            (returncode, stdout, stderr)

        Raises:
            ScriptError: se lo script non esiste o fallisce (con check).
        """
        if not self.available:
            raise ScriptError(self.script_path.name, -1, "",
                              f"Script non trovato: {self.script_path}")

        timeout = timeout or self.timeout
        cmd = [str(self.script_path)] + (args or [])
        rc, stdout, stderr = run_command(cmd, timeout=timeout, sudo=sudo)

        if check_returncode and rc != 0:
            raise ScriptError(self.script_path.name, rc, stdout, stderr)

        return rc, stdout, stderr

    def run_with_output(self, args: Optional[List[str]] = None,
                        **kwargs) -> Dict[str, Any]:
        """Esegue e restituisce un dict con output già parsato."""
        rc, stdout, stderr = self.run(args, **kwargs)
        return {
            "returncode": rc,
            "stdout": stdout,
            "stderr": stderr,
            "parsed_output": self.parse_output(stdout, stderr),
        }

    def parse_output(self, stdout: str, stderr: str) -> Dict[str, Any]:
        """Da sovrascrivere nelle sottoclassi."""
        return {"stdout": stdout, "stderr": stderr}
