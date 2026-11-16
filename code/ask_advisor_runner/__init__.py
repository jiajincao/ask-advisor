"""Offline-only stage-0 skeleton for the Ask-Advisor experiment runner."""

from .config import ExperimentConfig
from .bridge import (
    ApprovalDecision,
    ApprovalRequest,
    RestrictedApprovalAnswerer,
    canonical_request_hash,
)
from .runner import OfflineRunner
from .dsh_events import AuditChain, OfflineDshEventAdapter, SimulatedDshEvent
from .dsh_readonly import FollowSnapshotSummary, ReadonlyDshAdapter, RootSessionCandidate
from .dsh_transport import HttpJsonResponse, ReadonlyDshTransportExecutor
from .dsh_secure_transport import AuthenticatedDshSession, SecureReadonlyDshClient
from .dsh_browser_session import (
    BrowserAuthenticatedDshSession,
    make_browser_readonly_client,
)
from .dsh_runtime_transport import (
    JsonWebSocketConnection,
    OneShotBootstrapUrlProvider,
    make_json_websocket_dialer,
)
from .dsh_managed_process import ManagedDshProcess
from .dsh_stdlib_websocket import StdlibRawWebSocket, dial_loopback_websocket
from .dsh_session_identity import (
    SessionIdentityPin,
    VerifiedSessionIdentity,
    create_session_identity_pin,
    verify_session_identity,
)
from .dsh_session_pin_store import SessionPinStore
from .dsh_candidate_confirmation import (
    AnonymousSessionCandidate,
    SessionCandidateConfirmation,
)
from .dsh_one_shot_bridge import (
    OneShotPromptGate,
    OneShotPromptGrant,
    RestrictedApprovalDispatcher,
    prompt_sha256,
)
from .dsh_approval_transport import ApprovalPresentation, OneShotApprovalChannel
from .dsh_one_shot_flow import AdvisorResult, OneShotAdvisorFlow, OneShotFlowResult
from .dsh_prompt_transport import OneShotPromptTransportExecutor
from .dsh_tool_call_resolver import OneShotToolCallResolver
from .dsh_advisor_result_reader import OneShotAdvisorResultReader
from .dsh_systemone_bridge import (
    DshSystemOneBridge,
    DshSystemOneBridgeResult,
    SystemOneGateResult,
)
from .chat_interceptor import (
    ChatRequestInterceptor,
    InterceptionResult,
    ToolObservation,
)
from .harbor_integration import HarborQwenProxyConfig
from .proxy_http import (
    DisabledProxyError,
    OpenAIChatProxy,
    ProxyExchange,
    ProxyResponse,
    ProxyStream,
)
from .proxy_runtime import AuthenticatedProxyServer, StdlibVllmTransport
from .runtime_composition import (
    AdvisorInvocation,
    DeploymentCheck,
    RuntimeBridgeCoordinator,
    check_deployment_environment,
)

__all__ = [
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalPresentation",
    "AdvisorResult",
    "AnonymousSessionCandidate",
    "AuthenticatedDshSession",
    "BrowserAuthenticatedDshSession",
    "AuditChain",
    "ExperimentConfig",
    "FollowSnapshotSummary",
    "HttpJsonResponse",
    "JsonWebSocketConnection",
    "ManagedDshProcess",
    "OfflineRunner",
    "OneShotPromptGate",
    "OneShotPromptTransportExecutor",
    "OneShotToolCallResolver",
    "OneShotApprovalChannel",
    "OneShotAdvisorFlow",
    "OneShotAdvisorResultReader",
    "OneShotFlowResult",
    "OneShotPromptGrant",
    "OneShotBootstrapUrlProvider",
    "OfflineDshEventAdapter",
    "ReadonlyDshAdapter",
    "ReadonlyDshTransportExecutor",
    "RestrictedApprovalAnswerer",
    "RestrictedApprovalDispatcher",
    "RootSessionCandidate",
    "SecureReadonlyDshClient",
    "SessionIdentityPin",
    "SessionCandidateConfirmation",
    "SessionPinStore",
    "StdlibRawWebSocket",
    "VerifiedSessionIdentity",
    "SimulatedDshEvent",
    "canonical_request_hash",
    "create_session_identity_pin",
    "make_browser_readonly_client",
    "make_json_websocket_dialer",
    "prompt_sha256",
    "verify_session_identity",
    "dial_loopback_websocket",
    "DshSystemOneBridge",
    "DshSystemOneBridgeResult",
    "SystemOneGateResult",
    "ChatRequestInterceptor",
    "ProxyStream",
    "HarborQwenProxyConfig",
    "InterceptionResult",
    "ToolObservation",
    "DisabledProxyError",
    "OpenAIChatProxy",
    "ProxyExchange",
    "ProxyResponse",
    "AuthenticatedProxyServer",
    "StdlibVllmTransport",
    "AdvisorInvocation",
    "DeploymentCheck",
    "RuntimeBridgeCoordinator",
    "check_deployment_environment",
]
