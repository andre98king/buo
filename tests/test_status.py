#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test del comando `buo status` (fix C1: mai dati mock su hardware reale).

Coprono:
    • `buo status` senza --mock legge l'hardware REALE (RealHardwareReader),
      mai MockHardware;
    • `buo status --mock` resta simulato (MockHardware);
    • state dir NON di sistema → avviso fail-soft, mai crash (il warning
      segue state_dir(), non l'euid);
    • lettura hardware che solleva → avviso fail-soft, mai crash.

Tutte le letture hardware sono patchate: i test non toccano mai hardware
reale né /sys/class/hwmon.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from buo.cli import cli


class _FakeReader:
    """Reader reale simulato: stessa interfaccia di RealHardwareReader.

    Valori fissi e controllabili; None = sensore non leggibile
    (fail-soft: mai inventare valori).
    """

    def __init__(self, **values):
        self._v = {
            "cpu_temp": 42.5,
            "gpu_temp": 51.0,
            "gpu_voltage": 1045,
            "gpu_power": 90.0,
            "cpu_vid": None,
            "total_power": None,
        }
        self._v.update(values)

    def get_cpu_temp(self):
        return self._v["cpu_temp"]

    def get_gpu_temp(self):
        return self._v["gpu_temp"]

    def get_gpu_voltage(self):
        return self._v["gpu_voltage"]

    def get_gpu_power(self):
        return self._v["gpu_power"]

    def get_cpu_vid(self):
        return self._v["cpu_vid"]

    def get_total_power(self):
        return self._v["total_power"]

    def get_system_info(self):
        """Stessa forma di MockHardware.get_system_info(); ciò che hwmon
        non espone è None (la CLI mostra 'non rilevabile')."""
        return {
            "core_mask": None,
            "cpu_cores": None,
            "cpu_freq": None,
            "cpu_vid": self.get_cpu_vid(),
            "cpu_temp": self.get_cpu_temp(),
            "gpu_cu": None,
            "gpu_freq": None,
            "gpu_voltage": self.get_gpu_voltage(),
            "gpu_temp": self.get_gpu_temp(),
            "gpu_power": self.get_gpu_power(),
            "total_power": self.get_total_power(),
            "ambient_temp": None,
            "fan_speed": None,
            "is_undervolted": None,
            "is_overclocked": None,
            "is_40cu_enabled": None,
            "is_acpi_fixed": None,
            "is_tlb_fixed": None,
            "is_ace_fixed": None,
            "iommu_off": None,
            "reboot_count": None,
        }


class TestStatusCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name
        os.environ["BUO_DEPS_DIR"] = self._tmp.name
        self.runner = CliRunner()

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        os.environ.pop("BUO_DEPS_DIR", None)
        self._tmp.cleanup()

    def _invoke(self, args):
        return self.runner.invoke(cli, args)

    def test_status_real_uses_real_reader(self):
        """Senza --mock: valori dal reader REALE, mai dati MockHardware."""
        with mock.patch("buo.safety.reader.RealHardwareReader",
                        return_value=_FakeReader(cpu_temp=42.5)):
            result = self._invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("42.5°C", result.output)  # temp reale (reader)
        self.assertIn("non rilevabile", result.output)  # fail-soft su None
        self.assertNotIn("45.0°C", result.output)       # mai la temp mock
        self.assertNotIn("6/8", result.output)          # mai i core mock

    def test_status_mock_uses_mock(self):
        """Con --mock: valori simulati (MockHardware), comportamento invariato."""
        result = self._invoke(["status", "--mock"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("6/8", result.output)         # default mock: 6 core
        self.assertIn("24/40", result.output)       # default mock: 24 CU
        self.assertNotIn("sudo buo status", result.output)  # niente avviso

    def test_status_non_system_state_dir_shows_warning(self):
        """State dir NON di sistema (BUO_STATE_DIR in test): avviso
        fail-soft, mai crash — l'avviso segue state_dir(), non l'euid."""
        with mock.patch("buo.safety.reader.RealHardwareReader",
                        return_value=_FakeReader()):
            result = self._invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("sudo buo status", result.output)  # avviso fail-soft
        self.assertIn("Fase corrente: init", result.output)

    def test_status_system_state_dir_no_warning(self):
        """State dir di sistema (/var/lib/buo): NESSUN avviso, anche se
        l'utente non è root — il warning dipende dallo state dir risolto."""
        with mock.patch("buo.safety.reader.RealHardwareReader",
                        return_value=_FakeReader()), \
                mock.patch("buo.utils.paths.state_dir",
                           return_value=Path("/var/lib/buo")):
            result = self._invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn("sudo buo status", result.output)

    def test_status_reader_error_fail_soft(self):
        """Reader che solleva (es. /sys non leggibile): avviso fail-soft
        e uscita pulita, mai crash con exit 1."""
        with mock.patch("buo.safety.reader.RealHardwareReader",
                        side_effect=OSError("hwmon non leggibile")):
            result = self._invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Lettura hardware non riuscita", result.output)


if __name__ == "__main__":
    unittest.main()
