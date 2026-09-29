"""Load the integration's pure modules without importing Home Assistant."""

import sys
import types
from pathlib import Path

PKG = Path(__file__).parent.parent / "custom_components" / "tvtracker"

pkg = types.ModuleType("tvt")
pkg.__path__ = [str(PKG)]
sys.modules["tvt"] = pkg
