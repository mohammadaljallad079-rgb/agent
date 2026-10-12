"""Phase 5: real local model runtime.

Two layers are tested here:

* **Unit layer** (always runs): the pieces that turn "a model on disk" into a
  routable provider — hardware gating, model security, the descriptor registry,
  fail-closed routing, ``<tool_call>`` parsing and the gateway's local/remote
  accounting. These use a deterministic backend double, never a real model.
* **Integration layer** (opt-in, ``@pytest.mark.local_model``): real inference
  through ``llama-cpp-python`` against a GGUF file named by ``AAP_TEST_MODEL_PATH``.
  English and Arabic are both exercised, with real token accounting and real
  tool-call emission. Skipped by design when no model file is present.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from arabic_agent.core.config import Settings
from arabic_agent.core.enums import (
    DataSensitivity,
    ModelCapability,
    ModelLocation,
    OperatingMode,
    PlatformKind,
)
from arabic_agent.core.errors import (
    ConfigurationError,
    PermissionDeniedError,
    ValidationError,
)
from arabic_agent.local.hardware import (
    HardwareProfile,
    ResourceRequirement,
    can_run,
    detect_hardware,
)
from arabic_agent.local.inference import (
    LlamaCppBackend,
    LocalInferenceBackend,
    _split_streamable,
    parse_tool_calls,
)
from arabic_agent.local.model import (
    LocalBackendProvider,
    build_backend,
    discover_local_model,
)
from arabic_agent.local.model_registry import (
    KNOWN_BACKENDS,
    LocalModelDescriptor,
    ModelRegistry,
    model_logical_id,
    resource_requirement_from_file,
)
from arabic_agent.local.model_security import (
    ensure_within_storage,
    resolve_storage_dir,
    safe_load_config,
    verify_model_file,
)
from arabic_agent.models.base import (
    ChatMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    Usage,
)
from arabic_agent.models.gateway import ModelGateway
from arabic_agent.models.registry import ProviderRegistry
from arabic_agent.models.routing import ModelRouter, RoutingPolicy

MODEL_PATH = os.environ.get("AAP_TEST_MODEL_PATH", "")
requires_model = pytest.mark.skipif(
    not MODEL_PATH or not Path(MODEL_PATH).is_file(),
    reason="AAP_TEST_MODEL_PATH not set to a readable GGUF file",
)


# --------------------------------------------------------------------------
# Backend double: satisfies the LocalInferenceBackend protocol deterministically.
# --------------------------------------------------------------------------
class FakeBackend:
    """A deterministic in-process backend; never loads a real model."""

    name = "fake"

    def __init__(self, *, response: ModelResponse | None = None) -> None:
        self._response = response or ModelResponse(
            message=ChatMessage.assistant("ok"),
            model="fake",
            usage=Usage(prompt_tokens=1, completion_tokens=1),
        )
        self.loaded = False
        self.closed = False
        self.requests: list[ModelRequest] = []

    async def load(self) -> None:
        self.loaded = True

    async def generate(self, request: ModelRequest, *, timeout_seconds: float) -> ModelResponse:
        self.requests.append(request)
        return self._response

    async def stream(self, request: ModelRequest, *, timeout_seconds: float):
        yield self._response  # type: ignore[misc]

    async def healthy(self) -> bool:
        return self.loaded and not self.closed

    async def close(self) -> None:
        self.closed = True

    def model_info(self) -> dict[str, object]:
        return {"backend": self.name}


def _descriptor(**overrides: object) -> LocalModelDescriptor:
    base: dict[str, object] = {
        "name": "local",
        "backend": "llama_cpp",
        "model_path": "/tmp/model.gguf",
        "model_id": "model.gguf",
        "capabilities": frozenset({ModelCapability.CHAT, ModelCapability.TOOL_CALLING}),
    }
    base.update(overrides)
    return LocalModelDescriptor(**base)


# --------------------------------------------------------------------------
# Hardware gating
# --------------------------------------------------------------------------
def test_hardware_detection_reports_real_values():
    profile = detect_hardware()
    assert profile.cpu_count >= 1
    assert profile.total_ram_bytes is None or profile.total_ram_bytes > 0


def test_can_run_rejects_insufficient_ram():
    profile = HardwareProfile(
        platform=PlatformKind.LINUX,
        architecture="x86_64",
        cpu_count=4,
        cpu_model="test",
        total_ram_bytes=2 * 2**30,
        available_ram_bytes=2 * 2**30,
        gpus=(),
        disk_free_bytes=100 * 2**30,
    )
    verdict = can_run(profile, ResourceRequirement(min_ram_bytes=8 * 2**30))
    assert verdict.runnable is False
    assert "ram" in verdict.reason.lower()


def test_can_run_accepts_sufficient_resources():
    profile = HardwareProfile(
        platform=PlatformKind.LINUX,
        architecture="x86_64",
        cpu_count=8,
        cpu_model="test",
        total_ram_bytes=32 * 2**30,
        available_ram_bytes=16 * 2**30,
        gpus=(),
        disk_free_bytes=100 * 2**30,
    )
    verdict = can_run(profile, ResourceRequirement(min_ram_bytes=8 * 2**30, disk_bytes=2**30))
    assert verdict.runnable is True


def test_resource_requirement_scales_with_file(tmp_path: Path):
    weights = tmp_path / "m.gguf"
    weights.write_bytes(b"0" * 4096)
    req = resource_requirement_from_file(weights, context_window=4096)
    assert req.disk_bytes == 4096
    assert req.min_ram_bytes > 4096


# --------------------------------------------------------------------------
# Model security
# --------------------------------------------------------------------------
def test_model_path_cannot_escape_storage(tmp_path: Path):
    storage = resolve_storage_dir(str(tmp_path / "store"))
    outside = tmp_path / "evil.gguf"
    outside.write_bytes(b"x")
    with pytest.raises(ValidationError):
        ensure_within_storage(outside, storage_dir=storage)


def test_model_path_inside_storage_is_accepted(tmp_path: Path):
    storage = resolve_storage_dir(str(tmp_path / "store"))
    inside = storage / "m.gguf"
    inside.write_bytes(b"x")
    assert ensure_within_storage(inside, storage_dir=storage) == inside.resolve()


def test_symlinked_model_path_is_refused(tmp_path: Path):
    storage = resolve_storage_dir(str(tmp_path / "store"))
    real = storage / "real.gguf"
    real.write_bytes(b"x")
    link = storage / "link.gguf"
    link.symlink_to(real)
    with pytest.raises(ValidationError):
        ensure_within_storage(link, storage_dir=storage)


def test_verify_model_file_checks_hash(tmp_path: Path):
    weights = tmp_path / "m.gguf"
    weights.write_bytes(b"hello")
    good = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    verify_model_file(weights, sha256=good)  # does not raise
    with pytest.raises(ValidationError):
        verify_model_file(weights, sha256="0" * 64)


def test_verify_model_file_rejects_missing(tmp_path: Path):
    with pytest.raises(ValidationError):
        verify_model_file(tmp_path / "absent.gguf", sha256=None)


def test_model_config_rejects_embedded_credentials():
    with pytest.raises(ValidationError):
        safe_load_config(json.dumps({"api_key": "sk-secret"}))


def test_model_config_rejects_command_keys():
    with pytest.raises(ValidationError):
        safe_load_config(json.dumps({"command": "rm -rf /"}))


def test_model_config_accepts_plain_data():
    payload = safe_load_config(json.dumps({"context_window": 4096, "nested": {"x": 1}}))
    assert payload["context_window"] == 4096


# --------------------------------------------------------------------------
# Descriptor registry
# --------------------------------------------------------------------------
def test_descriptor_rejects_unknown_backend():
    # The field validator rejects it; pydantic surfaces that as its own
    # ValidationError, which is distinct from the platform's.
    with pytest.raises(PydanticValidationError):
        _descriptor(backend="magic")


def test_descriptor_known_backends_are_available():
    assert "llama_cpp" in KNOWN_BACKENDS


def test_registry_registers_and_resolves_logical_id():
    registry = ModelRegistry()
    descriptor = _descriptor()
    registry.register(descriptor)
    assert registry.has("local")
    assert model_logical_id(descriptor) == "local/model.gguf"
    assert registry.logical_ids() == ["local/model.gguf"]


def test_registry_rejects_duplicate_registration():
    registry = ModelRegistry()
    registry.register(_descriptor())
    with pytest.raises(ConfigurationError):
        registry.register(_descriptor())


def test_build_backend_refuses_missing_file(tmp_path: Path):
    descriptor = _descriptor(model_path=str(tmp_path / "missing.gguf"))
    with pytest.raises(ValidationError):
        build_backend(descriptor)


# --------------------------------------------------------------------------
# Fail-closed routing
# --------------------------------------------------------------------------
def test_router_prefers_local_then_remote():
    router = ModelRouter(local_models=["local/m"], remote_models=["openai/gpt"])
    decision = router.decide(policy=RoutingPolicy(mode=OperatingMode.HYBRID))
    assert decision.candidates == ("local/m", "openai/gpt")
    assert decision.location is ModelLocation.LOCAL
    assert decision.fail_closed is False


def test_router_fails_closed_when_offline_and_no_local():
    router = ModelRouter(local_models=[], remote_models=["openai/gpt"])
    decision = router.decide(policy=RoutingPolicy(mode=OperatingMode.OFFLINE))
    assert decision.fail_closed is True
    assert decision.candidates == ()
    assert decision.location is ModelLocation.LOCAL


def test_router_pins_private_data_to_local():
    router = ModelRouter(local_models=["local/m"], remote_models=["openai/gpt"])
    decision = router.decide(
        policy=RoutingPolicy(mode=OperatingMode.HYBRID),
        sensitivity=DataSensitivity.CONFIDENTIAL,
    )
    assert decision.candidates == ("local/m",)
    assert decision.remote_allowed is False


def test_router_refuses_remote_request_under_local_requirement():
    router = ModelRouter(local_models=["local/m"], remote_models=["openai/gpt"])
    decision = router.decide(
        policy=RoutingPolicy(mode=OperatingMode.OFFLINE),
        requested_model="openai/gpt",
    )
    assert decision.fail_closed is True


def test_router_respects_capability_filter():
    router = ModelRouter(
        local_models=["local/chat", "local/vision"],
        remote_models=[],
        capabilities={
            "local/chat": frozenset({ModelCapability.CHAT}),
            "local/vision": frozenset({ModelCapability.VISION}),
        },
    )
    decision = router.decide(
        policy=RoutingPolicy(mode=OperatingMode.OFFLINE),
        capability=ModelCapability.VISION,
    )
    assert decision.candidates == ("local/vision",)


def test_router_local_only_refuses_remote_even_when_available():
    """A hard local-only pin never lets a remote model serve a request."""
    router = ModelRouter(local_models=["local/m"], remote_models=["openai/gpt"])
    decision = router.decide(
        policy=RoutingPolicy(mode=OperatingMode.HYBRID, local_only=True)
    )
    assert decision.candidates == ("local/m",)
    assert decision.remote_allowed is False
    assert decision.fail_closed is False


def test_router_local_only_refuses_remote_requested_model():
    router = ModelRouter(local_models=["local/m"], remote_models=["openai/gpt"])
    decision = router.decide(
        policy=RoutingPolicy(mode=OperatingMode.HYBRID, local_only=True),
        requested_model="openai/gpt",
    )
    assert decision.fail_closed is True
    assert decision.candidates == ()
    assert "local-only" in decision.reason


def test_router_local_only_fails_closed_with_no_local_model():
    router = ModelRouter(local_models=[], remote_models=["openai/gpt"])
    decision = router.decide(policy=RoutingPolicy(mode=OperatingMode.HYBRID, local_only=True))
    assert decision.fail_closed is True
    assert decision.candidates == ()
    assert decision.remote_allowed is False


async def test_gateway_fails_closed_and_does_not_fall_back():
    registry = ProviderRegistry()
    registry.register(LocalBackendProvider(descriptor=_descriptor(), backend=FakeBackend()))
    registry.register(_RemoteProvider())
    router = ModelRouter(local_models=[], remote_models=["remote/m"])
    gateway = ModelGateway(
        registry,
        settings=Settings(environment="test"),
        default_model="remote/m",
        router=router,
        routing_policy=RoutingPolicy(mode=OperatingMode.OFFLINE),
    )
    with pytest.raises(PermissionDeniedError):
        await gateway.complete(ModelRequest(messages=(ChatMessage.user("secret"),)))
    assert gateway.stats.refusals == 1
    assert gateway.stats.remote_requests == 0


async def test_gateway_counts_local_and_remote_requests():
    registry = ProviderRegistry()
    registry.register(LocalBackendProvider(descriptor=_descriptor(), backend=FakeBackend()))
    registry.register(_RemoteProvider())
    gateway = ModelGateway(
        registry,
        settings=Settings(environment="test"),
        default_model="local/model.gguf",
        fallbacks=("remote/m",),
    )
    await gateway.complete(ModelRequest(messages=(ChatMessage.user("hi"),)))
    assert gateway.stats.local_requests == 1
    assert gateway.stats.remote_requests == 0


async def test_local_only_mode_never_calls_a_remote_model():
    """In local-only mode a request never reaches the remote provider.

    The remote provider records every call, so this proves no fallback happened
    even though the remote model is registered and would otherwise be a valid
    fallback for the default model.
    """
    remote = _RemoteProvider()
    registry = ProviderRegistry()
    registry.register(LocalBackendProvider(descriptor=_descriptor(), backend=FakeBackend()))
    registry.register(remote)
    router = ModelRouter(local_models=["local/model.gguf"], remote_models=["remote/m"])
    gateway = ModelGateway(
        registry,
        settings=Settings(environment="test"),
        default_model="local/model.gguf",
        fallbacks=("remote/m",),
        router=router,
        routing_policy=RoutingPolicy(mode=OperatingMode.OFFLINE, local_only=True),
    )
    await gateway.complete(ModelRequest(messages=(ChatMessage.user("مرحبا"),)))
    assert gateway.stats.local_requests == 1
    assert gateway.stats.remote_requests == 0
    assert remote.calls == 0


async def test_local_only_mode_refuses_rather_than_falling_back():
    """With no local model, local-only fails closed instead of using the cloud."""
    remote = _RemoteProvider()
    registry = ProviderRegistry()
    registry.register(remote)
    router = ModelRouter(local_models=[], remote_models=["remote/m"])
    gateway = ModelGateway(
        registry,
        settings=Settings(environment="test"),
        default_model="remote/m",
        router=router,
        routing_policy=RoutingPolicy(mode=OperatingMode.OFFLINE, local_only=True),
    )
    with pytest.raises(PermissionDeniedError):
        await gateway.complete(ModelRequest(messages=(ChatMessage.user("سرّي"),)))
    assert gateway.stats.remote_requests == 0
    assert remote.calls == 0


class _RemoteProvider(ModelProvider):
    """A minimal non-local provider for accounting tests."""

    name = "remote"
    capabilities = frozenset({ModelCapability.CHAT})
    is_local = False

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(message=ChatMessage.assistant("remote"), model="remote")


class _FailingBackend:
    """A local backend that always fails, to exercise fail-closed recovery."""

    name = "failing"

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def load(self) -> None:
        return None

    async def generate(self, request: ModelRequest, *, timeout_seconds: float) -> ModelResponse:
        raise self._error

    async def stream(self, request: ModelRequest, *, timeout_seconds: float):
        raise self._error  # pragma: no cover - not exercised
        yield  # pragma: no cover

    async def healthy(self) -> bool:
        return False

    async def close(self) -> None:
        return None

    def model_info(self) -> dict[str, object]:
        return {"backend": self.name}


async def test_local_crash_offline_does_not_fall_back_to_cloud():
    """LOCAL_ONLY + a crashing local backend must not reach a cloud model."""
    from arabic_agent.core.errors import ExternalServiceError

    registry = ProviderRegistry()
    registry.register(
        LocalBackendProvider(
            descriptor=_descriptor(),
            backend=_FailingBackend(ExternalServiceError("worker crashed")),
        )
    )
    registry.register(_RemoteProvider())
    router = ModelRouter(local_models=["local/model.gguf"], remote_models=["remote/m"])
    gateway = ModelGateway(
        registry,
        settings=Settings(environment="test"),
        default_model="local/model.gguf",
        fallbacks=("remote/m",),
        router=router,
        routing_policy=RoutingPolicy(mode=OperatingMode.OFFLINE),
    )
    with pytest.raises(ExternalServiceError):
        await gateway.complete(ModelRequest(messages=(ChatMessage.user("secret"),)))
    assert gateway.stats.remote_requests == 0
    assert gateway.stats.local_requests == 0


async def test_local_timeout_offline_does_not_fall_back_to_cloud():
    """LOCAL_ONLY + a timed-out local backend must not reach a cloud model."""
    from arabic_agent.core.errors import ExternalServiceError

    registry = ProviderRegistry()
    registry.register(
        LocalBackendProvider(
            descriptor=_descriptor(),
            backend=_FailingBackend(ExternalServiceError("deadline exceeded")),
        )
    )
    registry.register(_RemoteProvider())
    router = ModelRouter(local_models=["local/model.gguf"], remote_models=["remote/m"])
    gateway = ModelGateway(
        registry,
        settings=Settings(environment="test"),
        default_model="local/model.gguf",
        fallbacks=("remote/m",),
        router=router,
        routing_policy=RoutingPolicy(mode=OperatingMode.OFFLINE),
    )
    with pytest.raises(ExternalServiceError):
        await gateway.complete(ModelRequest(messages=(ChatMessage.user("secret"),)))
    assert gateway.stats.remote_requests == 0


# --------------------------------------------------------------------------
# Tool-call parsing (content-embedded, the llama.cpp reality)
# --------------------------------------------------------------------------
def test_parse_tool_call_block_from_content():
    content = (
        "Sure, let me check.\n"
        '<tool_call>\n{"name": "get_weather", "arguments": {"city": "Cairo"}}\n</tool_call>'
    )
    calls, remaining = parse_tool_calls(content)
    assert len(calls) == 1
    assert calls[0].name == "get_weather"
    assert calls[0].arguments == {"city": "Cairo"}
    assert remaining == "Sure, let me check."


def test_parse_tool_call_handles_nested_arguments():
    content = '<tool_call>{"name": "t", "arguments": {"a": {"b": 1}}}</tool_call>'
    calls, remaining = parse_tool_calls(content)
    assert calls[0].arguments == {"a": {"b": 1}}
    assert remaining == ""


def test_parse_tool_call_handles_string_arguments():
    content = '<tool_call>{"name": "t", "arguments": "{\\"x\\": 2}"}</tool_call>'
    calls, _ = parse_tool_calls(content)
    assert calls[0].arguments == {"x": 2}


def test_parse_tool_call_leaves_incomplete_block():
    content = '<tool_call>{"name": "t", "arguments": {'
    calls, remaining = parse_tool_calls(content)
    assert calls == ()
    assert "<tool_call>" in remaining


def test_parse_multiple_tool_calls():
    content = (
        '<tool_call>{"name": "a", "arguments": {}}</tool_call>'
        "text"
        '<tool_call>{"name": "b", "arguments": {"n": 1}}</tool_call>'
    )
    calls, remaining = parse_tool_calls(content)
    assert [c.name for c in calls] == ["a", "b"]
    assert remaining == "text"


def test_split_streamable_withholds_open_tag():
    emitted, held = _split_streamable("hello <tool_call>{")
    assert emitted == "hello "
    assert held == "<tool_call>{"


def test_split_streamable_withholds_partial_tag():
    emitted, held = _split_streamable("hello <tool")
    assert emitted == "hello "
    assert held == "<tool"


def test_split_streamable_passes_plain_text():
    emitted, held = _split_streamable("just text")
    assert emitted == "just text"
    assert held == ""


def test_fake_backend_satisfies_protocol():
    assert isinstance(FakeBackend(), LocalInferenceBackend)


async def test_local_backend_provider_reports_is_local():
    provider = LocalBackendProvider(descriptor=_descriptor(), backend=FakeBackend())
    assert provider.is_local is True
    await provider.load()
    assert await provider.probe() is True
    response = await provider.complete(ModelRequest(messages=(ChatMessage.user("hi"),)))
    assert response.content == "ok"


async def test_discover_local_model_failure_is_reported_not_raised(tmp_path: Path):
    descriptor = _descriptor(model_path=str(tmp_path / "missing.gguf"))
    provider, status = await discover_local_model(descriptor)
    assert provider is None
    assert status.available is False
    assert status.detail


# --------------------------------------------------------------------------
# Real inference (opt-in)
# --------------------------------------------------------------------------
@pytest.mark.local_model
@requires_model
async def test_real_inference_english_with_token_accounting():
    backend = LlamaCppBackend(model_path=MODEL_PATH, context_window=2048)
    await backend.load()
    try:
        response = await backend.generate(
            ModelRequest(messages=(ChatMessage.user("Say the word: hello"),), max_tokens=16),
            timeout_seconds=120.0,
        )
        assert response.content.strip()
        assert response.usage.prompt_tokens > 0
        assert response.usage.completion_tokens > 0
    finally:
        await backend.close()


@pytest.mark.local_model
@requires_model
async def test_real_inference_arabic():
    backend = LlamaCppBackend(model_path=MODEL_PATH, context_window=2048)
    await backend.load()
    try:
        response = await backend.generate(
            ModelRequest(
                messages=(ChatMessage.user("اكتب كلمة: مرحبا"),),
                max_tokens=24,
            ),
            timeout_seconds=120.0,
        )
        assert response.content.strip()
    finally:
        await backend.close()


@pytest.mark.local_model
@requires_model
async def test_real_inference_streaming_parses_tool_calls():
    from arabic_agent.models.base import ToolSpec

    backend = LlamaCppBackend(model_path=MODEL_PATH, context_window=2048)
    await backend.load()
    tool = ToolSpec(
        name="get_weather",
        description="Get the weather for a city",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )
    try:
        request = ModelRequest(
            messages=(ChatMessage.user("What is the weather in Cairo? Use the tool."),),
            tools=(tool,),
            max_tokens=64,
        )
        calls = []
        text = ""
        async for delta in backend.stream(request, timeout_seconds=120.0):
            text += delta.content
            calls.extend(delta.tool_calls)
        # Either the model emitted a parsed tool call, or it answered in text.
        # Both are valid; what must never happen is a raw block leaking as text.
        assert "<tool_call>" not in text
        if calls:
            assert calls[0].name == "get_weather"
    finally:
        await backend.close()
