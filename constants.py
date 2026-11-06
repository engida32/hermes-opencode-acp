"""Non-secret release settings; no discovery, network, or filesystem writes."""
from pathlib import Path

from .launcher import resolve_opencode

VERSION = "0.4.0"
PROVIDER = "hermes-opencode-acp"
DEFAULT_MODEL = "opencode/glm-5.3-flash-free"
# Explicitly curated free IDs, never arbitrary strings ending in '-free'.
# ACP discovery intersects this list; inference checks live availability again.
FREE_MODELS = (
    DEFAULT_MODEL,
    "opencode/deepseek-v4.1-flash-free",
    "opencode/mimo-v2.6-flash-free",
    "opencode/nemotron-3.5-super-vl-free",
    "opencode/nemotron-3.5-lightning-free",
    "opencode/beam-free",
    "opencode/big-pickle",
)
WORKSPACE = Path(__file__).resolve().parent / "workspace"


def opencode_command():
    """Resolve an operator-owned executable, never a model-supplied command."""
    return resolve_opencode()
