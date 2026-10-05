"""Shared enumerations for the platform domain model.

Keeping these in one module avoids circular imports between the tool,
permission, task and agent layers, which all need to agree on these values.
"""

from __future__ import annotations

from enum import StrEnum as _StdStrEnum


class StrEnum(_StdStrEnum):
    """String enum that serialises to its value (JSON/pydantic friendly)."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class RiskLevel(StrEnum):
    """How dangerous an operation is if executed without scrutiny."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PermissionEffect(StrEnum):
    """Outcome of a policy decision."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRES_APPROVAL = "requires_approval"


class ErrorClass(StrEnum):
    """Classification of a failure, driving recovery strategy.

    The agent never assumes the first execution succeeded; it classifies the
    error and chooses an appropriate recovery path.
    """

    TRANSIENT = "transient"  # safe to retry (network blips, rate limits)
    TIMEOUT = "timeout"  # retry with backoff or smaller scope
    VALIDATION = "validation"  # bad input; repair args, do not blindly retry
    PERMISSION = "permission"  # policy denied; requires human/approval
    NOT_FOUND = "not_found"  # missing resource; adjust plan
    CONFLICT = "conflict"  # state changed; re-read then act
    RESOURCE = "resource"  # out of quota/space; free resources or abort
    EXTERNAL = "external"  # upstream integration failed
    INTERNAL = "internal"  # bug in our code; do not retry blindly


class RunStatus(StrEnum):
    """Lifecycle of a single agent run (one user goal).

    Valid transitions are enforced by :class:`RunRecord`; see
    ``runs.base.RUN_TRANSITIONS``. ``RETRYING`` exists so a run recovered by a
    worker after a crash is distinguishable from one that never started.
    """

    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    """Lifecycle of a single task step."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class ApprovalStatus(StrEnum):
    """Lifecycle of a human-in-the-loop approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class WorkflowStatus(StrEnum):
    """Lifecycle of a workflow *definition* (not a run)."""

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class WorkflowRunStatus(StrEnum):
    """Lifecycle of one execution of a workflow version."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepRunStatus(StrEnum):
    """Lifecycle of a single workflow step execution."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class StepKind(StrEnum):
    """What a workflow step does. Each maps onto an existing capability."""

    DETERMINISTIC = "deterministic"  # a registered, sandboxed pure action
    AGENT = "agent"  # invokes the existing agent runtime
    TOOL = "tool"  # invokes an existing tool through the policy gate
    APPROVAL = "approval"  # blocks on a human decision


class StepErrorPolicy(StrEnum):
    """How the run reacts when a step fails after its retries are exhausted."""

    FAIL_RUN = "fail_run"  # the whole run fails (default)
    SKIP = "skip"  # mark the step skipped; dependents continue
    CONTINUE = "continue"  # mark the step failed; the run keeps going


class TriggerKind(StrEnum):
    """How a workflow run is started without a human calling the API."""

    MANUAL = "manual"  # an authenticated "fire now" call
    CRON = "cron"  # a schedule
    WEBHOOK = "webhook"  # an inbound HTTP call with an HMAC signature
    EVENT = "event"  # an internal domain event


class MisfirePolicy(StrEnum):
    """What a scheduler does when it discovers a schedule it missed.

    A scheduler restart, a paused process or a slow loop can leave scheduled
    occurrences in the past. The policy makes the catch-up behaviour explicit
    rather than accidental:

    * ``SKIP``    — do not run missed occurrences; jump to the next future one.
    * ``RUN_ALL`` — run every missed occurrence (bounded by ``max_catchup``).
    * ``RUN_ONE`` — run exactly one catch-up occurrence, then resume normally.
    """

    SKIP = "skip"
    RUN_ALL = "run_all"
    RUN_ONE = "run_one"


class MemoryKind(StrEnum):
    """Categories of memory, each with distinct retention/access rules."""

    CONVERSATION = "conversation"
    PREFERENCE = "preference"
    COMPANY = "company"
    TASK = "task"
    KNOWLEDGE = "knowledge"
    WORKFLOW = "workflow"


class ModelCapability(StrEnum):
    """Capabilities a model may advertise, so tasks can be routed."""

    CHAT = "chat"
    TOOL_CALLING = "tool_calling"
    VISION = "vision"
    AUDIO = "audio"
    EMBEDDING = "embedding"
    CODE = "code"


class ModelLocation(StrEnum):
    """Where a model runs.

    Used for routing and cost accounting: a ``LOCAL`` call never leaves the
    machine and never incurs a cloud API cost; a ``REMOTE`` call does.
    """

    LOCAL = "local"
    REMOTE = "remote"


class RuntimeKind(StrEnum):
    """Where a piece of work executes."""

    CLOUD = "cloud"
    LOCAL = "local"


class RuntimeState(StrEnum):
    """Lifecycle state of the local runtime process.

    The transitions are explicit and enforced by
    :class:`~arabic_agent.local.lifecycle.LifecycleStateMachine` so a runtime
    can never claim to be ``READY`` before it has started, or accept work while
    ``STOPPING``.
    """

    STARTING = "starting"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class OperatingMode(StrEnum):
    """Whether the runtime may reach the cloud.

    The mode is *declared*, not inferred from a failed request: the runtime
    knows which mode it is in, and the data boundary refuses to send local data
    to the cloud unless the mode permits it. There is no silent online access
    in ``OFFLINE``.
    """

    ONLINE = "online"
    OFFLINE = "offline"
    HYBRID = "hybrid"


class DataBoundary(StrEnum):
    """Where a unit of data is allowed to be processed.

    Every model call and every tool declares one of these, and the boundary
    policy answers deterministically whether an operation may proceed.
    """

    LOCAL = "local"  # must never leave the machine
    REMOTE = "remote"  # cloud/remote processing is acceptable
    EITHER = "either"  # no sensitivity constraint


class DataSensitivity(StrEnum):
    """How sensitive a payload is, used to derive a :class:`DataBoundary`."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"  # noqa: S105 - a sensitivity label, not a credential


