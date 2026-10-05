"""Project / repository understanding.

Given a directory, this module produces a bounded, honest summary of what kind
of software project it is: languages by file count and size, dependency
manifests, configuration files, tests, entrypoints and documentation. It is
deliberately data-driven (a table of marker files and extensions), not a
hardcoded list of frameworks, so a new ecosystem is added by extending the
tables — not by changing the agent.

Nothing here runs anything from the project; it only reads file names and the
first lines of small manifest files. All reads are bounded.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

#: Directories never walked.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        "target",
        ".next",
        ".turbo",
        "vendor",
    }
)

#: Language detection by file extension.
_LANGUAGE_EXTENSIONS: dict[str, str] = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".c": "C",
    ".h": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".hh": "C++",
    ".cs": "C#",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".dart": "Dart",
    ".swift": "Swift",
    ".m": "Objective-C",
    ".scala": "Scala",
    ".sh": "Shell",
    ".bash": "Shell",
    ".ps1": "PowerShell",
    ".sql": "SQL",
    ".r": "R",
    ".lua": "Lua",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".html": "HTML",
    ".css": "CSS",
    ".scss": "CSS",
}

#: Manifest file name -> ecosystem label.
_MANIFESTS: dict[str, str] = {
    "pyproject.toml": "Python (PEP 621 / pyproject)",
    "setup.py": "Python (setuptools)",
    "setup.cfg": "Python (setuptools)",
    "requirements.txt": "Python (pip)",
    "Pipfile": "Python (pipenv)",
    "poetry.lock": "Python (poetry)",
    "uv.lock": "Python (uv)",
    "package.json": "Node.js",
    "package-lock.json": "Node.js (npm)",
    "yarn.lock": "Node.js (yarn)",
    "pnpm-lock.yaml": "Node.js (pnpm)",
    "tsconfig.json": "TypeScript",
    "pom.xml": "Java (Maven)",
    "build.gradle": "Java/Kotlin (Gradle)",
    "build.gradle.kts": "Kotlin (Gradle)",
    "settings.gradle": "Gradle",
    "Cargo.toml": "Rust (Cargo)",
    "go.mod": "Go modules",
    "composer.json": "PHP (Composer)",
    "Gemfile": "Ruby (Bundler)",
    "pubspec.yaml": "Dart/Flutter (pub)",
    "CMakeLists.txt": "C/C++ (CMake)",
    "Makefile": "Make",
    "Dockerfile": "Docker",
    "docker-compose.yml": "Docker Compose",
    "docker-compose.yaml": "Docker Compose",
}

#: Config file names of interest.
_CONFIG_NAMES = frozenset(
    {
        ".env.example",
        "alembic.ini",
        "tox.ini",
        "mypy.ini",
        ".eslintrc",
        ".eslintrc.json",
        ".prettierrc",
        "ruff.toml",
        ".ruff.toml",
        "pytest.ini",
        "jest.config.js",
        "vite.config.ts",
        "next.config.js",
        "webpack.config.js",
        "nginx.conf",
    }
)

#: Documentation file names / patterns.
_DOC_NAMES = frozenset(
    {
        "README.md",
        "README.rst",
        "README.txt",
        "README",
        "ARCHITECTURE.md",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "TODO.md",
        "LICENSE",
        "LICENSE.md",
    }
)

#: Directories that typically hold tests.
_TEST_DIR_NAMES = frozenset({"test", "tests", "spec", "specs", "__tests__"})

#: How many bytes of a manifest to read when summarising it.
_MANIFEST_READ_BYTES = 16 * 1024


@dataclass(frozen=True)
class ProjectFile:
    """A notable file discovered in the project."""

    path: str
    kind: str
    note: str = ""


@dataclass(frozen=True)
class ProjectSummary:
    """A bounded, honest description of a software project."""

    root: str
    languages: tuple[tuple[str, int], ...]
    manifests: tuple[ProjectFile, ...]
    configs: tuple[ProjectFile, ...]
    tests: tuple[ProjectFile, ...]
    entrypoints: tuple[ProjectFile, ...]
    docs: tuple[ProjectFile, ...]
    total_files: int
    total_dirs: int
    truncated: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def primary_language(self) -> str | None:
        return self.languages[0][0] if self.languages else None

    def render(self) -> str:
        lines = [f"Project at {self.root}"]
        if self.languages:
            langs = ", ".join(f"{name} ({count})" for name, count in self.languages[:6])
            lines.append(f"Languages: {langs}")
        if self.manifests:
            lines.append("Manifests: " + ", ".join(m.path for m in self.manifests))
        if self.configs:
            lines.append("Configs: " + ", ".join(c.path for c in self.configs))
        if self.tests:
            lines.append("Tests: " + ", ".join(t.path for t in self.tests))
        if self.entrypoints:
            lines.append("Entrypoints: " + ", ".join(e.path for e in self.entrypoints))
        if self.docs:
            lines.append("Docs: " + ", ".join(d.path for d in self.docs))
        lines.append(f"Files: {self.total_files}, directories: {self.total_dirs}")
        return "\n".join(lines)


def _is_test_path(parts: tuple[str, ...], name: str) -> bool:
    lowered = {p.lower() for p in parts}
    if lowered & _TEST_DIR_NAMES:
        return True
    base = name.lower()
    return base.startswith("test_") or base.endswith("_test.py") or base.endswith(".test.js")


def _is_entrypoint(parts: tuple[str, ...], name: str) -> bool:
    base = name.lower()
    if base in {"main.py", "__main__.py", "manage.py", "wsgi.py", "asgi.py", "app.py", "cli.py"}:
        return True
    if base in {"index.js", "index.ts", "server.js", "server.ts", "main.go", "main.rs", "main.c"}:
        return True
    return bool(base.endswith(".py") and "main" in base)


def summarize_project(
    root: Path,
    *,
    max_files: int = 20_000,
    max_depth: int = 12,
    max_listed: int = 40,
) -> ProjectSummary:
    """Walk ``root`` (bounded) and summarise the project it contains."""
    language_counts: Counter[str] = Counter()
    manifests: list[ProjectFile] = []
    configs: list[ProjectFile] = []
    tests: list[ProjectFile] = []
    entrypoints: list[ProjectFile] = []
    docs: list[ProjectFile] = []
    total_files = 0
    total_dirs = 0
    truncated = False
    warnings: list[str] = []

    root = root.resolve()
    for current_dir, dir_names, file_names in os.walk(root):
        current = Path(current_dir)
        depth = len(current.relative_to(root).parts)
        if depth >= max_depth:
            dir_names[:] = []
        else:
            dir_names[:] = [d for d in dir_names if d not in SKIP_DIRS]
        total_dirs += len(dir_names)
        for file_name in file_names:
            total_files += 1
            if total_files > max_files:
                truncated = True
                warnings.append(f"file cap ({max_files}) reached; summary is partial")
                break
            relative = current.relative_to(root)
            parts = relative.parts
            rel_path = str(relative / file_name)
            suffix = Path(file_name).suffix.lower()
            language = _LANGUAGE_EXTENSIONS.get(suffix)
            if language:
                language_counts[language] += 1
            if file_name in _MANIFESTS and len(manifests) < max_listed:
                manifests.append(
                    ProjectFile(path=rel_path, kind="manifest", note=_MANIFESTS[file_name])
                )
            if file_name in _CONFIG_NAMES and len(configs) < max_listed:
                configs.append(ProjectFile(path=rel_path, kind="config"))
            if _is_test_path(parts, file_name) and len(tests) < max_listed:
                tests.append(ProjectFile(path=rel_path, kind="test"))
            if _is_entrypoint(parts, file_name) and len(entrypoints) < max_listed:
                entrypoints.append(ProjectFile(path=rel_path, kind="entrypoint"))
            if file_name in _DOC_NAMES and len(docs) < max_listed:
                docs.append(ProjectFile(path=rel_path, kind="doc"))
        if total_files > max_files:
            break

    return ProjectSummary(
        root=str(root),
        languages=tuple(language_counts.most_common()),
        manifests=tuple(manifests),
        configs=tuple(configs),
        tests=tuple(tests),
        entrypoints=tuple(entrypoints),
        docs=tuple(docs),
        total_files=total_files,
        total_dirs=total_dirs,
        truncated=truncated,
        warnings=tuple(warnings),
    )


def read_manifest_summary(path: Path) -> dict[str, object]:
    """Read a small, safe summary of a dependency manifest.

    Only file size and a bounded, redacted text head are returned — never a
    whole lockfile and never anything that looks like a credential.
    """
    from ..documents.loader import scan_text_for_secrets

    size = path.stat().st_size
    head = path.read_bytes()[:_MANIFEST_READ_BYTES].decode("utf-8", errors="replace")
    findings = scan_text_for_secrets(head, locator=str(path))
    payload: dict[str, object] = {
        "path": str(path),
        "size": size,
        "truncated": size > _MANIFEST_READ_BYTES,
        # Key name kept free of credential-like words so boundary redaction
        # does not mask the list itself.
        "sensitive_kinds": sorted({f.kind for f in findings}),
    }
    if path.suffix.lower() == ".json":
        try:
            json_head = head if size <= _MANIFEST_READ_BYTES else head[: head.rfind("}") + 1]
            parsed = json.loads(json_head)
            if isinstance(parsed, dict):
                for key in ("name", "version", "description", "scripts"):
                    if key in parsed:
                        payload[key] = parsed[key]
        except (json.JSONDecodeError, ValueError):
            payload["note"] = "JSON manifest could not be fully parsed (truncated or invalid)"
    return payload


__all__ = [
    "ProjectFile",
    "ProjectSummary",
    "read_manifest_summary",
    "summarize_project",
]
