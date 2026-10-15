"""The five built-in specialists.

Each is a :class:`~arabic_agent.specialists.base.Specialist` — a scope over the
one shared core, not an engine of its own. The tool scopes below are the tools
each specialist *advertises*; the executor still enforces permissions, approvals
and (now) the scope itself, so a specialist cannot reach a tool it did not
declare even if a model hallucinates the name.

Tool scopes are curated from the real registry. When a tool is absent because an
optional extra is not installed, :meth:`Specialist.scoped_names` intersects the
scope with what exists, and readiness reporting shows the gap — the run degrades
to available tools instead of failing on a missing one.
"""

from __future__ import annotations

from ..core.enums import ModelCapability
from .base import CODING, EDUCATION, GENERAL, VIDEO, VOICE, Specialist

#: Primitives every specialist keeps so the loop can read, act and finish. The
#: runner always advertises the core set alongside a step's tool, so these must
#: be allowed by every scope or a step would advertise a tool it may not call.
_COMMON = frozenset(
    {
        "finish",
        "echo",
        "read_file",
        "write_file",
        "list_directory",
        "read_file_range",
        "edit_file",
        "append_file",
        "create_file",
        "copy_file",
        "move_file",
        "delete_file",
        "make_directory",
        "file_info",
        "directory_tree",
        "search_files",
        "file_type",
    }
)

_DOCUMENT = frozenset(
    {
        "generate_document",
        "create_spreadsheet",
        "create_presentation",
        "document_generation_formats",
        "document_editing_formats",
        "edit_document",
        "export_document",
        "inspect_document",
        "read_document",
        "search_document",
        "inspect_archive",
        "list_documents",
        "inspect_project",
    }
)

_GENERAL = Specialist(
    id=GENERAL,
    name_ar="الوكيل العام",
    name_en="General Agent",
    description_ar=(
        "وكيل تشغيل عام يستطيع التخطيط والتنفيذ عبر كل الأدوات المتاحة: المستندات، "
        "الملفات، البحث في الوثائق، والعمليات المعتمدة على الحاسوب والمتصفح."
    ),
    description_en=(
        "General-purpose operator that plans and executes across every available "
        "tool: documents, files, document search, and approved computer/browser actions."
    ),
    #: Empty scope == the whole registry. The general agent is the default.
    tool_scope=frozenset(),
    required_capabilities=frozenset({ModelCapability.CHAT, ModelCapability.TOOL_CALLING}),
    domains=("documents", "files", "research", "business"),
)

_VOICE = Specialist(
    id=VOICE,
    name_ar="المساعد الصوتي والشخصي",
    name_en="Voice & Personal Assistant",
    description_ar=(
        "مساعد صوتي عربي: يفهم أمرًا منطوقًا، ينفّذه عبر الأدوات، ويجيب بصوت. "
        "يعتمد على أدوات الصوت والتحويل النصي المسجّلة في وقت التشغيل."
    ),
    description_en=(
        "Arabic voice assistant: understands a spoken command, executes it with tools, "
        "and answers by voice. Uses the speech tools registered at runtime."
    ),
    tool_scope=_COMMON
    | _DOCUMENT
    | frozenset(
        {
            "list_printers",
            "print_document",
            "speech_to_text",
            "text_to_speech",
            "speech_capabilities",
            "http_request",
            "webhook_send",
            "calendar_read",
            "calendar_write",
            "calendar_delete",
            "connector_verify",
        }
    ),
    required_capabilities=frozenset({ModelCapability.CHAT, ModelCapability.TOOL_CALLING}),
    domains=("voice", "personal", "documents"),
)

_VIDEO = Specialist(
    id=VIDEO,
    name_ar="استوديو الفيديو والمحتوى",
    name_en="Video & Content Studio",
    description_ar=(
        "مساحة إنتاج وسائط: نص إلى صورة، قصة، لوحة قصص، مشاهد، تعليق صوتي عربي، "
        "ترجمات، وتجميع فيديو MP4 حقيقي عبر أدوات الاستوديو."
    ),
    description_en=(
        "Media production workspace: text-to-image, script, storyboard, scenes, Arabic "
        "voiceover, subtitles, and real MP4 composition through the studio tools."
    ),
    tool_scope=_COMMON
    | _DOCUMENT
    | frozenset(
        {
            "studio_project_create",
            "studio_project_status",
            "studio_storyboard",
            "studio_scene_render",
            "studio_voiceover",
            "studio_subtitles",
            "studio_compose",
            "studio_capabilities",
            "text_to_speech",
            "speech_capabilities",
            "image_generate",
        }
    ),
    required_capabilities=frozenset({ModelCapability.CHAT, ModelCapability.TOOL_CALLING}),
    domains=("video", "image", "content"),
)

_CODING = Specialist(
    id=CODING,
    name_ar="وكيل البرمجة",
    name_en="Coding Agent",
    description_ar=(
        "وكيل تطوير برمجي: يحلّل المستودع، يشرح المعمارية، يحدد الأخطاء، يعدّل الملفات، "
        "يكتب الاختبارات، يشغّل أوامر مسموحًا بها، ويجهّز مراجعة وفرقًا قابلًا للتطبيق."
    ),
    description_en=(
        "Software development agent: analyses a repository, explains architecture, "
        "finds bugs, edits files, writes tests, runs permitted commands, and produces a "
        "reviewable diff."
    ),
    tool_scope=_COMMON
    | frozenset(
        {
            "inspect_project",
            "run_local_command",
            "search_local_files",
            "read_local_file",
            "list_local_directory",
            "code_analyze",
            "code_review",
            "git_diff",
            "document_generation_formats",
            "document_editing_formats",
        }
    ),
    #: Reading the repository is not optional for a coding agent; these stay even
    #: if a scope is edited.
    extra_tools=frozenset({"inspect_project", "read_file", "search_files"}),
    required_capabilities=frozenset(
        {ModelCapability.CHAT, ModelCapability.TOOL_CALLING, ModelCapability.CODE}
    ),
    domains=("code", "repository", "testing"),
)

_EDUCATION = Specialist(
    id=EDUCATION,
    name_ar="الوكيل التعليمي",
    name_en="Education Agent",
    description_ar=(
        "متخصص تعليمي: يخطّط المقررات، يحدد مخرجات التعلم، ينشئ الدروس والتمارين "
        "والاختبارات، وينتج دروسًا مصوّرة بالتعليق الصوتي عبر استوديو الفيديو."
    ),
    description_en=(
        "Education specialist: plans courses, defines learning outcomes, generates "
        "lessons, exercises and quizzes, and produces narrated lessons via the video "
        "studio."
    ),
    tool_scope=_COMMON
    | _DOCUMENT
    | frozenset(
        {
            "education_course_plan",
            "education_lesson",
            "education_quiz",
            "studio_project_create",
            "studio_storyboard",
            "studio_scene_render",
            "studio_voiceover",
            "studio_subtitles",
            "studio_compose",
            "studio_capabilities",
            "text_to_speech",
            "speech_capabilities",
        }
    ),
    required_capabilities=frozenset({ModelCapability.CHAT, ModelCapability.TOOL_CALLING}),
    domains=("education", "content", "video"),
)

BUILTIN: dict[str, Specialist] = {s.id: s for s in (_GENERAL, _VOICE, _VIDEO, _CODING, _EDUCATION)}

DEFAULT_SPECIALIST = _GENERAL
