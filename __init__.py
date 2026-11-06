"""Hermes OpenCode ACP, using only Hermes's documented provider interface."""
from providers import register_provider
from providers.base import ProviderProfile

from .constants import DEFAULT_MODEL, FREE_MODELS, VERSION, opencode_command


class OpenCodeACPProfile(ProviderProfile):
    def create_client(self, **client_kwargs):
        from .acp_client import OpenCodeACPClient

        if not client_kwargs.get("command") and not client_kwargs.get("acp_command"):
            client_kwargs["command"] = opencode_command()
        if not client_kwargs.get("args") and not client_kwargs.get("acp_args"):
            client_kwargs["args"] = ["acp", "--pure"]
        return OpenCodeACPClient(**client_kwargs)

    def fetch_models(self, *, api_key=None, base_url=None, timeout=15.0):
        client = self.create_client()
        try:
            offered = set(client.list_models(timeout_seconds=timeout))
            return [model for model in FREE_MODELS if model in offered]
        except (OSError, RuntimeError, TimeoutError, ValueError):
            return None
        finally:
            client.close()

    def get_model_context_length(self, model):
        return 131072 if model in FREE_MODELS else None


profile = OpenCodeACPProfile(
    name="hermes-opencode-acp",
    display_name="Hermes OpenCode ACP",
    description=f"Real OpenCode CLI, Hermes-owned tools, curated free models (bridge {VERSION})",
    auth_type="external_process",
    api_mode="chat_completions",
    base_url="acp://hermes-opencode-acp",
    process_command=opencode_command(),
    process_args=("acp", "--pure"),
    process_command_env_vars=("HERMES_OPENCODE_ACP_COMMAND",),
    supports_health_check=False,
    fallback_models=FREE_MODELS,
    default_aux_model=DEFAULT_MODEL,
    model_capabilities={model: {
        "context_window": 131072,
        "supports_tools": True,
        "supports_vision": False,
        "supports_reasoning": False,
    } for model in FREE_MODELS},
)
register_provider(profile)