class NetworkPolicy(StrEnum):
    """How a local runtime treats outbound network access from tools.

    ``DISABLED`` is the safe default: a local tool cannot silently reach the
    network. An operator opts into a wider policy explicitly.
    """

    DISABLED = "disabled"
    ALLOWLIST = "allowlist"
    USER_APPROVAL = "user_approval"
    FULL_ACCESS = "full_access"


class CapabilityRisk(StrEnum):
    """Risk class of a local capability.

    Finer-grained than :class:`RiskLevel` because local computer control ranges
    from a harmless directory listing to full keyboard/mouse control. Tools
    declare this; the policy layer maps it onto approval requirements.
    """

    READ_ONLY = "read_only"
    LOW_RISK = "low_risk"
    MODERATE_RISK = "moderate_risk"
    HIGH_RISK = "high_risk"
    CRITICAL = "critical"


class PlatformKind(StrEnum):
    """Operating-system family a platform adapter targets."""

    LINUX = "linux"
    MACOS = "macos"
    WINDOWS = "windows"
    UNKNOWN = "unknown"


class DeviceStatus(StrEnum):
    """Lifecycle of an enrolled device identity."""

    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class SyncKind(StrEnum):
    """Category of an offline synchronization action.

    Only these categories are ever synchronized. Arbitrary files and secrets are
    never on the list, and a payload is checked against the data boundary before
    it is queued.
    """

    WORKFLOW_STATUS = "workflow_status"
    TASK_STATUS = "task_status"
    APPROVED_CONFIG = "approved_config"
    SELECTED_MEMORY = "selected_memory"
    AUDIT_METADATA = "audit_metadata"
    DEVICE_STATUS = "device_status"


class SyncStatus(StrEnum):
    """Delivery state of a queued synchronization action."""

    PENDING = "pending"
    SYNCED = "synced"
    FAILED = "failed"
    REJECTED = "rejected"


class Locale(StrEnum):
    """Supported UI/content locales. Arabic is first-class, not a translation."""

    AR = "ar"
    EN = "en"


class Direction(StrEnum):
    """Text direction derived from locale."""

    RTL = "rtl"
    LTR = "ltr"
