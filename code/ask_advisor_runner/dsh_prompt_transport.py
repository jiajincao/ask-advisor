from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

from .dsh_transport import HttpJsonResponse


HttpPost = Callable[[str, Mapping[str, Any]], HttpJsonResponse]
IdFactory = Callable[[], str]


class OneShotPromptTransportExecutor:
    """Strict HTTP executor for one reviewed DSH `session/prompt` call."""

    _ENDPOINT = "session/prompt"
    _PATH = "/api/session/prompt"

    def __init__(
        self,
        *,
        http_post: HttpPost,
        id_factory: IdFactory | None = None,
    ) -> None:
        if not callable(http_post):
            raise TypeError("http_post must be callable")
        self._http_post = http_post
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._used = False

    def _new_id(self) -> str:
        value = self._id_factory()
        if not isinstance(value, str) or not value:
            raise ValueError("DSH prompt transport rejected: invalid_ephemeral_id")
        return value

    @staticmethod
    def _valid_args(args: object) -> bool:
        if not isinstance(args, dict) or set(args) != {
            "sessionId",
            "requestId",
            "mode",
            "content",
        }:
            return False
        content = args.get("content")
        return (
            isinstance(args.get("sessionId"), str)
            and bool(args["sessionId"])
            and isinstance(args.get("requestId"), str)
            and bool(args["requestId"])
            and args.get("mode") == "queue"
            and isinstance(content, list)
            and len(content) == 1
            and isinstance(content[0], dict)
            and set(content[0]) == {"type", "text"}
            and content[0].get("type") == "text"
            and isinstance(content[0].get("text"), str)
            and bool(content[0]["text"].strip())
        )

    def __call__(self, endpoint: str, args: dict[str, Any]) -> Mapping[str, Any]:
        if self._used:
            raise RuntimeError("DSH prompt transport rejected: transport_replayed")
        if endpoint != self._ENDPOINT or not self._valid_args(args):
            raise RuntimeError("DSH prompt transport rejected: request_not_allowed")
        self._used = True
        rpc_id = self._new_id()
        response = self._http_post(
            self._PATH,
            {
                "type": "client-request",
                "rpcId": rpc_id,
                "method": self._ENDPOINT,
                "payload": {"args": {"request": args}},
            },
        )
        if response.status != 200:
            raise RuntimeError("DSH prompt transport failed: http_status_not_ok")
        body = response.body
        if not isinstance(body, Mapping) or body.get("rpcId") != rpc_id:
            raise RuntimeError("DSH prompt transport failed: rpc_id_mismatch")
        result = body.get("result")
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise RuntimeError("DSH prompt transport failed: rpc_result_not_ok")
        value = result.get("value")
        if not isinstance(value, Mapping) or value.get("accepted") is not True:
            raise RuntimeError("DSH prompt transport failed: prompt_not_accepted")
        return {"accepted": True}

