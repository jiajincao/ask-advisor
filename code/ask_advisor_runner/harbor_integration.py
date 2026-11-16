from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HarborQwenProxyConfig:
    """Credential-free Harbor agent configuration for the future proxy.

    The proxy URL is supplied only at runtime and is intentionally not stored
    in this object, logs, or generated config.  Harbor's built-in qwen-coder
    adapter is sufficient once its OpenAI-compatible base URL points at the
    separately controlled interceptor.
    """

    model_name: str
    api_key_env_name: str = "ASK_ADVISOR_PROXY_API_KEY"

    def __post_init__(self) -> None:
        if not isinstance(self.model_name, str) or not self.model_name.strip():
            raise ValueError("Harbor integration rejected: invalid_model_name")
        if self.api_key_env_name != "ASK_ADVISOR_PROXY_API_KEY":
            raise ValueError("Harbor integration rejected: unexpected_api_key_env")

    def agent_config(self, *, runtime_base_url: str) -> dict[str, Any]:
        if not isinstance(runtime_base_url, str) or not runtime_base_url.strip():
            raise ValueError("Harbor integration rejected: runtime_base_url_required")
        if not runtime_base_url.startswith("http://"):
            raise ValueError("Harbor integration rejected: http_runtime_proxy_required")
        return {
            "name": "qwen-coder",
            "model_name": self.model_name,
            "base_url": runtime_base_url,
            "model_api_key_name": self.api_key_env_name,
            "model_api_format": "openai-chat-completions",
            "kwargs": {"disable_web_search": True},
        }
