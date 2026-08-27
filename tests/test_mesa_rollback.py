#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test del rilevamento Mesa (bug sul campo: "Mesa 4.6" letto da
"OpenGL version string: 4.6 (Compatibility Profile) Mesa 25.2.4")
e del rollback "solo applicati" (niente rumore su livelli mai toccati).
"""

import unittest

from buo.audit.hardware import HardwareAudit
from buo.audit.problems import ProblemDetector
from buo.state.rollback import RollbackManager
from buo.constants import ROLLBACK_ORDER


class TestMesaDetection(unittest.TestCase):
    def test_parses_mesa_not_opengl(self):
        """Bug sul campo: 4.6 è OpenGL, non Mesa."""
        out = ("OpenGL version string: 4.6 (Compatibility Profile) "
               "Mesa 25.2.4")
        raw = HardwareAudit._parse_mesa_string(out)
        self.assertEqual(raw, "25.2.4")

    def test_version_meets_minimum(self):
        out = "OpenGL version string: 4.6 Mesa 25.2.4"
        raw = HardwareAudit._parse_mesa_string(out)
        # il metodo di versione usa la stessa regex
        import re
        m = re.search(r"(\d+)\.(\d+)", raw)
        version = tuple(int(x) for x in m.groups())
        from buo.constants import MESA_MIN
        self.assertGreaterEqual(version, MESA_MIN)

    def test_plain_opengl_string_fallback(self):
        out = "OpenGL version string: 4.6"
        raw = HardwareAudit._parse_mesa_string(out)
        self.assertEqual(raw, "4.6")

    def test_empty_returns_none(self):
        self.assertIsNone(HardwareAudit._parse_mesa_string(""))

    def test_detector_no_mesa_old_when_version_none(self):
        """Bug #13: sessione headless → version=None non è 'vecchia',
        non deve comparire il problema mesa_old."""
        det = ProblemDetector(mock=True)
        audit = {"mesa": {"version": None, "meets_minimum": False}}
        ids = [p["id"] for p in det.detect(audit)]
        self.assertNotIn("mesa_old", ids)

    def test_detector_mesa_old_when_version_old(self):
        """Mesa leggibile ma vecchia → mesa_old segnalato."""
        det = ProblemDetector(mock=True)
        audit = {"mesa": {"version": "24.3", "meets_minimum": False}}
        ids = [p["id"] for p in det.detect(audit)]
        self.assertIn("mesa_old", ids)


class TestRollbackAppliedOnly(unittest.TestCase):
    def test_only_applied_levels_executed(self):
        manager = RollbackManager(mock=True)
        executed = []
        for level in ROLLBACK_ORDER:
            manager.register(level, lambda l=level: executed.append(l) or True)

        ok = manager.rollback(applied={"gpu_40cu", "acpi_fix"})
        self.assertTrue(ok)
        self.assertEqual(sorted(executed), sorted(["gpu_40cu", "acpi_fix"]))

    def test_empty_applied_means_nothing_to_do(self):
        manager = RollbackManager(mock=True)
        executed = []
        for level in ROLLBACK_ORDER:
            manager.register(level, lambda l=level: executed.append(l) or True)
        ok = manager.rollback(applied=set())
        self.assertTrue(ok)
        self.assertEqual(executed, [])

    def test_full_rollback_when_applied_none(self):
        """Rollback manuale (applied=None) → cascata completa."""
        manager = RollbackManager(mock=True)
        executed = []
        for level in ROLLBACK_ORDER:
            manager.register(level, lambda l=level: executed.append(l) or True)
        manager.rollback(applied=None)
        self.assertEqual(executed, ROLLBACK_ORDER)


if __name__ == "__main__":
    unittest.main()
