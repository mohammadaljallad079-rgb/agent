"""A generic business / ERP / CRM connector.

Business systems (ERP, CRM, order management, ticketing, inventory) all reduce
to a small set of normalized concepts. This connector implements those concepts
once, in terms of an underlying transport (an :class:`HttpConnector`), so a
provider is added by *configuration* — a resource path and a field mapping — not
by new agent-core code.

Normalized operations:

* ``record.search``  — list/query records (READ_ONLY);
* ``record.get``     — fetch one record (READ_ONLY);
* ``record.create``  — create a record (CONSEQUENTIAL_WRITE);
* ``record.update``  — update a record's fields (LOW_RISK_WRITE by default; the
  operator may raise it to CONSEQUENTIAL_WRITE for sensitive resources);
* ``action.execute`` — invoke a named side-effecting action (HIGH_RISK_ACTION).

No provider-specific schema lives here. The caller names a ``resource`` (which
maps to a configured path) and supplies normalized fields; the connector maps
them onto the wire format and normalizes the response back.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from ..core.enums import ActionRisk, ConnectorKind
from ..core.errors import NotFoundError, ValidationError
from .base import (
    Connector,
    ConnectorCall,
    ConnectorCapability,
    ConnectorHealth,
    ConnectorOperation,
    ConnectorResult,
    OperationKind,
)
from .http import HttpConnector, HttpRequest


class ResourceMapping(BaseModel):
    """How one logical resource maps onto the transport."""

    model_config = ConfigDict(frozen=True)

    #: Logical name the agent uses (e.g. "orders").
    name: str
    #: URL path of the collection, e.g. "/orders".
    path: str
    #: URL path template for a single item, e.g. "/orders/{id}". Defaults to
    #: ``"{path}/{{id}}"`` when empty, so most providers need only ``path``.
    item_path: str = ""
    #: Field that uniquely identifies a record (used for get/update).
    id_field: str = "id"
    #: Whether updating this resource is consequential rather than low-risk.
    consequential_update: bool = False

    def item_template(self) -> str:
        """The item path template, derived from :attr:`path` when unset."""
        return self.item_path or f"{self.path.rstrip('/')}/{{id}}"


class BusinessConnector(Connector):
    """Normalized record/action operations over an HTTP business API."""

    kind = ConnectorKind.BUSINESS
    capabilities = (
        ConnectorCapability.RECORD_SEARCH,
        ConnectorCapability.RECORD_GET,
        ConnectorCapability.RECORD_CREATE,
        ConnectorCapability.RECORD_UPDATE,
        ConnectorCapability.ACTION_EXECUTE,
    )

    def __init__(
        self,
        *,
        name: str,
        transport: HttpConnector,
        resources: tuple[ResourceMapping, ...] = (),
        max_page_size: int = 50,
    ) -> None:
        self.name = name
        self._transport = transport
        self._resources = {r.name: r for r in resources}
        self._max_page_size = max_page_size
        self.operations = (
            ConnectorOperation(
                name="record.search", kind=OperationKind.READ, risk=ActionRisk.READ_ONLY
            ),
            ConnectorOperation(
                name="record.get", kind=OperationKind.READ, risk=ActionRisk.READ_ONLY
            ),
            ConnectorOperation(
                name="record.create",
                kind=OperationKind.WRITE,
                risk=ActionRisk.CONSEQUENTIAL_WRITE,
            ),
            ConnectorOperation(
                name="record.update",
                kind=OperationKind.WRITE,
                risk=ActionRisk.LOW_RISK_WRITE,
            ),
            ConnectorOperation(
                name="action.execute",
                kind=OperationKind.WRITE,
                risk=ActionRisk.HIGH_RISK_ACTION,
            ),
        )
        super().__init__()

    # -- Health ---------------------------------------------------------
    def health(self) -> ConnectorHealth:
        transport_health = self._transport.health()
        return ConnectorHealth(
            state=transport_health.state,
            configured=transport_health.configured,
            authenticated=transport_health.authenticated,
            read_only=False,
            detail=transport_health.detail,
        )

    async def healthcheck(self) -> ConnectorHealth:
        return await self._transport.healthcheck()

    def resource(self, name: str) -> ResourceMapping:
        mapping = self._resources.get(name)
        if mapping is None:
            raise NotFoundError(
                f"unknown resource: {name}", details={"known": sorted(self._resources)}
            )
        return mapping

    def risk_for(self, operation: str, resource: str | None) -> ActionRisk:
        """The declared risk of an operation, honouring a resource override."""
        if operation == "record.update" and resource:
            mapping = self._resources.get(resource)
            if mapping is not None and mapping.consequential_update:
                return ActionRisk.CONSEQUENTIAL_WRITE
        for op in self.operations:
            if op.name == operation:
                return op.risk
        return ActionRisk.HIGH_RISK_ACTION

    # -- Execution ------------------------------------------------------
    async def call(self, call: ConnectorCall) -> ConnectorResult:
        payload = dict(call.payload)
        match call.operation:
            case "record.search":
                return await self._search(payload)
            case "record.get":
                return await self._get(payload)
            case "record.create":
                return await self._create(payload)
            case "record.update":
                return await self._update(payload)
            case "action.execute":
                return await self._execute(payload)
            case _:
                raise ValidationError(f"unsupported business operation: {call.operation!r}")

    async def _search(self, payload: dict[str, Any]) -> ConnectorResult:
        resource = self.resource(str(payload.get("resource") or ""))
        filters = payload.get("filters") or {}
        if not isinstance(filters, dict):
            raise ValidationError("filters must be an object")
        limit = int(payload.get("limit") or self._max_page_size)
        limit = max(1, min(limit, self._max_page_size))
        params = {k: str(v) for k, v in filters.items()}
        params["limit"] = str(limit)
        if payload.get("offset") is not None:
            params["offset"] = str(payload["offset"])
        response = await self._transport.request(
            HttpRequest(method="GET", path=resource.path, params=params)
        )
        if not response.ok:
            return ConnectorResult.failure(
                f"search failed: {response.summary()}", data={"status": response.status}
            )
        records = _extract_records(response.body)
        return ConnectorResult.success(
            f"found {len(records)} record(s) in {resource.name}",
            data={"resource": resource.name, "records": records, "count": len(records)},
        )

    async def _get(self, payload: dict[str, Any]) -> ConnectorResult:
        resource = self.resource(str(payload.get("resource") or ""))
        record_id = payload.get("id")
        if record_id in (None, ""):
            raise ValidationError("record.get requires an id")
        subs = {"id": record_id, resource.id_field: record_id}
        path = resource.item_template().format(**subs)
        response = await self._transport.request(HttpRequest(method="GET", path=path))
        if response.status == 404:
            return ConnectorResult.failure(
                f"{resource.name} record {record_id} was not found",
                data={"status": 404, "resource": resource.name, "id": record_id},
            )
        if not response.ok:
            return ConnectorResult.failure(
                f"get failed: {response.summary()}", data={"status": response.status}
            )
        record = _extract_record(response.body)
        return ConnectorResult.success(
            f"fetched {resource.name} {record_id}",
            data={"resource": resource.name, "record": record},
        )

    async def _create(self, payload: dict[str, Any]) -> ConnectorResult:
        resource = self.resource(str(payload.get("resource") or ""))
        fields = payload.get("fields") or {}
        if not isinstance(fields, dict):
            raise ValidationError("fields must be an object")
        response = await self._transport.request(
            HttpRequest(method="POST", path=resource.path, json_body=fields)
        )
        if not response.ok:
            return ConnectorResult.failure(
                f"create failed: {response.summary()}",
                data={"status": response.status, "body": response.body},
            )
        record = _extract_record(response.body)
        return ConnectorResult.success(
            f"created {resource.name} record",
            data={"resource": resource.name, "record": record},
        )

    async def _update(self, payload: dict[str, Any]) -> ConnectorResult:
        resource = self.resource(str(payload.get("resource") or ""))
        record_id = payload.get("id")
        if record_id in (None, ""):
            raise ValidationError("record.update requires an id")
        fields = payload.get("fields") or {}
        if not isinstance(fields, dict) or not fields:
            raise ValidationError("record.update requires non-empty fields")
        subs = {"id": record_id, resource.id_field: record_id}
        path = resource.item_template().format(**subs)
        response = await self._transport.request(
            HttpRequest(method="PATCH", path=path, json_body=fields)
        )
        if not response.ok:
            return ConnectorResult.failure(
                f"update failed: {response.summary()}",
                data={"status": response.status, "body": response.body},
            )
        record = _extract_record(response.body)
        return ConnectorResult.success(
            f"updated {resource.name} {record_id}",
            data={"resource": resource.name, "id": record_id, "record": record},
        )

    async def _execute(self, payload: dict[str, Any]) -> ConnectorResult:
        action = str(payload.get("action") or "")
        if not action:
            raise ValidationError("action.execute requires an action name")
        path = str(payload.get("path") or f"/actions/{action}")
        body = payload.get("arguments") or {}
        if not isinstance(body, dict):
            raise ValidationError("arguments must be an object")
        response = await self._transport.request(
            HttpRequest(method="POST", path=path, json_body=body)
        )
        if not response.ok:
            return ConnectorResult.failure(
                f"action {action} failed: {response.summary()}",
                data={"status": response.status, "body": response.body},
            )
        return ConnectorResult.success(
            f"executed action {action}",
            data={"action": action, "result": _extract_record(response.body)},
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


def _extract_records(body: str) -> list[dict[str, Any]]:
    """Pull a list of records out of a JSON body, tolerating common shapes."""
    import json

    try:
        data = json.loads(body) if body else None
    except ValueError:
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("records", "items", "results", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        return [data]
    return []


def _extract_record(body: str) -> dict[str, Any]:
    import json

    try:
        data = json.loads(body) if body else {}
    except ValueError:
        return {}
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict):
            return inner
        return data
    return {}


class StaticBusinessBackend:
    """A minimal in-process business backend for unit tests.

    It is *not* used by the acceptance tests (which exercise the real HTTP
    connector against a real local HTTP service); it exists so the business
    connector's mapping/classification logic can be unit-tested without a
    socket, and it is clearly labelled as a test double.
    """

    def __init__(self, records: dict[str, dict[str, Any]] | None = None) -> None:
        self.records: dict[str, dict[str, Any]] = dict(records or {})


__all__ = ["BusinessConnector", "ResourceMapping", "StaticBusinessBackend"]
