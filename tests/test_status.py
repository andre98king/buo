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

    Valori fissi e controllabili (default realistici: core sbloccati,
    40 CU, SoC ~58 W…); None = sensore non leggibile (fail-soft: mai
    inventare valori). I campi di stato non sensore restano None.
    """

    def __init__(self, **values):
        self._v = {
            "core_mask": "0xFF",
            "cpu_cores": 8,
            "cpu_freq": 3500,
            "cpu_vid": 993,
            "cpu_temp": 42.5,
            "gpu_cu": 40,
            "gpu_freq": 1500,
            "gpu_voltage": 1045,
            "gpu_temp": 51.0,
            "gpu_power": 90.0,
            "total_power": 57.8,
            "ambient_temp": 46.0,
            "fan_speed": 2090,
            "is_40cu_enabled": True,
        }
        self._v.update(values)

    def get_core_mask(self):
        return self._v["core_mask"]

    def get_cpu_cores(self):
        return self._v["cpu_cores"]

    def get_cpu_freq(self):
        return self._v["cpu_freq"]

    def get_cpu_vid(self):
        return self._v["cpu_vid"]

    def get_cpu_temp(self):
        return self._v["cpu_temp"]

    def get_gpu_cu(self):
        return self._v["gpu_cu"]

    def get_gpu_freq(self):
        return self._v["gpu_freq"]

    def get_gpu_voltage(self):
        return self._v["gpu_voltage"]

    def get_gpu_temp(self):
        return self._v["gpu_temp"]

    def get_gpu_power(self):
        return self._v["gpu_power"]

    def get_total_power(self):
        return self._v["total_power"]

    def get_ambient_temp(self):
        return self._v["ambient_temp"]

    def get_fan_speed(self):
        return self._v["fan_speed"]

    def get_is_40cu_enabled(self):
        return self._v["is_40cu_enabled"]

    def get_system_info(self):
        """Stessa forma di MockHardware.get_system_info(); ogni sensore
        non leggibile è None (la CLI mostra 'non rilevabile')."""
        return {
            "core_mask": self.get_core_mask(),
            "cpu_cores": self.get_cpu_cores(),
            "cpu_freq": self.get_cpu_freq(),
            "cpu_vid": self.get_cpu_vid(),
            "cpu_temp": self.get_cpu_temp(),
            "gpu_cu": self.get_gpu_cu(),
            "gpu_freq": self.get_gpu_freq(),
            "gpu_voltage": self.get_gpu_voltage(),
            "gpu_temp": self.get_gpu_temp(),
            "gpu_power": self.get_gpu_power(),
            "total_power": self.get_total_power(),
            "ambient_temp": self.get_ambient_temp(),
            "fan_speed": self.get_fan_speed(),
            "is_undervolted": None,
            "is_overclocked": None,
            "is_40cu_enabled": self.get_is_40cu_enabled(),
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
        """Senza --mock: valori dal reader REALE, mai dati MockHardware.

        fan_speed=None mantiene coperto il fail-soft (la CLI mostra
        'non rilevabile' per quel sensore, mai un valore inventato)."""
        with mock.patch("buo.safety.reader.RealHardwareReader",
                        return_value=_FakeReader(cpu_temp=42.5,
                                                 fan_speed=None)):
            result = self._invoke(["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("42.5°C", result.output)  # temp reale (reader)
        self.assertIn("non rilevabile", result.output)  # fail-soft su None
        self.assertNotIn("45.0°C", result.output)       # mai la temp mock
        self.assertNotIn("6/8", result.output)          # mai i core mock
        self.assertNotIn("24/40", result.output)        # mai le CU mock

    def test_status_mock_uses_mock(self):
        """Con --mock: valori simulati (MockHardware), comportamento invariato."""
        result = self._invoke(["status", "--mock"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("6/8", result.output)         # default mock: 6 core
        self.assertIn("24/40", result.output)       # default mock: 24 CU
        # Righe nuove: valori di MockHardwareState (mock.py) — nessun
        # impatto del percorso --mock (il change non lo tocca).
        self.assertIn("0x77", result.output)        # core_mask stock mock
        self.assertIn("1206 mV", result.output)     # CPU VID mock (state)
        self.assertIn("1050 mV", result.output)     # GPU Volt mock (state)
        self.assertIn("1500 MHz", result.output)    # GPU Freq mock (state)
        self.assertIn("22.0°C", result.output)      # Ambiente mock (state)
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
