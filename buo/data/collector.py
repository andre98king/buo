#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Data Collector — raccolta di campioni per il modello VRAM (ML).

Dal design (messaggi 53-54): "Iniziamo il machine learning usando grandi
somme di dati per avere una stima sempre più precisa". Ogni campione
contiene le feature lette dai sensori + (opzionale) la temperatura VRAM
reale misurata con un sensore fisico (termocoppia USB su /dev/ttyUSB0).

I campioni sono salvati in formato JSONL in
<state_dir>/dataset/vram_dataset.jsonl — pronti per `buo ml-train`.

PRIVACY: i dati sono anonimizzati (nessun identificativo) e caricati
solo con `buo data-upload` (federated learning, esplicitamente).
"""

import csv
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils.logging import LoggerMixin
from ..utils.paths import state_dir

DATASET_REL = Path("dataset") / "vram_dataset.jsonl"


def dataset_path() -> Path:
    """Percorso del dataset VRAM (JSONL)."""
    return state_dir() / DATASET_REL


class VRAMDataCollector(LoggerMixin):
    """Raccoglie e salva campioni per il training del modello VRAM."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 vram_sensor: Optional[str] = None):
        self.mock = mock
        self.hardware = mock_hardware
        self.vram_sensor = vram_sensor

    # ------------------------------------------------------------------ #

    def collect_one(self) -> Dict[str, Any]:
        """Raccoglie un singolo campione."""
        sample: Dict[str, Any] = {}

        if self.mock and self.hardware is not None:
            hw = self.hardware
            sample = {
                "gpu_temp": hw.get_gpu_temp(),
                "gpu_power": hw.get_gpu_power(),
                "gpu_utilization": hw.get_gpu_utilization(),
                "cpu_temp": hw.get_cpu_temp(),
                "gpu_freq": hw.get_gpu_freq(),
                "gpu_voltage": hw.get_gpu_voltage(),
                "fan_speed": hw.get_fan_speed(),
                "ambient_temp": hw.get_ambient_temp(),
                "cpu_freq": hw.get_cpu_freq(),
                "cooling_type": "push-pull",
                "heatsink_on_vram": False,
            }
        else:
            from ..audit.hardware import HardwareAudit
            audit = HardwareAudit(mock=False)
            temps = audit._audit_temps()
            cpu = audit._audit_cpu()
            gpu = audit._audit_gpu()
            sample = {
                "gpu_temp": temps.get("gpu_temp"),
                "gpu_power": None,
                "gpu_utilization": None,
                "cpu_temp": temps.get("cpu_temp"),
                "gpu_freq": None,
                "gpu_voltage": None,
                "fan_speed": None,
                "ambient_temp": temps.get("ambient"),
                "cpu_freq": None,
                "gpu_cu": gpu.get("cu_count"),
                "cpu_cores": cpu.get("cores"),
                "cooling_type": "unknown",
                "heatsink_on_vram": False,
            }

        # Temperatura VRAM reale (sensore fisico opzionale)
        vram_real = self._read_vram_sensor()
        if vram_real is not None:
            sample["vram_temp_real"] = vram_real

        sample["timestamp"] = datetime.now().isoformat()
        sample["anonymized"] = True
        return sample

    def collect(self, samples: int = 10, interval: float = 1.0) -> int:
        """Raccoglie `samples` campioni a intervalli di `interval` secondi."""
        path = dataset_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0

        self.logger.info("📥 Raccolta dati: %d campioni (intervallo %.1fs) → %s",
                         samples, interval, path)

        with open(path, "a", encoding="utf-8") as f:
            for i in range(samples):
                sample = self.collect_one()
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")
                written += 1
                self.logger.info("   [%d/%d] GPU %.1f°C | CPU %.1f°C%s",
                                 i + 1, samples,
                                 sample.get("gpu_temp") or 0,
                                 sample.get("cpu_temp") or 0,
                                 (f" | VRAM real "
                                  f"{sample['vram_temp_real']:.1f}°C")
                                 if "vram_temp_real" in sample else "")
                if i < samples - 1 and interval > 0:
                    time.sleep(interval)

        return written

    # ------------------------------------------------------------------ #

    def _read_vram_sensor(self) -> Optional[float]:
        """Legge la temperatura VRAM da un sensore seriale (termocoppia)."""
        if not self.vram_sensor:
            return None
        try:
            import serial  # pyserial opzionale
            with serial.Serial(self.vram_sensor, 9600, timeout=1) as ser:
                line = ser.readline().decode(errors="ignore").strip()
            return float(line)
        except ImportError:
            self.logger.warning("pyserial non installato — sensore "
                                "ignorato (pip install pyserial)")
            return None
        except Exception as e:
            self.logger.warning("Lettura sensore VRAM fallita: %s", e)
            return None

    # ------------------------------------------------------------------ #

    @staticmethod
    def load_dataset(path: Optional[Path] = None) -> List[Dict[str, Any]]:
        """Carica i campioni salvati (JSONL)."""
        path = path or dataset_path()
        rows: List[Dict[str, Any]] = []
        if not path.exists():
            return rows
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return rows

    @staticmethod
    def to_csv(rows: List[Dict[str, Any]], path: Path) -> int:
        """Esporta il dataset in CSV (per analisi/condivisione)."""
        if not rows:
            return 0
        fields = list(rows[0].keys())
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)
