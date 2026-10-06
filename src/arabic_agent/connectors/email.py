"""Provider-neutral email connector.

Email is implemented over the real protocols, not a vendor SDK: SMTP to send and
IMAP to read/search/verify. That keeps it provider-neutral (any SMTP/IMAP host
works) and testable against real local protocol servers, and it means the
*architecture* is provider-neutral even though no hosted provider (Gmail,
Microsoft 365) is claimed as verified.

Two properties are enforced here rather than trusted to the model:

* **Sending is a consequential external action.** The connector exposes
  ``email.send`` as ``CONSEQUENTIAL_WRITE``; the permission/approval layer gates
  it, and the tool layer binds the approval to the exact message digest. The
  connector itself never decides to send — it sends only when called, and the
  caller has already cleared the approval gate.
* **Email content is untrusted data.** Received bodies are *data*: the connector
  scans them for prompt-injection indicators and returns them as a warning, and
  nothing in an email can grant authority. Attachments may only be read from
  authorized workspace paths, so an email cannot cause an arbitrary host file to
  be attached and exfiltrated.

Blocking stdlib ``smtplib``/``imaplib`` calls run in a worker thread via
``asyncio.to_thread`` so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import contextlib
import email as email_lib
import hashlib
import imaplib
import smtplib
import ssl
from email.message import EmailMessage as MimeMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..browser.injection import InjectionIndicator, scan_for_injection
from ..core.enums import ActionRisk, ConnectorKind, ConnectorState
from ..core.errors import (
    ConfigurationError,
    TransientError,
    ValidationError,
)
from ..local.sandbox import FilesystemPolicy, FilesystemSandbox
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

#: Cap on an attachment, so a huge file cannot be read into memory.
DEFAULT_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
#: Cap on messages returned by one search.
DEFAULT_MAX_RESULTS = 50


class EmailAddress(BaseModel):
    model_config = ConfigDict(frozen=True)

    address: str
    name: str = ""

    @classmethod
    def parse(cls, raw: str) -> EmailAddress:
        name, address = parseaddr(raw)
        return cls(address=address, name=name)


class EmailAttachment(BaseModel):
    """Safe metadata about an attachment — never its content."""

    model_config = ConfigDict(frozen=True)

    filename: str
    content_type: str = "application/octet-stream"
    size_bytes: int = 0
    sha256: str = ""


class EmailMessage(BaseModel):
    """A normalized email message (untrusted data)."""

    model_config = ConfigDict(frozen=True)

    message_id: str = ""
    subject: str = ""
    sender: EmailAddress | None = None
    to: tuple[EmailAddress, ...] = ()
    cc: tuple[EmailAddress, ...] = ()
    date: str = ""
    body_text: str = ""
    body_html: str = ""
    snippet: str = ""
    attachments: tuple[EmailAttachment, ...] = ()
    #: Prompt-injection indicators found in the body (a warning, not authority).
    injection: tuple[InjectionIndicator, ...] = ()

    def summary(self) -> str:
        who = self.sender.address if self.sender else "unknown"
        return f"email from {who}: {self.subject!r}"


class OutgoingEmail(BaseModel):
    """A composed message ready to send."""

    model_config = ConfigDict(frozen=True)

    to: tuple[str, ...]
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    #: Workspace-relative or absolute paths of files to attach.
    attachments: tuple[str, ...] = ()
    reply_to_message_id: str = ""
    in_reply_to: str = ""


class EmailPreview(BaseModel):
    """A normalized, secret-free preview for the approval prompt."""

    model_config = ConfigDict(frozen=True)

    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body_preview: str
    attachments: tuple[EmailAttachment, ...] = ()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class EmailConnector(Connector):
    """SMTP send + IMAP read/search, provider-neutral."""

    kind = ConnectorKind.EMAIL
    capabilities = (
        ConnectorCapability.EMAIL_READ,
        ConnectorCapability.EMAIL_SEARCH,
        ConnectorCapability.EMAIL_SEND,
        ConnectorCapability.EMAIL_ATTACH,
    )
    operations = (
        ConnectorOperation(name="email.search", kind=OperationKind.READ, risk=ActionRisk.READ_ONLY),
        ConnectorOperation(name="email.get", kind=OperationKind.READ, risk=ActionRisk.READ_ONLY),
        ConnectorOperation(
            name="email.send",
            kind=OperationKind.WRITE,
            risk=ActionRisk.CONSEQUENTIAL_WRITE,
        ),
        ConnectorOperation(
            name="email.reply",
            kind=OperationKind.WRITE,
            risk=ActionRisk.CONSEQUENTIAL_WRITE,
        ),
        ConnectorOperation(
            name="email.prepare", kind=OperationKind.READ, risk=ActionRisk.READ_ONLY
        ),
    )

    def __init__(
        self,
        *,
        name: str = "email",
        smtp_host: str = "",
        smtp_port: int = 587,
        smtp_username: str = "",
        smtp_password: CredentialRef | None = None,
        smtp_use_tls: bool = True,
        imap_host: str = "",
        imap_port: int = 993,
        imap_username: str = "",
        imap_password: CredentialRef | None = None,
        imap_mailbox: str = "INBOX",
        credentials: CredentialResolver | None = None,
        from_address: str = "",
        filesystem_policy: FilesystemPolicy | None = None,
        workspace_dir: str = "",
        max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
        max_results: int = DEFAULT_MAX_RESULTS,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.name = name
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_username = smtp_username
        self._smtp_password = smtp_password
        self._smtp_use_tls = smtp_use_tls
        self._imap_host = imap_host
        self._imap_port = imap_port
        self._imap_username = imap_username
        self._imap_password = imap_password
        self._imap_mailbox = imap_mailbox
        self._credentials = credentials or CredentialResolver()
        self._from_address = from_address or smtp_username
        self._max_attachment_bytes = max_attachment_bytes
        self._max_results = max_results
        self._timeout = timeout_seconds
        self._sandbox = (
            FilesystemSandbox(filesystem_policy) if filesystem_policy is not None else None
        )
        self._workspace_dir = workspace_dir
        super().__init__()

    # -- Health ---------------------------------------------------------
    def health(self) -> ConnectorHealth:
        if not self._smtp_host and not self._imap_host:
            return ConnectorHealth(
                state=ConnectorState.NOT_CONFIGURED,
                detail="no SMTP or IMAP host configured",
                read_only=not self.write_capable,
            )
        return ConnectorHealth(
            state=ConnectorState.READY,
            configured=True,
            authenticated=True,
            read_only=not self.write_capable,
            detail="configured",
        )

    async def healthcheck(self) -> ConnectorHealth:
        health = self.health()
        if health.state is not ConnectorState.READY:
            return health
        # Verify credentials are present without revealing them.
        for ref in (self._smtp_password, self._imap_password):
            if ref is not None and not await self._credentials.has(ref):
                return ConnectorHealth(
                    state=ConnectorState.REAUTHENTICATION_REQUIRED,
                    configured=True,
                    authenticated=False,
                    read_only=not self.write_capable,
                    detail=f"credential {ref.name!r} is not available",
                )
        return health

    # -- Attachment authorization --------------------------------------
    def _authorize_attachment(self, requested: str) -> Path:
        """Resolve an attachment path through the workspace sandbox.

        An email may only attach a file the sandbox authorizes; an attempt to
        attach ``/etc/passwd`` or ``.env`` is refused here, not by the model.
        """
        if self._sandbox is None:
            raise ConfigurationError(
                "email attachments require a filesystem sandbox to be configured"
            )
        return self._sandbox.resolve_for_read(requested)

    def _attachment_meta(self, requested: str) -> tuple[Path, EmailAttachment]:
        path = self._authorize_attachment(requested)
        size = path.stat().st_size
        if size > self._max_attachment_bytes:
            raise ValidationError(
                "attachment exceeds the size limit",
                details={"filename": path.name, "size": size, "limit": self._max_attachment_bytes},
            )
        return path, EmailAttachment(
            filename=path.name,
            content_type=_guess_content_type(path.name),
            size_bytes=size,
            sha256=_sha256(path),
        )

    # -- Execution ------------------------------------------------------
    async def call(self, call: ConnectorCall) -> ConnectorResult:
        payload = dict(call.payload)
        match call.operation:
            case "email.search":
                return await self.search(payload)
            case "email.get":
                return await self.get(payload)
            case "email.send" | "email.reply":
                return await self.send(OutgoingEmail(**payload))
            case "email.prepare":
                return await self.prepare(OutgoingEmail(**payload))
            case _:
                raise ValidationError(f"unsupported email operation: {call.operation!r}")

    async def prepare(self, message: OutgoingEmail) -> ConnectorResult:
        """Compose and return a normalized preview. No external effect."""
        attachments = tuple(self._attachment_meta(p)[1] for p in message.attachments)
        preview = EmailPreview(
            to=message.to,
            cc=message.cc,
            subject=message.subject,
            body_preview=message.body_text[:500],
            attachments=attachments,
        )
        return ConnectorResult.success(
            f"prepared email to {', '.join(message.to)}",
            data=preview.model_dump(),
        )

    async def send(self, message: OutgoingEmail) -> ConnectorResult:
        """Send a message over SMTP and return the provider message id."""
        if not self._smtp_host:
            raise ConfigurationError("email connector has no SMTP host configured")
        if not message.to:
            raise ValidationError("email requires at least one recipient")
        mime = self._build_mime(message)
        message_id = mime["Message-ID"]
        password = ""
        if self._smtp_password is not None:
            credential = await self._credentials.resolve(self._smtp_password)
            password = credential.reveal() if credential else ""

        def _send() -> str:
            context = ssl.create_default_context()
            if self._smtp_use_tls:
                client: smtplib.SMTP = smtplib.SMTP(
                    self._smtp_host, self._smtp_port, timeout=self._timeout
                )
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            else:
                client = smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=self._timeout)
                client.ehlo()
            try:
                if self._smtp_username:
                    client.login(self._smtp_username, password)
                client.send_message(mime)
            finally:
                try:
                    client.quit()
                except smtplib.SMTPException:
                    client.close()
            return str(message_id)

        try:
            provider_id = await asyncio.to_thread(_send)
        except (smtplib.SMTPException, OSError) as exc:
            raise TransientError(f"smtp send failed: {exc}") from exc
        return ConnectorResult.success(
            f"sent email to {', '.join(message.to)}",
            data={
                "provider_message_id": provider_id,
                "to": list(message.to),
                "subject": message.subject,
                "attachments": [Path(p).name for p in message.attachments],
            },
        )

    async def search(self, payload: dict[str, Any]) -> ConnectorResult:
        """Search the mailbox over IMAP and return normalized summaries."""
        if not self._imap_host:
            raise ConfigurationError("email connector has no IMAP host configured")
        criteria = str(payload.get("query") or "ALL")
        limit = max(1, min(int(payload.get("limit") or self._max_results), self._max_results))
        mailbox = str(payload.get("mailbox") or self._imap_mailbox)
        password = ""
        if self._imap_password is not None:
            credential = await self._credentials.resolve(self._imap_password)
            password = credential.reveal() if credential else ""

        def _search() -> list[EmailMessage]:
            import imaplib

            client = imaplib.IMAP4(self._imap_host, self._imap_port)
            try:
                if self._imap_username:
                    client.login(self._imap_username, password)
                client.select(mailbox)
                typ, data = client.search(None, criteria)
                if typ != "OK" or not data or not data[0]:
                    return []
                ids = data[0].split()[-limit:]
                messages: list[EmailMessage] = []
                for msg_id in ids:
                    typ, fetched = client.fetch(msg_id, "(RFC822)")
                    if typ != "OK" or not fetched:
                        continue
                    for part in fetched:
                        if isinstance(part, tuple) and len(part) >= 2:
                            messages.append(_parse_message(part[1]))
                return messages
            finally:
                with contextlib.suppress(imaplib.IMAP4.error):
                    client.logout()

        try:
            messages = await asyncio.to_thread(_search)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise TransientError(f"imap search failed: {exc}") from exc
        return ConnectorResult.success(
            f"found {len(messages)} message(s)",
            data={"messages": [m.model_dump() for m in messages], "count": len(messages)},
        )

    async def get(self, payload: dict[str, Any]) -> ConnectorResult:
        """Fetch one message by ``Message-ID`` via IMAP."""
        message_id = str(payload.get("message_id") or "")
        if not message_id:
            raise ValidationError("email.get requires a message_id")
        result = await self.search({"query": f'HEADER Message-ID "{message_id}"', "limit": 1})
        if not result.ok:
            return result
        messages = result.data.get("messages") or []
        if not messages:
            return ConnectorResult.failure(f"message {message_id} was not found")
        return ConnectorResult.success(
            f"fetched message {message_id}",
            data={"message": messages[0], "found": True},
        )

    def _build_mime(self, message: OutgoingEmail) -> MimeMessage:
        mime = MimeMessage()
        mime["From"] = self._from_address or self._smtp_username or "agent@localhost"
        mime["To"] = ", ".join(message.to)
        if message.cc:
            mime["Cc"] = ", ".join(message.cc)
        mime["Subject"] = message.subject
        mime["Date"] = formatdate(localtime=True)
        mime["Message-ID"] = make_msgid()
        if message.in_reply_to:
            mime["In-Reply-To"] = message.in_reply_to
            mime["References"] = message.in_reply_to
        if message.body_html:
            mime.set_content(message.body_text or "")
            mime.add_alternative(message.body_html, subtype="html")
        else:
            mime.set_content(message.body_text or "")
        for requested in message.attachments:
            path, meta = self._attachment_meta(requested)
            data = path.read_bytes()
            maintype, _, subtype = meta.content_type.partition("/")
            mime.add_attachment(
                data,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=meta.filename,
            )
        return mime

    async def aclose(self) -> None:
        return None


def _parse_message(raw: bytes) -> EmailMessage:
    parsed = email_lib.message_from_bytes(raw)
    body_text = ""
    body_html = ""
    attachments: list[EmailAttachment] = []
    if parsed.is_multipart():
        for part in parsed.walk():
            disposition = part.get("Content-Disposition", "") or ""
            content_type = part.get_content_type()
            if "attachment" in disposition or part.get_filename():
                payload = part.get_payload(decode=True)
                if not isinstance(payload, bytes):
                    payload = b""
                attachments.append(
                    EmailAttachment(
                        filename=part.get_filename() or "attachment",
                        content_type=content_type,
                        size_bytes=len(payload),
                        sha256=hashlib.sha256(payload).hexdigest(),
                    )
                )
            elif content_type == "text/plain":
                body_text += _decode_part(part)
            elif content_type == "text/html":
                body_html += _decode_part(part)
    else:
        body_text = _decode_part(parsed)
    text = body_text or body_html
    injection = scan_for_injection(text)
    return EmailMessage(
        message_id=str(parsed.get("Message-ID", "")),
        subject=str(parsed.get("Subject", "")),
        sender=EmailAddress.parse(str(parsed.get("From", ""))),
        to=tuple(EmailAddress.parse(a) for a in _addresses(parsed.get("To", ""))),
        cc=tuple(EmailAddress.parse(a) for a in _addresses(parsed.get("Cc", ""))),
        date=str(parsed.get("Date", "")),
        body_text=body_text,
        body_html=body_html,
        snippet=text[:200],
        attachments=tuple(attachments),
        injection=injection,
    )


def _decode_part(part: email_lib.message.Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _addresses(value: object) -> list[str]:
    raw = str(value or "")
    if not raw:
        return []
    return [a.strip() for a in raw.split(",") if a.strip()]


def _guess_content_type(filename: str) -> str:
    import mimetypes

    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


__all__ = [
    "EmailAddress",
    "EmailAttachment",
    "EmailConnector",
    "EmailMessage",
    "EmailPreview",
    "OutgoingEmail",
]
