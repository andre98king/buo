#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test dell'import lazy: il core funziona anche senza click/rich.

Esegue un subprocess con `python -S` (site-packages disabilitati) per
verificare che `import buo.constants` non richieda dipendenze CLI.
"""

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestLazyImport(unittest.TestCase):
    def test_core_importable_without_site_packages(self):
        code = (
            "import sys; sys.path.insert(0, '.')\n"
            "import buo.constants as c\n"
            "import buo.exceptions, buo.config\n"
            "import buo.state.checkpoint, buo.state.rollback\n"
            "import buo.models.vram_estimator\n"
            "import buo.audit.hardware\n"
            "print(c.CORE_MASK_UNLOCKED)\n"
        )
        result = subprocess.run(
            [sys.executable, "-S", "-c", code],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0,
                         msg=f"stdout={result.stdout!r} stderr={result.stderr!r}")
        self.assertIn("255", result.stdout)  # 0xFF

    def test_lazy_getattr(self):
        """In un interprete pulito, buo.cli è il gruppo click (callable)."""
        code = (
            "import sys; sys.path.insert(0, '.')\n"
            "import buo\n"
            "assert buo.__version__ == '1.2.0'\n"
            "assert callable(buo.Orchestrator)\n"
            "assert callable(buo.cli)\n"
            "from buo import cli\n"
            "assert callable(cli)\n"
            "try:\n"
            "    buo.cose_che_non_esistono\n"
            "    raise SystemExit('doveva sollevare AttributeError')\n"
            "except AttributeError:\n"
            "    pass\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0,
                         msg=f"stdout={result.stdout!r} stderr={result.stderr!r}")
        self.assertIn("OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
