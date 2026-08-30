#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Accesso SMN via PCI config space (helper condiviso).

Le operazioni SMN (System Management Network) passano dal PCI config
space del northbridge (0000:00:00.0): si scrive l'indirizzo del register
a 0xB8 e si legge il valore a 0xBC. Qui vive la primitiva di lettura
della core presence mask, usata sia da `buo/unlock/cpu.py` (unlock core)
sia da `buo/safety/reader.py` (letture di stato reali per `buo status`).
"""

import os
import struct

from ..constants import CORE_MASK_REG, PCI_CONFIG_PATH


def read_core_mask(pci_config_path: str = PCI_CONFIG_PATH) -> int:
    """Legge la core presence mask (SMN 0x5A870, byte basso).

    La lettura NON è fail-soft di proposito: alza un'eccezione (es.
    OSError) se il PCI config space non è accessibile, e sono i
    chiamanti a decidere la policy — CORE_MASK_STOCK in
    `buo/unlock/cpu.py` (hardware assente: assumi stock), None nel
    reader (`buo/safety/reader.py`: fail-soft, mai valori inventati).
    """
    fd = os.open(pci_config_path, os.O_RDWR)
    try:
        os.pwrite(fd, struct.pack("<I", CORE_MASK_REG), 0xB8)
        return struct.unpack("<I", os.pread(fd, 4, 0xBC))[0] & 0xFF
    finally:
        os.close(fd)
