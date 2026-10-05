"""Project / repository understanding tool (Phase 6).

``inspect_project`` gives the agent a bounded, honest picture of a software
project — languages, manifests, configs, tests, entrypoints and docs — so it can
plan how to work on it (which files matter, what to read, how to verify) instead
of guessing. It is a normal policy-gated tool; it only reads names and, on
request, a bounded summary of a manifest.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ...core.errors import NotFoundError
from ...local.sandbox_provider import SandboxProvider
from ...permissions.base import Permission
from ...project.summary import read_manifest_summary, summarize_project
from ..base import Tool, ToolContext, ToolResult


class InspectProjectTool(Tool):
    name = "inspect_project"
    description = (
        "Analyse a directory as a software project: detect languages, dependency "
        "manifests, config files, tests, entrypoints and documentation. Use this "
        "to understand an unfamiliar repository before reading or editing it. "
        "Read-only and bounded."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Project root (default '.')"},
            "read_manifests": {
                "type": "boolean",
                "description": "Also read bounded summaries of dependency manifests",
            },
        },
        "additionalProperties": False,
    }
    permissions = (Permission.FILES_READ, Permission.FILES_SEARCH)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        requested = arguments.get("path") or context.workspace_dir or "."
        sandbox = SandboxProvider().for_context(context)
        root = sandbox.resolve_for_read(requested)
        if not root.is_dir():
            raise NotFoundError("path is not a directory", details={"path": requested})

        summary = summarize_project(root)
        manifests: list[dict[str, Any]] = []
        if arguments.get("read_manifests"):
            for manifest in summary.manifests[:10]:
                candidate = root / manifest.path
                if candidate.is_file() and sandbox.authorize_read(str(candidate)).allowed:
                    try:
                        manifests.append(read_manifest_summary(candidate))
                    except OSError:
                        continue

        return ToolResult.success(
            f"project at {requested}: {summary.primary_language or 'unknown'} "
            f"({summary.total_files} files)",
            data={
                "path": requested,
                "languages": list(summary.languages),
                "primary_language": summary.primary_language,
                "manifests": [m.path for m in summary.manifests],
                "configs": [c.path for c in summary.configs],
                "tests": [t.path for t in summary.tests],
                "entrypoints": [e.path for e in summary.entrypoints],
                "docs": [d.path for d in summary.docs],
                "total_files": summary.total_files,
                "total_dirs": summary.total_dirs,
                "truncated": summary.truncated,
                "warnings": list(summary.warnings),
                "manifest_summaries": manifests,
                "render": summary.render(),
            },
        )


#: Project understanding tools.
PROJECT_TOOLS: tuple[type[Tool], ...] = (InspectProjectTool,)
