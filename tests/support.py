"""Load only the transport, so CI needs neither Hermes nor OpenCode installed."""
import importlib
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
NAME = "_hermes_opencode_acp_offline_tests"
package = types.ModuleType(NAME)
package.__path__ = [str(ROOT)]
sys.modules[NAME] = package
client = importlib.import_module(NAME + ".acp_client")
constants = importlib.import_module(NAME + ".constants")
launcher = importlib.import_module(NAME + ".launcher")
