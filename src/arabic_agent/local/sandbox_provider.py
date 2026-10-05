"""Sandbox provider for local filesystem tools.

Local tools must resolve every path through the :class:`FilesystemSandbox`, but
they also receive the per-run ``workspace_dir`` in their
:class:`~arabic_agent.tools.base.ToolContext`. This provider produces a sandbox
whose allowed roots are the run workspace plus the operator-configured extra
roots, so a local run can reach the directories the user approved *and* its own
workspace, and nothing else.

Building a sandbox is cheap (it only canonicalises paths), so one is produced
per tool call rather than cached. That keeps the policy a pure function of the
call context and avoids a stale sandbox outliving a changed configuration.
"""

from __future__ import annotations

from ..core.errors import ValidationError
from ..tools.base import ToolContext
from .sandbox import FilesystemPolicy, FilesystemSandbox


class SandboxProvider:
    """Builds a :class:`FilesystemSandbox` for a tool call."""

    def __init__(
        self,
        *,
        extra_roots: tuple[str, ...] = (),
        denied_roots: tuple[str, ...] = (),
        allow_read: bool = True,
        allow_write: bool = True,
    ) -> None:
        self._extra_roots = tuple(r for r in extra_roots if r and r.strip())
        self._denied_roots = tuple(r for r in denied_roots if r and r.strip())
        self._allow_read = allow_read
        self._allow_write = allow_write

    def for_context(self, context: ToolContext) -> FilesystemSandbox:
        """Return the sandbox governing one tool call.

        The run workspace is always allowed and is the anchor for relative
        paths. Operator roots from the context's ``filesystem_policy`` and the
        provider's own extra roots are added, in that order, de-duplicated.
        """
        workspace = (context.workspace_dir or "").strip()
        policy = context.metadata.get("filesystem_policy")
        allowed: list[str] = []
        if isinstance(policy, FilesystemPolicy):
            allowed.extend(policy.allowed_roots)
            denied = policy.denied_roots
        else:
            denied = self._denied_roots
        allowed.extend(self._extra_roots)
        roots = ([workspace] if workspace else []) + allowed
        deduped = tuple(dict.fromkeys(r for r in roots if r and r.strip()))
        if not deduped:
            raise ValidationError("no workspace or allowed roots configured for local tools")
        return FilesystemSandbox(
            FilesystemPolicy(
                allowed_roots=deduped,
                denied_roots=denied,
                workspace_dir=workspace,
                allow_read=self._allow_read,
                allow_write=self._allow_write,
            )
        )

    @property
    def extra_roots(self) -> tuple[str, ...]:
        return self._extra_roots
