#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""setup.py per BUO — BC-250 Ultimate Orchestrator."""

from setuptools import find_packages, setup

with open("README.md", "r", encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="buo",
    version="1.0.0",
    description="BC-250 Ultimate Orchestrator — ottimizzazione automatica "
                "per ASRock BC-250",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="BC-250 Community",
    license="GPL-3.0-or-later",
    packages=find_packages(exclude=["tests", "tests.*"]),
    include_package_data=True,
    python_requires=">=3.10",
    install_requires=[
        "click>=8.0",
        "rich>=13.0",
        "pyyaml>=6.0",
    ],
    extras_require={
        "ml": ["numpy>=1.24", "scikit-learn>=1.0"],
        "dev": ["pytest>=7.0"],
    },
    entry_points={
        "console_scripts": [
            "buo=buo.cli:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: POSIX :: Linux",
        "Topic :: System :: Hardware",
    ],
)
