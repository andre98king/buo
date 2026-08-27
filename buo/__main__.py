#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Entry point per BUO: `python -m buo`.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
