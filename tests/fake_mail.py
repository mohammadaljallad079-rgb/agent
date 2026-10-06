"""Real SMTP/IMAP protocol services for connector tests.

The email connector is implemented over the real protocols, so its tests run
against real protocol servers rather than a stubbed library:

* :class:`SMTPSink` is a minimal SMTP server built on :mod:`asyncio` that speaks
  enough of the protocol (EHLO, STARTTLS, AUTH, MAIL/RCPT/DATA, QUIT) to receive
  a real ``smtplib`` send. ``starttls`` is accepted but not upgraded — the tests
  only assert the *message* reached the server.
* :class:`IMAPMailbox` is a minimal IMAP4 server that serves a fixed set of
  pre-built RFC822 messages for ``SELECT``/``SEARCH``/``FETCH``/``LOGOUT``.

Both are real servers on real sockets; the connector's stdlib client talks to
them unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class SMTPSink:
    """A minimal in-process SMTP server that records received messages."""

    messages: list[bytes] = field(default_factory=list)
    recipients: list[str] = field(default_factory=list)
    username: str = ""
    password: str = ""

    def __post_init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.port = 0

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def send(line: str) -> None:
            writer.write((line + "\r\n").encode())
            await writer.drain()

        await send("220 fake-smtp ready")
        data_mode = False
        while True:
            try:
                raw = await reader.readline()
            except (ConnectionError, asyncio.IncompleteReadError):
                break
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if data_mode:
                if line == ".":
                    data_mode = False
                    self.messages.append("\n".join(self._pending).encode("utf-8"))
                    await send("250 OK queued")
                else:
                    self._pending.append(line)
                continue
            upper = line.upper()
            if upper.startswith("EHLO") or upper.startswith("HELO"):
                await send("250-fake-smtp")
                await send("250-STARTTLS")
                await send("250 AUTH LOGIN PLAIN")
            elif upper.startswith("STARTTLS"):
                await send("220 Ready to start TLS")
            elif upper.startswith("AUTH"):
                await send("235 Authentication successful")
            elif upper.startswith("MAIL FROM"):
                await send("250 OK")
            elif upper.startswith("RCPT TO"):
                self.recipients.append(line)
                await send("250 OK")
            elif upper == "DATA":
                data_mode = True
                self._pending: list[str] = []
                await send("354 End data with <CR><LF>.<CR><LF>")
            elif upper == "QUIT":
                await send("221 Bye")
                break
            elif upper == "RSET":
                await send("250 OK")
            else:
                await send("250 OK")
        with contextlib.suppress(Exception):
            writer.close()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        async def _start() -> None:
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]
            self._ready.set()

        loop.run_until_complete(_start())
        loop.run_forever()

    def start(self) -> SMTPSink:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return self

    def stop(self) -> None:
        if self._server is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._server.close)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)


class IMAPMailbox:
    """A minimal IMAP4 server serving fixed messages."""

    def __init__(self, messages: list[bytes] | None = None) -> None:
        self._messages = list(messages or [])
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.port = 0
        self.selected = ""
        self.logged_in = False

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def send(line: str) -> None:
            writer.write((line + "\r\n").encode())
            await writer.drain()

        def split_command(raw: bytes) -> tuple[str, str]:
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            head, _, tail = text.partition(" ")
            return head.upper(), tail.strip()

        await send("* OK fake-imap ready")
        while True:
            try:
                raw = await reader.readline()
            except (ConnectionError, asyncio.IncompleteReadError):
                break
            if not raw:
                break
            tag, command = split_command(raw)
            upper = command.upper()
            if upper.startswith("CAPABILITY"):
                await send("* CAPABILITY IMAP4rev1")
                await send(f"{tag} OK CAPABILITY completed")
            elif upper.startswith("LOGIN"):
                self.logged_in = True
                await send(f"{tag} OK LOGIN completed")
            elif upper.startswith("SELECT") or upper.startswith("EXAMINE"):
                self.selected = command.split(" ", 1)[1].strip().strip('"')
                await send(f"* {len(self._messages)} EXISTS")
                await send(f"{tag} OK [READ-WRITE] SELECT completed")
            elif upper.startswith("SEARCH"):
                ids = " ".join(str(i + 1) for i in range(len(self._messages)))
                await send(f"* SEARCH {ids}".rstrip())
                await send(f"{tag} OK SEARCH completed")
            elif upper.startswith("FETCH"):
                parts = command.split()
                seq = parts[1] if len(parts) > 1 else "1"
                index = int(seq) - 1
                if 0 <= index < len(self._messages):
                    body = self._messages[index]
                    await send(f"* {seq} FETCH (RFC822 {{{len(body)}}}")
                    writer.write(body + b")\r\n")
                    await writer.drain()
                await send(f"{tag} OK FETCH completed")
            elif upper.startswith("LOGOUT"):
                await send("* BYE logging out")
                await send(f"{tag} OK LOGOUT completed")
                break
            else:
                await send(f"{tag} OK completed")
        with contextlib.suppress(Exception):
            writer.close()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        async def _start() -> None:
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]
            self._ready.set()

        loop.run_until_complete(_start())
        loop.run_forever()

    def start(self) -> IMAPMailbox:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return self

    def stop(self) -> None:
        if self._server is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._server.close)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)


def build_message(
    *,
    sender: str = "sender@example.com",
    to: str = "inbox@example.com",
    subject: str = "Hello",
    body: str = "Body text",
) -> bytes:
    return (
        f"From: {sender}\r\n"
        f"To: {to}\r\n"
        f"Subject: {subject}\r\n"
        "Message-ID: <msg-1@example.com>\r\n"
        "\r\n"
        f"{body}\r\n"
    ).encode()


@contextmanager
def smtp_sink(**kwargs: object) -> Iterator[SMTPSink]:
    sink = SMTPSink(**kwargs).start()  # type: ignore[arg-type]
    try:
        yield sink
    finally:
        sink.stop()


@contextmanager
def imap_mailbox(messages: list[bytes] | None = None) -> Iterator[IMAPMailbox]:
    box = IMAPMailbox(messages).start()
    try:
        yield box
    finally:
        box.stop()


__all__ = ["IMAPMailbox", "SMTPSink", "build_message", "imap_mailbox", "smtp_sink"]
