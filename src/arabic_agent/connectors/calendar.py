"""Provider-neutral calendar connector.

Calendar (Google Calendar, Microsoft Graph, CalDAV) is expressed through one
provider-neutral abstraction: a *calendar*, an *event* with a start/end and
optional attendees, a *free/busy* query, and a *result*. A provider is an adapter
that implements :class:`CalendarProvider`; the connector is the agent-facing
wrapper that classifies operations and gates writes.

The provider shipped here is the **official Google Calendar API v3** adapter,
implemented against Google's documented REST endpoints. It is real code that
talks to the real API given OAuth credentials; it supports both a ready access
token and a refresh-token exchange, so a long-lived deployment stays
authenticated without a human re-consenting. When no credentials are present it
reports ``NOT_CONFIGURED`` — it never pretends a calendar is reachable.

Two rules are structural, not stylistic:

* **A calendar write is a consequential action.** Creating, moving or deleting
  an event changes other people's time and usually notifies them. The risk is
  *declared* by the operation, never inferred from the model's phrasing, and the
  existing :class:`~arabic_agent.connectors.runtime.ConnectorRuntime` enforces
  approval binding, the idempotency ledger and verification around it.
* **No provider schema leaks into the agent core.** The tool surface speaks in
  normalized events; only this adapter knows Google's wire format.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict

from ..core.enums import ActionRisk, ConnectorKind, ConnectorState
from ..core.errors import (
    ConfigurationError,
    ExternalServiceError,
    TransientError,
    ValidationError,
)
from .base import (
    Connector,
    ConnectorCall,
    ConnectorCapability,
    ConnectorHealth,
    ConnectorOperation,
    ConnectorResult,
    OperationKind,
)
from .credentials import CredentialRef, CredentialResolver
from .retry import RetryPolicy, is_retryable_status, with_retry

#: Google Calendar's default API root and OAuth token endpoint.
GOOGLE_CALENDAR_BASE_URL = "https://www.googleapis.com/calendar/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret

#: Largest response accepted from the provider (2 MiB) — a calendar page never
#: legitimately exceeds this; a larger body is treated as hostile/misconfigured.
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

#: Applied when the model does not name a calendar. ``primary`` is Google's
#: stable alias for the authenticated user's main calendar.
_DEFAULT_CALENDAR = "primary"


class CalendarEvent(BaseModel):
    """A normalized calendar event, provider-neutral."""

    model_config = ConfigDict(frozen=True)

    id: str = ""
    calendar_id: str = ""
    summary: str = ""
    description: str = ""
    location: str = ""
    #: RFC3339 start/end. For an all-day event these are the date (``YYYY-MM-DD``).
    start: str = ""
    end: str = ""
    all_day: bool = False
    #: ``confirmed`` / ``tentative`` / ``cancelled`` (provider-normalized).
    status: str = ""
    attendees: tuple[str, ...] = ()
    organizer: str = ""
    html_link: str = ""
    updated: str = ""


class FreeBusySlot(BaseModel):
    """A busy interval returned by a free/busy query."""

    model_config = ConfigDict(frozen=True)

    start: str
    end: str


class CalendarListEntry(BaseModel):
    """One calendar the account can see."""

    model_config = ConfigDict(frozen=True)

    id: str
    summary: str = ""
    primary: bool = False
    time_zone: str = ""
    access_role: str = ""


class CalendarProvider(ABC):
    """A provider adapter: the only provider-specific code in calendar support."""

    name: str

    @abstractmethod
    async def list_calendars(self) -> list[CalendarListEntry]:
        """List the calendars this account can access."""

    @abstractmethod
    async def list_events(
        self,
        *,
        calendar_id: str,
        time_min: str = "",
        time_max: str = "",
        max_results: int = 50,
        query: str = "",
    ) -> list[CalendarEvent]:
        """List events in a time window (optionally text-filtered)."""

    @abstractmethod
    async def get_event(self, *, calendar_id: str, event_id: str) -> CalendarEvent:
        """Fetch one event."""

    @abstractmethod
    async def free_busy(
        self, *, calendar_id: str, time_min: str, time_max: str
    ) -> list[FreeBusySlot]:
        """Return the busy intervals in a window."""

    @abstractmethod
    async def create_event(self, *, calendar_id: str, event: CalendarEvent) -> CalendarEvent:
        """Create an event and return it as the provider stored it."""

    @abstractmethod
    async def update_event(self, *, calendar_id: str, event: CalendarEvent) -> CalendarEvent:
        """Patch an event and return it as the provider stored it."""

    @abstractmethod
    async def delete_event(self, *, calendar_id: str, event_id: str) -> bool:
        """Delete an event; return whether the provider accepted it."""

    def state(self) -> ConnectorState:
        return ConnectorState.READY

    def detail(self) -> str:
        return "configured"

    async def verify(self, event_id: str, *, calendar_id: str = "") -> CalendarEvent | None:
        """Look an event up by id to confirm a write (timeout safety)."""
        return None

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Normalization helpers (provider JSON -> normalized models)
# ---------------------------------------------------------------------------


def _require_attendee_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    raise ValidationError("attendees must be a string or a list of strings")


def _validate_instant(value: str, *, field: str) -> None:
    """Reject a value that is neither an RFC3339 instant nor an ISO date."""
    text = (value or "").strip()
    if not text:
        raise ValidationError(f"{field} is required")
    iso = text.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(iso)
        return
    except ValueError:
        pass
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(
            f"{field} must be an RFC3339 instant or an ISO date", details={"value": text}
        ) from exc


def _looks_all_day(start: str) -> bool:
    """An all-day event carries a date, not a date-time."""
    return "T" not in (start or "")


def normalized_event(payload: dict[str, Any], *, calendar_id: str = "") -> CalendarEvent:
    """Map a provider event object onto :class:`CalendarEvent`.

    Accepts Google Calendar's ``start``/``end`` shape (``{"dateTime": ...}`` or
    ``{"date": ...}``) and a flat ``{start, end}`` shape, so the same normalizer
    serves every provider.
    """

    def field(name: str) -> str:
        raw = payload.get(name)
        if isinstance(raw, dict):
            return str(raw.get("dateTime") or raw.get("date") or "")
        return str(raw or "")

    start = field("start")
    end = field("end")
    attendees: list[str] = []
    for item in payload.get("attendees") or []:
        if isinstance(item, dict):
            email = item.get("email")
            if email:
                attendees.append(str(email))
        elif item:
            attendees.append(str(item))
    organizer = payload.get("organizer")
    organizer_email = organizer.get("email") if isinstance(organizer, dict) else organizer
    return CalendarEvent(
        id=str(payload.get("id") or ""),
        calendar_id=str(payload.get("calendarId") or calendar_id),
        summary=str(payload.get("summary") or ""),
        description=str(payload.get("description") or ""),
        location=str(payload.get("location") or ""),
        start=start,
        end=end,
        all_day=_looks_all_day(start),
        status=str(payload.get("status") or ""),
        attendees=tuple(attendees),
        organizer=str(organizer_email or ""),
        html_link=str(payload.get("htmlLink") or payload.get("html_link") or ""),
        updated=str(payload.get("updated") or ""),
    )


def _event_body(event: CalendarEvent, *, calendar_id: str) -> dict[str, Any]:
    """Build a provider request body from a normalized event, validating times."""
    _validate_instant(event.start, field="start")
    _validate_instant(event.end, field="end")
    all_day = event.all_day or _looks_all_day(event.start)
    if all_day:
        start: dict[str, Any] = {"date": event.start}
        end: dict[str, Any] = {"date": event.end or event.start}
    else:
        start = {"dateTime": event.start}
        end = {"dateTime": event.end}
    body: dict[str, Any] = {"start": start, "end": end}
    if event.summary:
        body["summary"] = event.summary
    if event.description:
        body["description"] = event.description
    if event.location:
        body["location"] = event.location
    if event.attendees:
        body["attendees"] = [{"email": email} for email in event.attendees]
    return body


class GoogleCalendarProvider(CalendarProvider):
    """Official Google Calendar API v3 adapter.

    Real code for the real API. It authenticates with either a pre-existing
    OAuth access token or a refresh-token exchange, so a long-running deployment
    can stay authenticated without a human in the loop. It never scrapes or
    reverse-engineers anything.
    """

    name = "google_calendar"

    def __init__(
        self,
        *,
        base_url: str = GOOGLE_CALENDAR_BASE_URL,
        token_url: str = GOOGLE_TOKEN_URL,
        access_token: CredentialRef | None = None,
        refresh_token: CredentialRef | None = None,
        client_id: CredentialRef | None = None,
        client_secret: CredentialRef | None = None,
        credentials: CredentialResolver | None = None,
        timeout_seconds: float = 20.0,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        retry: RetryPolicy | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_url = token_url
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._credentials = credentials or CredentialResolver()
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._retry = retry or RetryPolicy()
        self._client = client
        #: Cached access token from a refresh exchange (value never logged).
        self._cached_token: str = ""
        self._cached_token_expiry: float = 0.0

    # -- State ----------------------------------------------------------
    def state(self) -> ConnectorState:
        has_access = self._access_token is not None
        has_refresh = (
            self._refresh_token is not None
            and self._client_id is not None
            and self._client_secret is not None
        )
        if not has_access and not has_refresh:
            return ConnectorState.NOT_CONFIGURED
        return ConnectorState.READY

    def detail(self) -> str:
        if self.state() is ConnectorState.NOT_CONFIGURED:
            return "official Google Calendar API v3: no OAuth credential configured"
        if self._refresh_token is not None:
            return "official Google Calendar API v3 (refresh-token exchange)"
        return "official Google Calendar API v3 (static access token)"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    # -- Auth -----------------------------------------------------------
    async def credential_ready(self) -> bool:
        """Whether a usable OAuth credential is actually present.

        A named environment variable that is unset is *not* a credential; this
        checks presence rather than merely whether a reference was configured, so
        the connector reports ``NOT_CONFIGURED`` instead of masquerading as ready.
        """
        if self._access_token is not None and await self._credentials.has(self._access_token):
            return True
        if self._refresh_token is None or self._client_id is None or self._client_secret is None:
            return False
        return (
            await self._credentials.has(self._refresh_token)
            and await self._credentials.has(self._client_id)
            and await self._credentials.has(self._client_secret)
        )

    async def _bearer(self) -> str:
        """Resolve an access token, refreshing when only a refresh token exists."""
        if self._access_token is not None:
            credential = await self._credentials.resolve(self._access_token)
            if credential is None:
                raise ConfigurationError("google calendar access token is not available")
            return credential.reveal()
        import time

        if self._cached_token and time.monotonic() < self._cached_token_expiry:
            return self._cached_token
        return await self._refresh_access_token()

    async def _refresh_access_token(self) -> str:
        import time

        if self._refresh_token is None or self._client_id is None or self._client_secret is None:
            raise ConfigurationError("google calendar refresh credentials are incomplete")
        refresh = await self._credentials.resolve(self._refresh_token)
        client_id = await self._credentials.resolve(self._client_id)
        client_secret = await self._credentials.resolve(self._client_secret)
        if refresh is None or client_id is None or client_secret is None:
            raise ConfigurationError("google calendar refresh credentials are not available")
        client = await self._get_client()
        response = await client.post(
            self._token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id.reveal(),
                "client_secret": client_secret.reveal(),
                "refresh_token": refresh.reveal(),
            },
        )
        if response.status_code >= 400:
            if is_retryable_status(response.status_code):
                raise TransientError(
                    f"google token endpoint returned {response.status_code}",
                )
            # Never echo the body: it can contain token material.
            raise ConfigurationError(
                f"google token exchange failed with status {response.status_code}"
            )
        payload = response.json()
        token = str(payload.get("access_token") or "")
        if not token:
            raise ExternalServiceError("google token exchange returned no access_token")
        expires_in = float(payload.get("expires_in") or 3600)
        self._cached_token = token
        # Refresh a minute early so a request never races the expiry.
        self._cached_token_expiry = time.monotonic() + max(0.0, expires_in - 60.0)
        return token

    # -- HTTP -----------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        client = await self._get_client()
        url = f"{self._base_url}{path}"
        token = await self._bearer()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        async def attempt(_n: int) -> httpx.Response:
            response = await client.request(
                method, url, params=params, json=json_body, headers=headers
            )
            if is_retryable_status(response.status_code):
                raise TransientError(
                    f"google calendar returned {response.status_code} for {method} {path}"
                )
            return response

        response = await with_retry(attempt, policy=self._retry)
        if len(response.content) > self._max_response_bytes:
            raise ExternalServiceError(
                "the calendar response exceeded the configured size limit",
                details={"path": path, "bytes": len(response.content)},
            )
        return response

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, operation: str) -> None:
        if response.status_code < 400:
            return
        if response.status_code == 404:
            from ..core.errors import NotFoundError

            raise NotFoundError(
                "the calendar resource was not found",
                details={"operation": operation, "status": response.status_code},
            )
        if response.status_code in (401, 403):
            raise ConfigurationError(
                f"google calendar refused the request ({response.status_code}); "
                "the credential may be missing a required scope",
                details={"operation": operation},
            )
        raise ExternalServiceError(
            f"google calendar {operation} failed with status {response.status_code}",
            details={"operation": operation, "status": response.status_code},
        )

    # -- Reads ----------------------------------------------------------
    async def list_calendars(self) -> list[CalendarListEntry]:
        response = await self._request("GET", "/users/me/calendarList")
        self._raise_for_status(response, operation="calendarList")
        items = response.json().get("items") or []
        entries: list[CalendarListEntry] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            entries.append(
                CalendarListEntry(
                    id=str(item.get("id") or ""),
                    summary=str(item.get("summary") or ""),
                    primary=bool(item.get("primary")),
                    time_zone=str(item.get("timeZone") or ""),
                    access_role=str(item.get("accessRole") or ""),
                )
            )
        return entries

    async def list_events(
        self,
        *,
        calendar_id: str,
        time_min: str = "",
        time_max: str = "",
        max_results: int = 50,
        query: str = "",
    ) -> list[CalendarEvent]:
        if time_min:
            _validate_instant(time_min, field="time_min")
        if time_max:
            _validate_instant(time_max, field="time_max")
        params: dict[str, Any] = {
            "maxResults": max(1, min(max_results, 250)),
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        if time_min:
            params["timeMin"] = time_min
        if time_max:
            params["timeMax"] = time_max
        if query:
            params["q"] = query
        path = f"/calendars/{quote(calendar_id or _DEFAULT_CALENDAR, safe='')}/events"
        response = await self._request("GET", path, params=params)
        self._raise_for_status(response, operation="events.list")
        items = response.json().get("items") or []
        return [normalized_event(item, calendar_id=calendar_id) for item in items]

    async def get_event(self, *, calendar_id: str, event_id: str) -> CalendarEvent:
        if not event_id:
            raise ValidationError("event_id is required")
        path = (
            f"/calendars/{quote(calendar_id or _DEFAULT_CALENDAR, safe='')}"
            f"/events/{quote(event_id, safe='')}"
        )
        response = await self._request("GET", path)
        self._raise_for_status(response, operation="events.get")
        return normalized_event(response.json(), calendar_id=calendar_id)

    async def free_busy(
        self, *, calendar_id: str, time_min: str, time_max: str
    ) -> list[FreeBusySlot]:
        _validate_instant(time_min, field="time_min")
        _validate_instant(time_max, field="time_max")
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": calendar_id or _DEFAULT_CALENDAR}],
        }
        response = await self._request("POST", "/freeBusy", json_body=body)
        self._raise_for_status(response, operation="freeBusy")
        calendars = response.json().get("calendars") or {}
        entry = calendars.get(calendar_id or _DEFAULT_CALENDAR) or {}
        slots: list[FreeBusySlot] = []
        for busy in entry.get("busy") or []:
            if not isinstance(busy, dict):
                continue
            slots.append(
                FreeBusySlot(
                    start=str(busy.get("start") or ""),
                    end=str(busy.get("end") or ""),
                )
            )
        return slots

    # -- Writes ---------------------------------------------------------
    async def create_event(self, *, calendar_id: str, event: CalendarEvent) -> CalendarEvent:
        path = f"/calendars/{quote(calendar_id or _DEFAULT_CALENDAR, safe='')}/events"
        response = await self._request(
            "POST", path, json_body=_event_body(event, calendar_id=calendar_id)
        )
        self._raise_for_status(response, operation="events.insert")
        return normalized_event(response.json(), calendar_id=calendar_id)

    async def update_event(self, *, calendar_id: str, event: CalendarEvent) -> CalendarEvent:
        if not event.id:
            raise ValidationError("event id is required to update an event")
        path = (
            f"/calendars/{quote(calendar_id or _DEFAULT_CALENDAR, safe='')}"
            f"/events/{quote(event.id, safe='')}"
        )
        response = await self._request(
            "PATCH", path, json_body=_event_body(event, calendar_id=calendar_id)
        )
        self._raise_for_status(response, operation="events.patch")
        return normalized_event(response.json(), calendar_id=calendar_id)

    async def delete_event(self, *, calendar_id: str, event_id: str) -> bool:
        if not event_id:
            raise ValidationError("event_id is required to delete an event")
        path = (
            f"/calendars/{quote(calendar_id or _DEFAULT_CALENDAR, safe='')}"
            f"/events/{quote(event_id, safe='')}"
        )
        response = await self._request("DELETE", path)
        if response.status_code in (200, 204):
            return True
        self._raise_for_status(response, operation="events.delete")
        return False

    async def verify(self, event_id: str, *, calendar_id: str = "") -> CalendarEvent | None:
        try:
            return await self.get_event(
                calendar_id=calendar_id or _DEFAULT_CALENDAR, event_id=event_id
            )
        except Exception:
            return None

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()


class CalendarConnector(Connector):
    """Normalized calendar operations over a :class:`CalendarProvider`."""

    kind = ConnectorKind.CALENDAR
    capabilities = (
        ConnectorCapability.CALENDAR_LIST,
        ConnectorCapability.CALENDAR_READ,
        ConnectorCapability.CALENDAR_CREATE,
        ConnectorCapability.CALENDAR_UPDATE,
        ConnectorCapability.CALENDAR_DELETE,
        ConnectorCapability.CALENDAR_FREEBUSY,
    )

    def __init__(
        self,
        *,
        name: str,
        provider: CalendarProvider,
        default_calendar: str = _DEFAULT_CALENDAR,
    ) -> None:
        self.name = name
        self._provider = provider
        self._default_calendar = default_calendar or _DEFAULT_CALENDAR
        self.operations = (
            ConnectorOperation(
                name="calendar.list", kind=OperationKind.READ, risk=ActionRisk.READ_ONLY
            ),
            ConnectorOperation(
                name="calendar.read", kind=OperationKind.READ, risk=ActionRisk.READ_ONLY
            ),
            ConnectorOperation(
                name="calendar.freebusy", kind=OperationKind.READ, risk=ActionRisk.READ_ONLY
            ),
            ConnectorOperation(
                name="calendar.create",
                kind=OperationKind.WRITE,
                risk=ActionRisk.CONSEQUENTIAL_WRITE,
            ),
            ConnectorOperation(
                name="calendar.update",
                kind=OperationKind.WRITE,
                risk=ActionRisk.CONSEQUENTIAL_WRITE,
            ),
            ConnectorOperation(
                name="calendar.delete",
                kind=OperationKind.WRITE,
                risk=ActionRisk.HIGH_RISK_ACTION,
            ),
        )
        super().__init__()

    @property
    def provider(self) -> CalendarProvider:
        return self._provider

    # -- Health ---------------------------------------------------------
    def health(self) -> ConnectorHealth:
        # Synchronous health cannot probe the credential store; it reports the
        # *configuration* honestly and leaves authentication to healthcheck().
        state = self._provider.state()
        return ConnectorHealth(
            state=state,
            configured=state is not ConnectorState.NOT_CONFIGURED,
            authenticated=state is ConnectorState.READY,
            read_only=False,
            detail=self._provider.detail(),
        )

    async def healthcheck(self) -> ConnectorHealth:
        provider = self._provider
        ready = (
            await provider.credential_ready() if hasattr(provider, "credential_ready") else False
        )
        if ready:
            return ConnectorHealth(
                state=ConnectorState.READY,
                configured=True,
                authenticated=True,
                read_only=False,
                detail=provider.detail(),
            )
        return ConnectorHealth(
            state=ConnectorState.NOT_CONFIGURED,
            configured=False,
            authenticated=False,
            read_only=False,
            detail="official Google Calendar API v3: OAuth credentials are not configured",
        )

    # -- Execution ------------------------------------------------------
    async def call(self, call: ConnectorCall) -> ConnectorResult:
        payload = dict(call.payload)
        match call.operation:
            case "calendar.list":
                calendars = await self._provider.list_calendars()
                return ConnectorResult.success(
                    f"found {len(calendars)} calendar(s)",
                    data={
                        "count": len(calendars),
                        "calendars": [c.model_dump() for c in calendars],
                    },
                )
            case "calendar.read":
                return await self._read(payload)
            case "calendar.freebusy":
                return await self._freebusy(payload)
            case "calendar.create":
                return await self._create(payload)
            case "calendar.update":
                return await self._update(payload)
            case "calendar.delete":
                return await self._delete(payload)
            case _:
                raise ValidationError(f"unsupported calendar operation: {call.operation!r}")

    def _calendar_id(self, payload: dict[str, Any]) -> str:
        return str(payload.get("calendar_id") or self._default_calendar)

    async def _read(self, payload: dict[str, Any]) -> ConnectorResult:
        calendar_id = self._calendar_id(payload)
        event_id = str(payload.get("event_id") or "")
        if event_id:
            event = await self._provider.get_event(calendar_id=calendar_id, event_id=event_id)
            return ConnectorResult.success(f"event {event.id}", data={"event": event.model_dump()})
        events = await self._provider.list_events(
            calendar_id=calendar_id,
            time_min=str(payload.get("time_min") or ""),
            time_max=str(payload.get("time_max") or ""),
            max_results=int(payload.get("max_results") or 50),
            query=str(payload.get("query") or ""),
        )
        return ConnectorResult.success(
            f"found {len(events)} event(s)",
            data={"count": len(events), "events": [e.model_dump() for e in events]},
        )

    async def _freebusy(self, payload: dict[str, Any]) -> ConnectorResult:
        slots = await self._provider.free_busy(
            calendar_id=self._calendar_id(payload),
            time_min=str(payload.get("time_min") or ""),
            time_max=str(payload.get("time_max") or ""),
        )
        return ConnectorResult.success(
            f"{len(slots)} busy interval(s)",
            data={"count": len(slots), "busy": [s.model_dump() for s in slots]},
        )

    def _event_from_payload(self, payload: dict[str, Any]) -> CalendarEvent:
        return CalendarEvent(
            id=str(payload.get("event_id") or ""),
            calendar_id=self._calendar_id(payload),
            summary=str(payload.get("summary") or ""),
            description=str(payload.get("description") or ""),
            location=str(payload.get("location") or ""),
            start=str(payload.get("start") or ""),
            end=str(payload.get("end") or ""),
            all_day=bool(payload.get("all_day")),
            attendees=_require_attendee_list(payload.get("attendees")),
        )

    async def _create(self, payload: dict[str, Any]) -> ConnectorResult:
        event = self._event_from_payload(payload)
        created = await self._provider.create_event(
            calendar_id=self._calendar_id(payload), event=event
        )
        return ConnectorResult.success(
            f"created event {created.id}", data={"event": created.model_dump()}
        )

    async def _update(self, payload: dict[str, Any]) -> ConnectorResult:
        event = self._event_from_payload(payload)
        if not event.id:
            raise ValidationError("event_id is required to update an event")
        updated = await self._provider.update_event(
            calendar_id=self._calendar_id(payload), event=event
        )
        return ConnectorResult.success(
            f"updated event {updated.id}", data={"event": updated.model_dump()}
        )

    async def _delete(self, payload: dict[str, Any]) -> ConnectorResult:
        event_id = str(payload.get("event_id") or "")
        if not event_id:
            raise ValidationError("event_id is required to delete an event")
        deleted = await self._provider.delete_event(
            calendar_id=self._calendar_id(payload), event_id=event_id
        )
        return ConnectorResult.success(
            f"deleted event {event_id}" if deleted else f"event {event_id} was not deleted",
            data={"deleted": deleted, "event_id": event_id},
        )

    async def aclose(self) -> None:
        await self._provider.aclose()


__all__ = [
    "GOOGLE_CALENDAR_BASE_URL",
    "GOOGLE_TOKEN_URL",
    "CalendarConnector",
    "CalendarEvent",
    "CalendarListEntry",
    "CalendarProvider",
    "FreeBusySlot",
    "GoogleCalendarProvider",
    "normalized_event",
]
