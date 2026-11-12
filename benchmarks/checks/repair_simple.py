"""Trusted scorer, deliberately outside the writable fixture."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('fixture', Path.cwd() / 'arithmetic.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.add(2, 3) == 5
assert module.add(-7, 3) == -4
assert module.add(0, 0) == 0
