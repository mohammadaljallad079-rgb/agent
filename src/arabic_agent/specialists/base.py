"""Specialist agent profiles — the shared spine for Phase 04.

The platform has one orchestration core (agent loop, tools, permissions,
tasks, memory, context, model gateway, audit). A *specialist* is not a second
engine: it is a declarative scope over that core. A profile answers four
questions the runner, the planner and the API all need:

* **Which tools may this specialist advertise?** (``tool_scope``)
* **What model capability does it require?** (``required_capabilities``)
* **Which capability domains does it cover?** (``domains``)
* **What persona and guard-rails does it operate under?** (``system_prompt``)

Everything else — execution, approval, audit, recovery, memory, tenancy — is the
shared runtime. Adding a specialist is adding a profile here (and, if it needs a
new capability, an ordinary :class:`~arabic_agent.tools.base.Tool`), never a new
service. This is what keeps five specialists from becoming five applications.

Profiles are pure data with no dependencies on the container, so the runner can
import them without a cycle and a test can assert a profile's scope without
building a platform.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..core.enums import ModelCapability

#: Built-in specialist identifiers. The general agent is the default and keeps
#: the full registry; the others are scoped.
GENERAL = "general"
VOICE = "voice"
VIDEO = "video"
CODING = "coding"
EDUCATION = "education"

BUILTIN_SPECIALISTS: tuple[str, ...] = (GENERAL, VOICE, VIDEO, CODING, EDUCATION)


class Specialist(BaseModel):
    """A declarative scope over the shared agent core.

    ``tool_scope`` is the *advertised* set: the runner will only show these tools
    to the planner and the per-step model. It is a usability boundary, not the
    security boundary — the executor still enforces permissions/approvals on
    whatever the model calls, and a tool outside the scope that is called anyway
    is denied by the executor's scope check (see ``AgentRunner``). Defence in
    depth: a specialist both *offers less* and *accepts less*.

    An empty ``tool_scope`` means "the whole registry" (the general agent), which
    is why it is the explicit sentinel rather than an empty tuple being a mistake.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name_ar: str
    name_en: str
    description_ar: str
    description_en: str
    #: Advertised tool names. Empty == the whole registry (general agent).
    tool_scope: frozenset[str] = frozenset()
    #: Tool names that are always available even if not listed in ``tool_scope``
    #: (e.g. the file primitives a coding agent needs to read before it patches).
    extra_tools: frozenset[str] = frozenset()
    #: Model capabilities this specialist needs; the API/readiness layer uses
    #: this to refuse to run a video job on a text-only model rather than
    #: failing halfway.
    required_capabilities: frozenset[ModelCapability] = frozenset()
    #: Capability domains, for discovery and for the docs' readiness matrix.
    domains: tuple[str, ...] = ()
    #: Extra system directive appended to the shared operator prompt.
    system_prompt: str = ""

    def advertised_tools(self) -> frozenset[str]:
        """The tool names this specialist advertises (empty == whole registry)."""
        if not self.tool_scope:
            return frozenset()
        return self.tool_scope | self.extra_tools

    def allows(self, tool_name: str) -> bool:
        """Whether the specialist advertises (and therefore accepts) a tool."""
        scope = self.advertised_tools()
        return not scope or tool_name in scope

    def scoped_names(self, available: frozenset[str]) -> frozenset[str]:
        """Intersect the specialist scope with the tools the runtime actually has.

        A tool the specialist wants but the runtime did not register (an optional
        extra is absent) is silently dropped here — the specialist degrades to
        what exists, and readiness reporting surfaces the gap, rather than the
        run failing on a missing tool.
        """
        scope = self.advertised_tools()
        if not scope:
            return frozenset(available)
        return scope & available
