"""Vision model-layer and context-attachment tests (Phase 7).

Cover the multimodal contract: an :class:`ImageContent` on a message, the
OpenAI-compatible provider encoding it as a data-URI content array, the gateway
capability query, and the context manager including or dropping an image
depending on whether the active model can see and whether the budget allows it.
"""

from __future__ import annotations

from arabic_agent.context.attachments import (
    ContextAttachment,
    NullAttachmentProvider,
    StaticAttachmentProvider,
)
from arabic_agent.context.manager import ContextManager
from arabic_agent.core.config import Settings
from arabic_agent.core.enums import ModelCapability
from arabic_agent.models.base import ChatMessage, ImageContent
from arabic_agent.models.gateway import ModelGateway
from arabic_agent.models.providers.openai_compatible import OpenAICompatibleProvider
from arabic_agent.models.registry import ProviderRegistry

from .helpers import FakeProvider, text_response

# --- ImageContent & provider encoding ---------------------------------------


def test_image_content_from_bytes_encodes_base64():
    image = ImageContent.from_bytes(b"hello", width=10, height=20)
    assert image.mime_type == "image/png"
    assert image.size_bytes >= len(b"hello")
    assert image.width == 10


def test_openai_provider_encodes_multimodal_message_as_content_array():
    image = ImageContent.from_bytes(b"\x89PNG", width=1, height=1)
    message = ChatMessage.user("look", images=(image,))
    payload = OpenAICompatibleProvider._encode_message(message)
    content = payload["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_provider_plain_text_message_has_string_content():
    payload = OpenAICompatibleProvider._encode_message(ChatMessage.user("hello"))
    assert payload["content"] == "hello"


# --- Gateway capability query ------------------------------------------------


def _gateway(*, vision: bool, tmp_path) -> ModelGateway:
    provider = FakeProvider(lambda req, n: text_response("ok"))
    if vision:
        provider.capabilities = frozenset(
            {ModelCapability.CHAT, ModelCapability.TOOL_CALLING, ModelCapability.VISION}
        )
    registry = ProviderRegistry()
    registry.register(provider)
    return ModelGateway(registry, settings=_settings(tmp_path), default_model="fake/model")


def test_gateway_default_supports_reports_vision(tmp_path):
    vision = _gateway(vision=True, tmp_path=tmp_path)
    assert vision.default_supports(ModelCapability.VISION) is True
    text_only = _gateway(vision=False, tmp_path=tmp_path)
    assert text_only.default_supports(ModelCapability.VISION) is False


# --- Context manager attachment budgeting -----------------------------------


def _settings(tmp_path) -> Settings:
    return Settings(
        environment="test",
        local_store_dir=str(tmp_path / "data"),
        max_context_tokens=4000,
        database_url="",
        redis_url="",
    )


def _attachment() -> ContextAttachment:
    return ContextAttachment(
        image=ImageContent.from_bytes(b"x" * 100, width=1280, height=900),
        description="screenshot 1280x900",
        source="observation",
    )


def test_context_includes_image_for_vision_model(tmp_path):
    manager = ContextManager(_settings(tmp_path))
    package = manager.build(
        system_prompt="sys",
        goal="look at the screen",
        plan=None,
        recent_messages=[],
        attachments=[_attachment()],
        allow_images=True,
    )
    assert len(package.attachments) == 1
    assert any(msg.images for msg in package.messages)
    assert package.budget["images"] > 0


def test_context_drops_image_for_text_only_model(tmp_path):
    manager = ContextManager(_settings(tmp_path))
    package = manager.build(
        system_prompt="sys",
        goal="look at the screen",
        plan=None,
        recent_messages=[],
        attachments=[_attachment()],
        allow_images=False,
    )
    assert package.attachments == ()
    assert not any(msg.images for msg in package.messages)


def test_context_drops_image_when_budget_is_tiny(tmp_path):
    settings = Settings(
        environment="test",
        local_store_dir=str(tmp_path / "data"),
        max_context_tokens=512,
        database_url="",
        redis_url="",
    )
    manager = ContextManager(settings)
    package = manager.build(
        system_prompt="sys",
        goal="look",
        plan=None,
        recent_messages=[],
        attachments=[_attachment()],
        allow_images=True,
    )
    assert package.attachments == ()
    assert any(drop.startswith("image:") for drop in package.dropped)


# --- Providers ---------------------------------------------------------------


async def test_null_attachment_provider_returns_nothing():
    assert await NullAttachmentProvider().attachments_for(run_id="r") == []


async def test_static_attachment_provider_returns_configured():
    provider = StaticAttachmentProvider([_attachment()])
    assert len(await provider.attachments_for(run_id="r")) == 1
