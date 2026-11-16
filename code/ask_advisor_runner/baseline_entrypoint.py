"""Authenticated loopback chat forwarding for a plain Qwen benchmark run."""

import os
from pathlib import Path

from .audit import AppendOnlyJsonl
from .proxy_http import BaselineChatForwarder, OpenAIChatProxy
from .proxy_runtime import AuthenticatedProxyServer, StdlibVllmTransport
from .runtime_entrypoint import _port, _required_env, serve_until_stopped


def main() -> int:
    if os.environ.get("ASK_ADVISOR_ENABLE_RUNTIME") != "1":
        raise RuntimeError("runtime startup rejected: activation_disabled")
    transport = StdlibVllmTransport(
        base_url=_required_env("ASK_ADVISOR_VLLM_BASE_URL"), timeout_sec=1800.0
    )
    application = OpenAIChatProxy(
        interceptor=BaselineChatForwarder(_required_env("ASK_ADVISOR_MODEL")),
        upstream_chat_call=transport.chat,
        enabled=True,
    )
    server = AuthenticatedProxyServer(
        application=application,
        bind_host="127.0.0.1",
        bind_port=_port(_required_env("ASK_ADVISOR_PROXY_BIND_PORT")),
        bearer_token=_required_env("ASK_ADVISOR_PROXY_API_KEY"),
        delivery_record=AppendOnlyJsonl(Path(_required_env("ASK_ADVISOR_AUDIT_LOG"))).append,
        enabled=True,
    )
    return serve_until_stopped(server, transport)


if __name__ == "__main__":
    raise SystemExit(main())
