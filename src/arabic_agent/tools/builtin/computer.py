"""Computer-use tools: see the screen and operate the machine.

These are ordinary :class:`~arabic_agent.tools.base.Tool` instances, so they run
through the existing executor: the permission/policy layer decides allow / deny /
requires-approval, approvals pause the run, and every call is audited. The tools
themselves contain no OS logic — they translate model arguments into a
:class:`~arabic_agent.computer.runtime.ComputerRuntime` call, which enforces
policy, budgeting, freshness and the screen-data boundary.

They are registered only for a local runtime and only when computer control is
enabled, so on a cloud build the model cannot even name them.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ...computer.base import PointerButton
from ...computer.grounding import GroundingTarget
from ...computer.runtime import ComputerRuntime
from ...core.enums import RiskLevel
from ...core.errors import ConfigurationError, ValidationError
from ...permissions.base import Permission
from ..base import Tool, ToolContext, ToolResult


def computer_runtime(context: ToolContext) -> ComputerRuntime:
    """Fetch the computer runtime from the tool context, or fail honestly."""
    runtime = context.metadata.get("computer_runtime")
    if not isinstance(runtime, ComputerRuntime):
        raise ConfigurationError("computer control is not available in this runtime")
    return runtime


def _target_from_arguments(arguments: dict[str, Any]) -> GroundingTarget | None:
    """Build a grounding target from the shared pointer/text arguments.

    Precedence: an explicit element id, then role/label, then recognised text,
    then raw coordinates. Returns ``None`` when no target is described.
    """
    element_id = arguments.get("element_id")
    role = arguments.get("role")
    label = arguments.get("label")
    if element_id or role or label:
        return GroundingTarget.by_element(
            element_id=str(element_id) if element_id else None,
            role=str(role) if role else None,
            label=str(label) if label else None,
        )
    text = arguments.get("text")
    if text:
        return GroundingTarget.by_text(str(text), exact=bool(arguments.get("exact", False)))
    x, y = arguments.get("x"), arguments.get("y")
    if x is not None and y is not None:
        return GroundingTarget.at_point(int(x), int(y))
    return None


class _ComputerTool(Tool):
    """Shared base: declares that it needs the computer runtime."""

    permissions: tuple[Permission, ...] = ()

    @staticmethod
    def _runtime(context: ToolContext) -> ComputerRuntime:
        return computer_runtime(context)


class ComputerScreenTool(_ComputerTool):
    name = "computer_screen"
    description = (
        "Capture the screen and return a structured perception of it: recognised "
        "text with boxes, the active window, and monitor geometry. Use this to "
        "see the current state before acting, and again afterwards to verify the "
        "effect. The screenshot itself is not returned; it is attached to the "
        "context for a vision model when one is available."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "monitor_id": {"type": "integer", "description": "Capture a specific monitor id"},
            "include_ocr": {"type": "boolean", "description": "Run local OCR (default true)"},
            "include_windows": {
                "type": "boolean",
                "description": "Include window list/active window (default true)",
            },
            "observation_id": {
                "type": "string",
                "description": "Return a previously captured observation instead of a new one",
            },
        },
        "required": [],
        "additionalProperties": False,
    }
    permissions = (Permission.SCREEN_READ,)
    risks: ClassVar[dict[Permission, RiskLevel]] = {Permission.SCREEN_READ: RiskLevel.HIGH}

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        existing = arguments.get("observation_id")
        if existing:
            observation = runtime.get_observation(str(existing), run_id=context.run_id)
        else:
            monitor_id = arguments.get("monitor_id")
            observation = await runtime.observe(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                run_id=context.run_id,
                monitor_id=int(monitor_id) if monitor_id is not None else None,
                include_ocr=bool(arguments.get("include_ocr", True)),
                include_windows=bool(arguments.get("include_windows", True)),
            )
        summary = observation.summary()
        return ToolResult.success(
            f"captured screen ({observation.image_width}x{observation.image_height}); "
            f"sources: {', '.join(observation.sources) or 'none'}",
            data=summary,
        )


class ComputerMetadataTool(_ComputerTool):
    name = "computer_metadata"
    description = (
        "Return the current display configuration without capturing pixels: the "
        "monitors, their bounds and scale, the pointer position and the active "
        "window. Cheap and safe to call before targeting an action."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    permissions = (Permission.SCREEN_READ,)
    risks: ClassVar[dict[Permission, RiskLevel]] = {Permission.SCREEN_READ: RiskLevel.MEDIUM}

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        metadata = await runtime.backend.get_screen_metadata()
        return ToolResult.success(
            f"{len(metadata.monitors)} monitor(s); backend {metadata.backend}",
            data={
                "monitors": [m.model_dump() for m in metadata.monitors],
                "virtual_bounds": None
                if metadata.virtual_bounds is None
                else metadata.virtual_bounds.model_dump(),
                "cursor": None if metadata.cursor is None else metadata.cursor.model_dump(),
                "backend": metadata.backend,
                "notes": list(metadata.notes),
            },
        )


class ComputerPointerTool(_ComputerTool):
    name = "computer_pointer"
    description = (
        "Move or click the mouse. Describe the target semantically (element_id, "
        "role+label, or on-screen text) or by raw x/y coordinates. Actions: "
        "move, click, double_click, right_click, scroll."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["move", "click", "double_click", "right_click", "scroll"],
            },
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "element_id": {"type": "string"},
            "role": {"type": "string"},
            "label": {"type": "string"},
            "text": {"type": "string", "description": "On-screen text to target"},
            "exact": {"type": "boolean"},
            "amount": {"type": "integer", "description": "Scroll amount (+up / -down)"},
            "observation_id": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    permissions = (Permission.POINTER_CONTROL,)
    risks: ClassVar[dict[Permission, RiskLevel]] = {Permission.POINTER_CONTROL: RiskLevel.HIGH}
    side_effecting = True

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        action = str(arguments["action"])
        observation_id = arguments.get("observation_id")
        observation_id = str(observation_id) if observation_id else None
        common = {
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "run_id": context.run_id,
        }
        if action == "scroll":
            amount = arguments.get("amount")
            if amount is None:
                raise ValidationError("scroll requires an amount")
            target = _target_from_arguments(arguments)
            await runtime.pointer_scroll(
                **common, amount=int(amount), target=target, observation_id=observation_id
            )
            return ToolResult.success(f"scrolled by {amount}")

        target = _target_from_arguments(arguments)
        if target is None:
            raise ValidationError(
                "a target is required: provide x/y, element_id, role+label, or text"
            )
        if action == "move":
            resolved = await runtime.pointer_move(
                **common, target=target, observation_id=observation_id
            )
            return ToolResult.success(
                f"moved pointer to ({resolved.point.x}, {resolved.point.y})",
                data={"point": resolved.point.model_dump(), "matched": resolved.matched},
            )
        if action in ("click", "double_click", "right_click"):
            button = PointerButton.RIGHT if action == "right_click" else PointerButton.LEFT
            resolved = await runtime.pointer_click(
                **common,
                target=target,
                button=button,
                double=action == "double_click",
                observation_id=observation_id,
            )
            return ToolResult.success(
                f"{action} at ({resolved.point.x}, {resolved.point.y})",
                data={"point": resolved.point.model_dump(), "matched": resolved.matched},
            )
        raise ValidationError(f"unknown pointer action: {action}")


class ComputerKeyboardTool(_ComputerTool):
    name = "computer_keyboard"
    description = (
        "Type text or press keys. Actions: type (Unicode text, including Arabic), "
        "key (one named key such as Return or Tab), hotkey (a chord such as "
        "ctrl+s). Text is typed into whatever currently has focus."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["type", "key", "hotkey"]},
            "text": {"type": "string"},
            "key": {"type": "string"},
            "keys": {"type": "array", "items": {"type": "string"}},
            "observation_id": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    permissions = (Permission.KEYBOARD_CONTROL,)
    risks: ClassVar[dict[Permission, RiskLevel]] = {Permission.KEYBOARD_CONTROL: RiskLevel.HIGH}
    side_effecting = True

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        action = str(arguments["action"])
        common = {
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "run_id": context.run_id,
        }
        observation_id = arguments.get("observation_id")
        observation_id = str(observation_id) if observation_id else None
        if action == "type":
            text = arguments.get("text")
            if not isinstance(text, str) or not text:
                raise ValidationError("type requires text")
            length = await runtime.keyboard_type(**common, text=text, observation_id=observation_id)
            return ToolResult.success(f"typed {length} character(s)")
        if action == "key":
            key = arguments.get("key")
            if not isinstance(key, str) or not key:
                raise ValidationError("key requires a key name")
            await runtime.keyboard_key(**common, key=key)
            return ToolResult.success(f"pressed {key}")
        if action == "hotkey":
            keys = arguments.get("keys")
            if not isinstance(keys, list) or not keys:
                raise ValidationError("hotkey requires a list of keys")
            await runtime.keyboard_hotkey(**common, keys=[str(k) for k in keys])
            return ToolResult.success(f"pressed {'+'.join(str(k) for k in keys)}")
        raise ValidationError(f"unknown keyboard action: {action}")


class ComputerWindowTool(_ComputerTool):
    name = "computer_window"
    description = (
        "Inspect and focus top-level windows. Actions: list (all windows with "
        "titles and geometry), active (the focused window), focus (bring a "
        "window to the front by id)."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "active", "focus"]},
            "window_id": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    permissions = (Permission.WINDOW_CONTROL,)
    risks: ClassVar[dict[Permission, RiskLevel]] = {Permission.WINDOW_CONTROL: RiskLevel.MEDIUM}
    side_effecting = True

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        action = str(arguments["action"])
        if action == "list":
            windows = await runtime.list_windows(run_id=context.run_id)
            return ToolResult.success(
                f"{len(windows)} window(s)",
                data={"windows": [w.model_dump() for w in windows]},
            )
        if action == "active":
            windows = await runtime.list_windows(run_id=context.run_id)
            active = next((w for w in windows if w.active), None)
            return ToolResult.success(
                "active window" if active else "no active window",
                data={"active": None if active is None else active.model_dump()},
            )
        if action == "focus":
            window_id = arguments.get("window_id")
            if not window_id:
                raise ValidationError("focus requires a window_id")
            focused = await runtime.focus_window(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                run_id=context.run_id,
                window_id=str(window_id),
            )
            return ToolResult.success(
                "window focused" if focused else "window could not be focused",
                data={"window_id": str(window_id), "focused": focused},
            )
        raise ValidationError(f"unknown window action: {action}")


class ComputerVerifyTool(_ComputerTool):
    name = "computer_verify"
    description = (
        "Re-capture the screen and report whether it changed since a previous "
        "observation. Use after an action to confirm the intended effect actually "
        "happened, rather than assuming it did."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "observation_id": {
                "type": "string",
                "description": "The observation to compare against",
            },
        },
        "required": ["observation_id"],
        "additionalProperties": False,
    }
    permissions = (Permission.SCREEN_READ,)
    risks: ClassVar[dict[Permission, RiskLevel]] = {Permission.SCREEN_READ: RiskLevel.HIGH}

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        fresh, changed = await runtime.changed_since(
            str(arguments["observation_id"]),
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            run_id=context.run_id,
        )
        return ToolResult.success(
            "screen changed" if changed else "screen did not change materially",
            data={
                "changed": changed,
                "observation_id": fresh.id,
                "active_window": None if fresh.active_window is None else fresh.active_window.title,
            },
        )


COMPUTER_TOOLS: tuple[type[Tool], ...] = (
    ComputerScreenTool,
    ComputerMetadataTool,
    ComputerPointerTool,
    ComputerKeyboardTool,
    ComputerWindowTool,
    ComputerVerifyTool,
)

#: Tool names, so the container can advertise exactly the computer tool set.
COMPUTER_TOOL_NAMES = frozenset(cls.name for cls in COMPUTER_TOOLS)

__all__ = [
    "COMPUTER_TOOLS",
    "COMPUTER_TOOL_NAMES",
    "ComputerKeyboardTool",
    "ComputerMetadataTool",
    "ComputerPointerTool",
    "ComputerScreenTool",
    "ComputerVerifyTool",
    "ComputerWindowTool",
    "computer_runtime",
]
