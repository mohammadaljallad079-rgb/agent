"""Subprocess boundary: protocol safety and process lifecycle.

These tests drive the *real* parent backend
(:class:`~arabic_agent.local.subprocess_backend.SubprocessInferenceBackend`)
against a controlled child (``tests.fake_worker``) over the real length-prefixed
JSON protocol and real OS pipes. Only the child is a double; the parent, the
framing, the crash detection, the respawn and the pipe lifecycle are the
production code paths.

They never load a real model, so they run in the always-on suite.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from arabic_agent.core.errors import ExternalServiceError
from arabic_agent.local.subprocess_backend import (
    _HEADER,
    _MAX_FRAME,
    SubprocessInferenceBackend,
)
from arabic_agent.models.base import ChatMessage, ModelRequest

_WORKER = "tests.fake_worker"


def _backend(tmp_path: Path, mode: str, *, timeout: float = 10.0) -> SubprocessInferenceBackend:
    marker = str(tmp_path / f"marker-{mode}")
    os.environ["FAKE_WORKER_MODE"] = mode
    os.environ["FAKE_WORKER_MARKER"] = marker
    return SubprocessInferenceBackend(
        model_path=str(tmp_path / "model.gguf"),
        context_window=2048,
        timeout_seconds=timeout,
        worker_module=_WORKER,
    )


def _request() -> ModelRequest:
    return ModelRequest(messages=(ChatMessage.user("hi"),), max_tokens=8)


# --------------------------------------------------------------------------
# Frame reader: bounded size, JSON-object validation, desync safety.
# --------------------------------------------------------------------------
async def test_read_frame_rejects_oversize_without_allocating():
    reader = asyncio.StreamReader()
    reader.feed_data(_HEADER.pack(_MAX_FRAME + 1))
    reader.feed_eof()
    from arabic_agent.local.subprocess_backend import _read_frame

    with pytest.raises(ExternalServiceError):
        await _read_frame(reader)


async def test_read_frame_rejects_non_object_json():
    reader = asyncio.StreamReader()
    body = b'["not", "an", "object"]'
    reader.feed_data(_HEADER.pack(len(body)) + body)
    reader.feed_eof()
    from arabic_agent.local.subprocess_backend import _read_frame

    with pytest.raises(ExternalServiceError):
        await _read_frame(reader)


async def test_read_frame_round_trips_a_json_object():
    from arabic_agent.local.subprocess_backend import _read_frame, _write_frame

    reader = asyncio.StreamReader()
    writer = _CollectingWriter()
    await _write_frame(writer, {"ready": True, "note": "محمّد"})
    reader.feed_data(writer.buffer)
    reader.feed_eof()
    assert await _read_frame(reader) == {"ready": True, "note": "محمّد"}


class _CollectingWriter:
    def __init__(self) -> None:
        self.buffer = b""

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:  # pragma: no cover - trivial
        return None


# --------------------------------------------------------------------------
# Startup failure modes are recoverable, never fatal.
# --------------------------------------------------------------------------
async def test_malformed_ready_frame_is_recoverable(tmp_path: Path):
    backend = _backend(tmp_path, "malformed_ready")
    with pytest.raises(ExternalServiceError):
        await backend.load()
    assert not await backend.healthy()
    await backend.close()


async def test_ready_false_is_recoverable(tmp_path: Path):
    backend = _backend(tmp_path, "ready_false")
    with pytest.raises(ExternalServiceError):
        await backend.load()
    assert not await backend.healthy()
    await backend.close()


async def test_startup_timeout_is_recoverable(tmp_path: Path):
    backend = _backend(tmp_path, "never_ready", timeout=0.5)
    with pytest.raises(ExternalServiceError):
        await backend.load()
    assert not await backend.healthy()
    await backend.close()


# --------------------------------------------------------------------------
# Runtime failure modes become structured errors and drop the worker.
# --------------------------------------------------------------------------
async def test_child_crash_mid_request_is_structured(tmp_path: Path):
    backend = _backend(tmp_path, "crash")
    with pytest.raises(ExternalServiceError) as excinfo:
        await backend.generate(_request(), timeout_seconds=5.0)
    assert excinfo.value.retryable is True
    assert not await backend.healthy()
    await backend.close()


async def test_malformed_frame_mid_request_drops_worker(tmp_path: Path):
    backend = _backend(tmp_path, "malformed")
    with pytest.raises(ExternalServiceError):
        await backend.generate(_request(), timeout_seconds=5.0)
    assert not await backend.healthy()
    await backend.close()


async def test_oversize_frame_mid_request_drops_worker(tmp_path: Path):
    backend = _backend(tmp_path, "oversize")
    with pytest.raises(ExternalServiceError):
        await backend.generate(_request(), timeout_seconds=5.0)
    assert not await backend.healthy()
    await backend.close()


async def test_invalid_response_shape_is_structured(tmp_path: Path):
    backend = _backend(tmp_path, "invalid_response")
    with pytest.raises(ExternalServiceError):
        await backend.generate(_request(), timeout_seconds=5.0)
    assert not await backend.healthy()
    await backend.close()


async def test_request_timeout_kills_the_child(tmp_path: Path):
    backend = _backend(tmp_path, "silent")
    with pytest.raises(ExternalServiceError):
        await backend.generate(_request(), timeout_seconds=0.5)
    assert not await backend.healthy()
    await backend.close()


async def test_worker_exit_after_ready_is_recoverable(tmp_path: Path):
    backend = _backend(tmp_path, "exit_after_ready")
    await backend.load()
    with pytest.raises(ExternalServiceError):
        await backend.generate(_request(), timeout_seconds=5.0)
    await backend.close()


# --------------------------------------------------------------------------
# Recovery: a crashed worker is respawned and later inference succeeds.
# --------------------------------------------------------------------------
async def test_worker_restarts_after_crash(tmp_path: Path):
    backend = _backend(tmp_path, "crash_once")
    with pytest.raises(ExternalServiceError):
        await backend.generate(_request(), timeout_seconds=5.0)
    # The next call respawns the child and succeeds.
    response = await backend.generate(_request(), timeout_seconds=5.0)
    assert response.message.content == "ok"
    assert await backend.healthy()
    await backend.close()


async def test_close_is_idempotent_and_leaves_no_process(tmp_path: Path):
    backend = _backend(tmp_path, "ok")
    await backend.load()
    await backend.close()
    await backend.close()
    assert not await backend.healthy()


async def test_close_terminates_the_child_process(tmp_path: Path):
    backend = _backend(tmp_path, "ok")
    await backend.load()
    process = backend._process
    assert process is not None and process.returncode is None
    await backend.close()
    assert process.returncode is not None
    assert backend._process is None


async def test_stream_is_explicitly_not_implemented(tmp_path: Path):
    backend = _backend(tmp_path, "ok")
    with pytest.raises(NotImplementedError):
        backend.stream(_request(), timeout_seconds=1.0)
    await backend.close()


# --------------------------------------------------------------------------
# The child cannot redirect the parent to a different model path.
# --------------------------------------------------------------------------
async def test_worker_module_is_a_module_name_not_a_shell_string(tmp_path: Path):
    # A malicious "module" containing shell metacharacters is never interpolated
    # into a shell: create_subprocess_exec treats it as a single argv entry.
    backend = SubprocessInferenceBackend(
        model_path=str(tmp_path / "model.gguf"),
        worker_module="tests.fake_worker; touch /tmp/pwned",
    )
    with pytest.raises(Exception):
        await backend.load()
    await backend.close()
    assert not Path("/tmp/pwned").exists()  # noqa: ASYNC240 - test assertion
