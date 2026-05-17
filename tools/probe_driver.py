"""Probe the Whisplay driver so we know what to import and call.

Run on the Pi:
    cd ~/Whisplay/Driver && python3 ~/clawdmeter/probe_driver.py

Prints:
  - files in the driver dir
  - importable modules
  - public attributes of each module (classes, functions, constants)
  - if a likely "main" class is found, its public methods
"""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
import sys
from pathlib import Path

DRIVER_DIR = Path.home() / "Whisplay" / "Driver"
EXAMPLE_DIR = Path.home() / "Whisplay" / "example"

print(f"== driver dir: {DRIVER_DIR} ==")
for p in sorted(DRIVER_DIR.glob("*")):
    print(" ", p.name)

print(f"\n== example dir: {EXAMPLE_DIR} ==")
if EXAMPLE_DIR.exists():
    for p in sorted(EXAMPLE_DIR.glob("*")):
        print(" ", p.name)

sys.path.insert(0, str(DRIVER_DIR))

print("\n== importable python modules in driver dir ==")
py_modules = []
for info in pkgutil.iter_modules([str(DRIVER_DIR)]):
    py_modules.append(info.name)
    print(" ", info.name)

for mod_name in py_modules:
    print(f"\n== module: {mod_name} ==")
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:
        print(f"  import failed: {e}")
        continue
    public = [n for n in dir(mod) if not n.startswith("_")]
    for name in public:
        obj = getattr(mod, name)
        kind = type(obj).__name__
        print(f"  {name}: {kind}")
        if inspect.isclass(obj):
            methods = [m for m in dir(obj) if not m.startswith("_")]
            print(f"    methods: {methods}")
            try:
                sig = inspect.signature(obj.__init__)
                print(f"    __init__{sig}")
            except (TypeError, ValueError):
                pass
