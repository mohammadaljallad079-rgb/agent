"""Cron expression parsing and evaluation.

A small, dependency-free implementation of standard 5-field cron with timezone
support. It exists so scheduling behaviour is fully under our control and
auditable, rather than delegated to a library whose misfire semantics we would
still have to wrap.

Fields (in order): minute, hour, day-of-month, month, day-of-week.

Supported syntax per field:

* ``*``            — any value
* ``a``            — a single value
* ``a-b``          — an inclusive range
* ``a-b/n`` / ``*/n`` — a stepped range
* ``a,b,c``        — a list of any of the above

Day-of-week accepts ``0`` (Sunday) through ``6`` (Saturday); ``7`` is accepted
as Sunday too. Named macros ``@hourly``, ``@daily``/``@midnight``, ``@weekly``,
``@monthly`` and ``@yearly``/``@annually`` are expanded.

Day-of-month / day-of-week semantics follow Vixie cron: when *both* are
restricted (neither is ``*``), a day matches if *either* field matches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.errors import ValidationError

#: Search horizon for ``next_after`` (a yearly expression is found well inside
#: this; the bound only exists so a pathological expression cannot spin forever).
_MAX_SEARCH_DAYS = 366 * 5

_MACROS: dict[str, str] = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}

_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")
_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def load_timezone(name: str) -> ZoneInfo:
    """Return a :class:`ZoneInfo` for ``name``, raising a clear error if unknown."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(f"unknown timezone: {name}", details={"timezone": name}) from exc


def _parse_field(spec: str, *, name: str, low: int, high: int) -> frozenset[int]:
    if spec == "":
        raise ValidationError(f"cron {name} field is empty")
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValidationError(f"cron {name} field has an empty element")
        step = 1
        if "/" in part:
            base, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError as exc:
                raise ValidationError(f"cron {name} field has an invalid step: {part!r}") from exc
            if step <= 0:
                raise ValidationError(f"cron {name} field step must be positive: {part!r}")
            part = base
        if part == "*":
            start, end = low, high
        elif "-" in part[1:]:
            left, _, right = part.partition("-")
            try:
                start, end = int(left), int(right)
            except ValueError as exc:
                raise ValidationError(f"cron {name} field has an invalid range: {part!r}") from exc
        else:
            try:
                value = int(part)
            except ValueError as exc:
                raise ValidationError(f"cron {name} field has an invalid value: {part!r}") from exc
            start = end = value
        if start > end:
            raise ValidationError(f"cron {name} field range is reversed: {part!r}")
        if start < low or end > high:
            raise ValidationError(f"cron {name} field is out of range [{low}, {high}]: {part!r}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class CronExpression:
    """A parsed, validated cron expression with timezone-aware evaluation."""

    minute: frozenset[int]
    hour: frozenset[int]
    day_of_month: frozenset[int]
    month: frozenset[int]
    day_of_week: frozenset[int]
    #: Whether the DOM/DOW fields were restricted (neither ``*``).
    dom_restricted: bool
    dow_restricted: bool
    source: str

    @classmethod
    def parse(cls, expression: str) -> CronExpression:
        """Parse ``expression`` (5 fields or a macro). Raises ValidationError."""
        if expression is None or not expression.strip():
            raise ValidationError("cron expression is empty")
        text = expression.strip()
        expanded = _MACROS.get(text.lower())
        if expanded is not None:
            text = expanded
        fields = text.split()
        if len(fields) != 5:
            raise ValidationError(
                "cron expression must have exactly 5 fields",
                details={"expression": expression},
            )
        parsed = [
            _parse_field(
                spec, name=_FIELD_NAMES[i], low=_FIELD_BOUNDS[i][0], high=_FIELD_BOUNDS[i][1]
            )
            for i, spec in enumerate(fields)
        ]
        # Normalise Sunday: cron allows both 0 and 7.
        dow = parsed[4]
        if 7 in dow:
            dow = (dow - {7}) | {0}
        return cls(
            minute=parsed[0],
            hour=parsed[1],
            day_of_month=parsed[2],
            month=parsed[3],
            day_of_week=frozenset(dow),
            dom_restricted=fields[2].strip() != "*",
            dow_restricted=fields[4].strip() != "*",
            source=expression.strip(),
        )

    def _day_matches(self, day: datetime) -> bool:
        if day.month not in self.month:
            return False
        dom_ok = day.day in self.day_of_month
        # Python: Monday=0..Sunday=6; cron: Sunday=0..Saturday=6.
        cron_dow = (day.weekday() + 1) % 7
        dow_ok = cron_dow in self.day_of_week
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        if self.dom_restricted:
            return dom_ok
        if self.dow_restricted:
            return dow_ok
        return True

    def _times(self) -> list[tuple[int, int]]:
        return sorted((h, m) for h in self.hour for m in self.minute)

    def matches(self, moment: datetime) -> bool:
        """Whether ``moment`` (timezone-aware) falls exactly on this schedule."""
        local = moment.astimezone(moment.tzinfo or UTC)
        if local.second != 0 or local.microsecond != 0:
            return False
        if local.minute not in self.minute or local.hour not in self.hour:
            return False
        return self._day_matches(local)

    def next_after(self, after: datetime, *, tz: ZoneInfo) -> datetime:
        """The first scheduled instant strictly after ``after``, in ``tz``.

        ``after`` may be naive (assumed UTC) or aware. The result is always
        timezone-aware in ``tz``.
        """
        if after.tzinfo is None:
            after = after.replace(tzinfo=UTC)
        local = after.astimezone(tz)
        # Start from the next whole minute.
        start = local.replace(second=0, microsecond=0) + timedelta(minutes=1)
        times = self._times()
        if not times:
            raise ValidationError("cron expression matches no time")

        current_date = start.date()
        for offset in range(_MAX_SEARCH_DAYS):
            day = current_date + timedelta(days=offset)
            probe = datetime(day.year, day.month, day.day, tzinfo=tz)
            if not self._day_matches(probe):
                continue
            same_day = offset == 0
            for hour, minute in times:
                if same_day and (hour, minute) < (start.hour, start.minute):
                    continue
                candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                if candidate <= local:
                    continue
                return candidate
        raise ValidationError(
            "cron expression has no occurrence within the search horizon",
            details={"expression": self.source},
        )

    def previous_or_equal(self, moment: datetime, *, tz: ZoneInfo) -> datetime | None:
        """The most recent scheduled instant at or before ``moment`` (or None).

        Used by the scheduler to compute a catch-up window. Bounded to one year.
        """
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        local = moment.astimezone(tz).replace(second=0, microsecond=0)
        times = self._times()
        current_date = local.date()
        for offset in range(0, 366 + 1):
            day = current_date - timedelta(days=offset)
            probe = datetime(day.year, day.month, day.day, tzinfo=tz)
            if not self._day_matches(probe):
                continue
            for hour, minute in reversed(times):
                candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                if candidate <= local:
                    return candidate
        return None


def validate_cron(expression: str) -> CronExpression:
    """Parse ``expression`` and return it, raising :class:`ValidationError`."""
    return CronExpression.parse(expression)
