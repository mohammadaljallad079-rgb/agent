"""Phase 5B: real local model through the full agent loop.

Two layers:

* **Unit layer** (always runs): the tolerant ``<tool_call>`` parser and the
  llama.cpp context guard. These cover the two defects found when a real
  Qwen2.5-0.5B model was driven through the agent loop — tool calls emitted
  with a stray extra brace (dropped by the strict parser), and oversized
  prompts handed to a too-small context window (which aborts inside ggml).
* **Integration layer** (opt-in, ``@pytest.mark.local_model``): the *real*
  platform — gateway, planner, executor, policy engine, task engine and tools —
  driven end to end by a real GGUF model named by ``AAP_TEST_MODEL_PATH``,
  offline and local-only. Skipped by design when no model file is present.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from arabic_agent.container import Platform
from arabic_agent.context.budget import estimate_tokens, estimate_tool_schema
from arabic_agent.core.config import Settings
from arabic_agent.core.enums import OperatingMode, RunStatus, RuntimeKind
from arabic_agent.core.errors import ValidationError
from arabic_agent.local.inference import (
    LlamaCppBackend,
    _loads_tolerant,
    parse_tool_calls,
)
from arabic_agent.local.model import build_backend
from arabic_agent.local.model_registry import LocalModelDescriptor
from arabic_agent.local.subprocess_backend import (
    SubprocessInferenceBackend,
    build_isolated_backend,
)
from arabic_agent.models.base import ChatMessage, ModelRequest
from arabic_agent.permissions.base import Principal

MODEL_PATH = os.environ.get("AAP_TEST_MODEL_PATH", "")
requires_model = pytest.mark.skipif(
    not MODEL_PATH or not Path(MODEL_PATH).is_file(),
    reason="AAP_TEST_MODEL_PATH not set to a readable GGUF file",
)


# --------------------------------------------------------------------------
# Unit layer: tolerant tool-call parsing.
# --------------------------------------------------------------------------
def test_single_braced_tool_call_parses():
    calls, remaining = parse_tool_calls(
        '<tool_call>{"name": "echo", "arguments": {"text": "hi"}}</tool_call>'
    )
    assert len(calls) == 1
    assert calls[0].name == "echo"
    assert calls[0].arguments == {"text": "hi"}
    assert remaining == ""


def test_stray_extra_brace_tool_call_parses():
    # The exact shape a Qwen2.5-0.5B-Q4 model emitted: three opens, two closes.
    content = '<tool_call>\n{{"name": "echo", "arguments": {"text": "HELLO-42"}}\n</tool_call>'
    calls, remaining = parse_tool_calls(content)
    assert len(calls) == 1
    assert calls[0].name == "echo"
    assert calls[0].arguments == {"text": "HELLO-42"}
    assert remaining == ""


def test_extra_trailing_brace_tool_call_parses():
    calls, _ = parse_tool_calls(
        '<tool_call>{"name": "finish", "arguments": {"result": "ok"}}}</tool_call>'
    )
    assert [c.name for c in calls] == ["finish"]


def test_multiple_stray_braced_calls_parse_in_order():
    content = (
        '<tool_call>\n{{"name": "echo", "arguments": {"text": "a"}}\n</tool_call>\n'
        '<tool_call>\n{{"name": "finish", "arguments": {"result": "b"}}\n</tool_call>'
    )
    calls, remaining = parse_tool_calls(content)
    assert [c.name for c in calls] == ["echo", "finish"]
    assert remaining == ""


def test_unclosed_block_is_left_for_recovery():
    content = '<tool_call>\n{{"name": "echo", "arguments": {"text": "x"}}\n'
    calls, remaining = parse_tool_calls(content)
    assert calls == ()
    assert "<tool_call>" in remaining


def test_malformed_block_is_dropped_never_fabricated():
    calls, remaining = parse_tool_calls("<tool_call>not json at all</tool_call>")
    assert calls == ()
    assert remaining == ""


def test_text_without_calls_is_preserved():
    calls, remaining = parse_tool_calls("just a plain answer")
    assert calls == ()
    assert remaining == "just a plain answer"


def test_loads_tolerant_rejects_non_object_json():
    assert _loads_tolerant("[1, 2, 3]") is None
    assert _loads_tolerant("not json") is None
    assert _loads_tolerant('{"name": "x"}') == {"name": "x"}


# --------------------------------------------------------------------------
# Unit layer: llama.cpp context guard.
# --------------------------------------------------------------------------
def _stub_backend(*, context_window: int) -> LlamaCppBackend:
    backend = LlamaCppBackend(model_path="/tmp/does-not-matter.gguf", context_window=context_window)
    backend._llama = object()  # pretend it is loaded; the guard only checks presence
    return backend


def test_context_guard_refuses_oversized_prompt():
    backend = _stub_backend(context_window=512)
    big = ChatMessage.user("hello world " * 4000)
    with pytest.raises(ValidationError) as excinfo:
        backend._guard_context(ModelRequest(messages=(big,), max_tokens=32))
    assert "context window" in excinfo.value.message
    assert excinfo.value.details["context_window"] == 512


def test_context_guard_reserves_output_headroom_without_max_tokens():
    # No max_tokens declared: the guard must still leave room to generate,
    # otherwise the sampler has no logits and ggml aborts.
    backend = _stub_backend(context_window=512)
    almost_full = ChatMessage.user("x" * (512 * 4 - 40))
    with pytest.raises(ValidationError):
        backend._guard_context(ModelRequest(messages=(almost_full,)))


def test_context_guard_allows_a_fitting_prompt():
    backend = _stub_backend(context_window=2048)
    request = ModelRequest(messages=(ChatMessage.user("Say hello."),), max_tokens=16)
    backend._guard_context(request)  # must not raise


def _many_tool_specs(n: int):
    from arabic_agent.models.base import ToolSpec

    return tuple(
        ToolSpec(
            name=f"tool_{i}",
            description="A tool with a deliberately verbose schema for budget tests. " * 3,
            parameters={
                "type": "object",
                "properties": {f"arg_{j}": {"type": "string"} for j in range(6)},
            },
        )
        for i in range(n)
    )


def test_context_manager_sizes_to_a_small_window_not_the_configured_budget():
    """A fixed local window caps the budget below the (cloud-sized) default.

    This is the regression for a real defect: the context manager planned for
    ``max_context_tokens`` (16k by default) regardless of a local model's n_ctx,
    so an agent step advertised all tool schemas and the request was rejected at
    call time with "exceeds the model context window".
    """
    from arabic_agent.context.manager import ContextManager

    settings = Settings(
        environment="test",
        require_auth=False,
        max_context_tokens=16_000,
    )
    manager = ContextManager(settings)
    package = manager.build(
        system_prompt="You are a careful agent.",
        goal="Use the tools.",
        plan=None,
        recent_messages=(ChatMessage.user("hi"),),
        tool_specs=_many_tool_specs(40),
        window=2048,
    )
    # Never more than the window, and the over-large optional tools are dropped
    # rather than silently overflowing at call time.
    assert package.budget["total"] <= 2048
    assert package.truncated
    assert any(item.startswith("tool:") for item in package.dropped)
    advertised = (
        sum(
            estimate_tool_schema(spec.name, spec.description, spec.parameters)
            for spec in package.tools
        )
        + estimate_tokens("You are a careful agent.")
        + estimate_tokens("Use the tools.")
    )
    assert advertised <= package.budget["total"]


def test_context_manager_without_a_window_keeps_the_configured_budget():
    from arabic_agent.context.manager import ContextManager

    settings = Settings(environment="test", require_auth=False, max_context_tokens=16_000)
    manager = ContextManager(settings)
    package = manager.build(
        system_prompt="sys",
        goal="g",
        plan=None,
        recent_messages=(ChatMessage.user("hi"),),
        tool_specs=_many_tool_specs(3),
        window=None,
    )
    assert package.budget["total"] > 2048


# --------------------------------------------------------------------------
# Unit layer: process-isolated backend wiring (no model needed).
# --------------------------------------------------------------------------
def _fake_gguf(tmp_path: Path) -> Path:
    # A descriptor only checks that the file exists and the hash, if any,
    # matches; the bytes are never parsed by build_backend.
    model = tmp_path / "tiny.gguf"
    model.write_bytes(b"GGUF-not-really")
    return model


def test_build_backend_isolates_llama_cpp(tmp_path: Path):
    model = _fake_gguf(tmp_path)
    descriptor = LocalModelDescriptor(
        name="local",
        backend="llama_cpp",
        model_path=str(model),
        model_id=model.name,
        context_window=2048,
    )
    backend = build_backend(descriptor, isolate=True)
    assert isinstance(backend, SubprocessInferenceBackend)
    assert backend.model_info()["isolated"] is True

    in_process = build_backend(descriptor, isolate=False)
    assert isinstance(in_process, LlamaCppBackend)


def test_build_backend_isolate_is_noop_for_ollama(tmp_path: Path):
    descriptor = LocalModelDescriptor(
        name="local",
        backend="ollama",
        endpoint="http://localhost:11434",
        model_id="qwen2.5:7b",
    )
    # Endpoint engines are already external, so isolation has no effect.
    assert not isinstance(build_backend(descriptor, isolate=True), SubprocessInferenceBackend)


def test_isolated_backend_rejects_missing_model(tmp_path: Path):
    with pytest.raises(ValidationError):
        build_isolated_backend(model_path=str(tmp_path / "nope.gguf"))


def test_isolated_backend_refuses_to_stream(tmp_path: Path):
    backend = build_isolated_backend(model_path=str(_fake_gguf(tmp_path)))
    with pytest.raises(NotImplementedError):
        backend.stream(ModelRequest(messages=(ChatMessage.user("hi"),)), timeout_seconds=1.0)


async def test_isolated_backend_reports_worker_start_failure(tmp_path: Path):
    # A file that passes the existence check but is not a loadable model: the
    # child must exit cleanly and the parent must surface a recoverable error,
    # never hang or crash.
    backend = build_isolated_backend(model_path=str(_fake_gguf(tmp_path)), timeout_seconds=30.0)
    try:
        with pytest.raises(Exception) as excinfo:
            await backend.load()
        assert "worker failed to start" in str(excinfo.value) or "failed to start" in str(
            excinfo.value
        )
    finally:
        await backend.close()


# --------------------------------------------------------------------------
# Integration layer: the real agent loop on a real local model (opt-in).
# --------------------------------------------------------------------------
def _local_settings(tmp_path: Path) -> Settings:
    model = Path(MODEL_PATH)
    return Settings(
        environment="test",
        require_auth=False,
        runtime_kind=RuntimeKind.LOCAL,
        local_store_dir=str(tmp_path / "store"),
        local_model_storage_dir=str(model.parent),
        local_model_path=str(model),
        local_model_context_window=4096,
        local_mode=OperatingMode.OFFLINE,
        database_url=SecretStr(""),
        redis_url=SecretStr(""),
        execution_mode="inline",
        max_context_tokens=4096,
        max_steps_per_run=6,
        max_tool_retries=2,
        max_replans=1,
        # The run deadline must comfortably exceed the per-call local model
        # deadline (300s): on a contended CPU host a single planning call can
        # take minutes, and a run-level bound below that turns normal slow
        # inference into a spurious failure.
        max_run_seconds=900.0,
    )


@pytest.mark.local_model
@requires_model
async def test_real_model_completes_agent_loop_offline(tmp_path: Path):
    """The full platform, driven by a real local model, offline.

    Planning, tool selection, execution, verification and the audit trail are
    all real; only the goal wording is fixed. The model must call ``echo`` with
    the exact text and then reach a verified completion.
    """
    platform = Platform.build(_local_settings(tmp_path))
    await platform.initialize()
    try:
        result = await platform.build_runner().run(
            principal=Principal(tenant_id="tnt_1", user_id="usr_1"),
            goal=(
                "Use the echo tool to echo the exact text HELLO-42, "
                "then call finish with that echoed text."
            ),
            workspace_dir=str(tmp_path / "ws"),
        )
        assert result.status is RunStatus.COMPLETED, result.error
        assert result.verified
        assert result.tool_calls >= 1

        history = await platform.audit.history(tenant_id="tnt_1", run_id=result.run_id)
        event_types = {event.type.value for event in history}
        assert "run.started" in event_types
        assert "tool.called" in event_types
        assert "run.completed" in event_types

        # Offline, local-only: no request may reach a remote model.
        assert platform.gateway.stats.local_requests >= 1
        assert platform.gateway.stats.remote_requests == 0
    finally:
        await platform.aclose()


@pytest.mark.local_model
@requires_model
async def test_real_model_never_leaks_raw_tool_block_as_text(tmp_path: Path):
    """A tool call is parsed into ``tool_calls``, never leaked as visible text."""
    platform = Platform.build(_local_settings(tmp_path))
    await platform.initialize()
    try:
        # Advertise the tools the request actually names, as the context manager
        # does for a real step — handing a 0.5B model all ~37 schemas overflows
        # a 4096-token window before it can answer.
        tools = tuple(spec for spec in platform.tools.specs() if spec.name in {"echo", "finish"})
        response = await platform.gateway.complete(
            ModelRequest(
                messages=(ChatMessage.user("Echo the text HELLO-42 using the echo tool."),),
                tools=tools,
                temperature=0.0,
                max_tokens=48,
            )
        )
        assert "<tool_call>" not in response.content
        if response.tool_calls:
            assert response.tool_calls[0].name in {spec.name for spec in tools}
    finally:
        await platform.aclose()


@pytest.mark.local_model
@requires_model
async def test_real_model_arabic_agent_loop(tmp_path: Path):
    """A real Arabic task, driven end to end by the real local model, offline.

    Proves the whole path with Arabic I/O: an Arabic goal is planned, the model
    emits a tool call whose argument carries Arabic text, the real echo tool
    runs, the observation returns to the model, and the run finishes with a
    verified Arabic result. No FakeProvider, no injected ToolCall.
    """
    platform = Platform.build(_local_settings(tmp_path))
    await platform.initialize()
    try:
        result = await platform.build_runner().run(
            principal=Principal(tenant_id="tnt_1", user_id="usr_1"),
            goal=("استخدم أداة echo لإرجاع النص العربي: مرحبا-٤٢ ثم استدعِ finish بنفس النص."),
            workspace_dir=str(tmp_path / "ws"),
        )
        assert result.status is RunStatus.COMPLETED, result.error
        assert result.verified
        assert result.tool_calls >= 1
        # The run reached a real local model and never a remote one.
        assert platform.gateway.stats.local_requests >= 1
        assert platform.gateway.stats.remote_requests == 0

        history = await platform.audit.history(tenant_id="tnt_1", run_id=result.run_id)
        event_types = {event.type.value for event in history}
        assert "tool.called" in event_types
        assert "run.completed" in event_types
    finally:
        await platform.aclose()


@pytest.mark.local_model
@requires_model
async def test_real_model_context_guard_prevents_crash(tmp_path: Path):
    """An oversized prompt is refused before it reaches ggml, not crashed on."""
    backend = LlamaCppBackend(model_path=MODEL_PATH, context_window=256)
    await backend.load()
    try:
        with pytest.raises(ValidationError):
            await backend.generate(
                ModelRequest(messages=(ChatMessage.user("hello world " * 4000),), max_tokens=32),
                timeout_seconds=60.0,
            )
        # A fitting request still works after the refusal.
        response = await backend.generate(
            ModelRequest(messages=(ChatMessage.user("Say ok."),), max_tokens=8),
            timeout_seconds=60.0,
        )
        assert response.content.strip()
    finally:
        await backend.close()
