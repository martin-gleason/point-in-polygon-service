"""F8-T4 — the installer's own ASGI app: a separate, local-only service the
operator drags a file into.

This is not `app.main` with more routes on it, and that is the single most
important fact in the module.

D22 — why this is a second application
    Production runs `uvicorn app.main:app` (Dockerfile) and the live instance is
    public. Every route reachable from `app.main` is therefore served to the
    internet. The routes here accept file uploads and will, with F8-T6, rewrite
    `config.toml` and write into `data/`; SPEC §3 defers authentication out of
    v1, so there is nothing on the public app that could stand in front of them.

    An environment-variable switch — "these routes only mount when
    `PIP_ADMIN=1`" — is not a boundary. It is a boundary-shaped thing that is
    off by default and one stray line in a deployment file away from being on,
    and nobody would ever find out it was on until someone else did. A guard you
    can turn off by accident is not a guard.

    So the installer is an application `app.main` never imports. Nothing in
    `app/main.py` mentions this module, and `tests/test_admin_isolation.py`
    proves it in a clean subprocess: importing `app.main` must not so much as
    load `app.admin`. The public deployment cannot serve what it does not
    import. That invariant is checked mechanically because it is exactly the
    kind of thing a later refactor breaks with the best of intentions.

D23 — why loopback alone is not the boundary either
    Binding 127.0.0.1 keeps the internet out. It does not keep out:

    * another account on a shared machine, which can reach any loopback port;
    * **DNS rebinding**. A page the operator has open in another tab is allowed
      to point its own hostname at 127.0.0.1 and issue requests to whatever is
      listening there. The browser attaches no warning to this; from the
      socket's point of view the request is local, because it is.

    Three things answer that, and all three are enforced on every request by
    `_guard`:

    1. **A token minted per run.** Not a password, not a stored secret: a fresh
       `secrets.token_urlsafe(32)` for each launch, compared with
       `secrets.compare_digest`. A rebinding page can reach the socket; it
       cannot know this run's token. `create_admin_app` refuses to be built
       without one, because a default token is a published token.

    2. **A Host allowlist.** Only the loopback names, with this run's port.
       `Host: evil.example.com` is refused even though the request arrived on
       the right socket — which is precisely the rebinding case, where the
       browser resolves the attacker's own name to 127.0.0.1 and keeps sending
       that name in the Host header.

    3. **The token travels in a request header, never in the URL.** A query
       string is the wrong place for a secret and this is worth spelling out,
       because it is the convenient choice: a URL-borne token is written into
       the browser's history, is handed to every external resource the page
       loads in the `Referer` header, and is the first thing any access log
       records. A custom header has a fourth virtue here — a cross-origin page
       cannot set one without a CORS preflight, and this app answers no
       preflight and installs no CORS middleware, so the rebinding tab is
       stopped a second time by the browser itself.

       The launcher therefore prints the page's address with the token in the
       URL *fragment* (`#token=…`), which browsers do not send to servers and do
       not put in `Referer`; F8-T5's page reads it out of `location.hash`,
       clears the hash, and sends it as `X-Admin-Token` on every call. The token
       is never logged and never echoed back in a response.

D24 — nothing is written until the operator says so
    Inspecting and previewing are read-only. Uploads land in a temporary
    workspace that is deleted, never in `data/`. `POST /api/commit/{id}` exists
    as a route so the shape of the app is settled, and answers "not yet
    implemented" — F8-T6 fills it in.

D25 — the page is served from here, and guarded like everything else
    F8-T5's page is four files under `static/admin/`, served by the routes at
    the bottom of `create_admin_app` and named one by one in
    `ADMIN_PAGE_FILES` rather than mounted as a directory. `_guard` is
    registered on the application, so the document is refused without this
    run's token exactly as `/api/inspect` is — a page a rebinding tab can read
    is a page that hands a stranger the shape of every request this tool takes.

    That leaves one thing open, and it is left open deliberately: a browser
    sends no custom header when it follows a link, and a URL fragment never
    reaches a server, so the *first* navigation cannot carry the token in
    either of the two places D23 allows. Delivering that first document is
    F8-T7's problem. Nothing in F8-T5 loosened the guard to make a demo work.

Sessions, and the leak this design exists to prevent
    Inspect and preview are two requests, so the `Candidate` — which owns an
    extraction workspace on disk — has to outlive the first one. `SessionStore`
    holds them by opaque id under a hard cap and a TTL, and *every* way out of
    that store (eviction, expiry, replacement, shutdown) goes through
    `Candidate.cleanup()`. F8-T2's review already caught this exact leak once at
    a smaller scale; a tool left open all afternoon must not fill $TMPDIR with
    unpacked shapefiles.

Why this module parses multipart itself
    `python-multipart`, which FastAPI's `UploadFile`/`Form` require, is not a
    dependency of this project and the golden rules do not allow one to be added
    for a local tool. `_read_multipart` is the answer, and it is not merely a
    substitute: it streams each part straight to disk against a running byte
    count, so the request is bounded *while* it arrives rather than after — which
    is what the upload caps have to do to be worth anything.

ArcGIS / ArcPy equivalent
    This is the open-source stand-in for the Catalog pane's "add data" plus
    ArcGIS Pro's Share > Publish dialog: choose a dataset, look at it drawn over
    what is already published, then publish. ArcGIS Server puts that behind a
    Portal identity and a server-side token; there is no Portal here, so the
    equivalent guarantee is bought with a loopback socket, a per-run token and a
    Host allowlist — and with the fact that the public service is a different
    process that has never heard of this one.
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Protocol, Sequence, TypeVar
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

# Starlette is FastAPI's own dependency — already installed, nothing added. It
# is what FastAPI itself uses to run a plain `def` endpoint off the event loop,
# so `/api/inspect` borrowing it is the same mechanism, not a second one.
from starlette.concurrency import run_in_threadpool

from app.admin.codes import Finding, _json_safe, has_blocking, sort_findings
from app.admin.inspect import MAX_ARCHIVE_BYTES, Candidate, CandidateError, read_candidate
from app.admin.preview import DrawableLayer, build_preview, load_installed_layers
from app.admin.validate import InstalledLayer, validate_candidate

# The installed layers have to be described to the validator the way PIP-L009
# and PIP-L016 see them — a name and a box in degrees — and there is exactly one
# correct way to work that box out. Importing it is what keeps this module from
# growing a second, subtly different one. `app.admin.preview` imports from the
# same place and for the same reason.
from app.admin.validate import _bounds_in_degrees, _declared_crs, _drawn_shapes
from app.admin.validate import _geometry_series
from app.config import AppConfig, load_config
from app.errors import error_response

# --------------------------------------------------------------------------
# the boundary (D23)
# --------------------------------------------------------------------------

# Where the token travels. A header, not a query parameter — see the module
# docstring; the short version is that a URL leaks through `Referer`, through
# browser history and through every access log there is.
ADMIN_TOKEN_HEADER = "X-Admin-Token"

# The same name as ASGI hands it to us: lowercased, as bytes. `_supplied_token`
# matches on this, because the wire carries bytes and the decoded `str` view of
# a header is a lossy place to do a security comparison.
_ADMIN_TOKEN_HEADER_RAW = ADMIN_TOKEN_HEADER.lower().encode("ascii")

# The traceback an operator must never be shown goes here instead — the window
# they started the tool from, where the maintainer can read it.
_LOG = logging.getLogger("app.admin.installer")

# How many bytes of randomness a run's token carries. 32 bytes is 256 bits,
# which is not a number anything guesses in the lifetime of a browser tab.
TOKEN_BYTES = 32

# The only names this app answers to, before the port is appended. Everything
# else — including a name that resolves to 127.0.0.1, which is the whole trick
# of DNS rebinding — is refused.
LOOPBACK_HOST_NAMES = ("localhost", "127.0.0.1", "[::1]")

# --------------------------------------------------------------------------
# upload caps — what a request may cost, decided before it costs it
# --------------------------------------------------------------------------

# The same number `app.admin.inspect` bounds an archive with, deliberately. A
# tighter cap here would refuse files the reader itself would have accepted, and
# would refuse them with a message about HTTP rather than a message about the
# file — the reader is the component that decides what a layer may be, and this
# one only has to stop a request that is not trying to be a layer at all.
MAX_UPLOAD_BYTES = MAX_ARCHIVE_BYTES

# A shapefile set is five files; with a .cpg, a .shp.xml and the odd sidecar an
# honest drag-and-drop reaches perhaps eight. Sixteen is generous, and it stops
# a request that is a thousand parts long from being parsed at all.
MAX_UPLOAD_PARTS = 16

# One part's headers. A `Content-Disposition` line is a couple of hundred bytes;
# 8 KiB is room for anything real and a bound on a part whose headers never end.
MAX_PART_HEADER_BYTES = 8 * 1024

# A non-file form field (a URL, a layer id, a column list) held in memory. The
# longest honest value here is an ArcGIS REST address.
MAX_FIELD_BYTES = 64 * 1024

# The longest filename kept. Every mainstream filesystem stops at 255 bytes for
# one path component, so a longer name is not a name.
MAX_FILENAME_CHARS = 255

READ_CHUNK_BYTES = 64 * 1024

# --------------------------------------------------------------------------
# the page (F8-T5)
# --------------------------------------------------------------------------

# `app/admin/main.py` -> `app/admin` -> `app` -> the repo root.
ADMIN_PAGE_DIR = Path(__file__).resolve().parents[2] / "static" / "admin"

# The whole page, named file by file rather than served out of a directory.
#
# `StaticFiles` would be one line, and one line is how a directory grows a
# `.env`, an editor backup or a stray export and serves it. This tool's audience
# drops files into folders for a living. An explicit table can only ever serve
# these four, and the media types are stated rather than guessed — a module
# script served as `text/plain` is refused by the browser outright, which is a
# blank page with an error only the console shows.
ADMIN_PAGE_FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
    "/preview_model.js": ("preview_model.js", "text/javascript; charset=utf-8"),
    "/admin.css": ("admin.css", "text/css; charset=utf-8"),
}


class _HasLayerId(Protocol):
    """Anything with a layer's short name on it — `InstalledLayer` from the
    validator, `DrawableLayer` from the preview. `_split_replaced` needs no more
    than this, and asking for no more is what lets one function serve both."""

    id: str


_L = TypeVar("_L", bound=_HasLayerId)

# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

# How many inspected candidates are held at once. Each owns a temporary
# workspace holding a full copy of somebody's data, and an operator installs one
# layer at a time — a handful covers "I inspected the wrong file, let me try the
# other one" without ever letting a stack of extracted shapefiles build up.
MAX_SESSIONS = 8

# How long an inspected candidate is kept without being asked for. Long enough
# to read every finding on the page and think about it; short enough that a tab
# left open over lunch is not still holding an unpacked county on disk.
SESSION_TTL_SECONDS = 60 * 60


@dataclass
class CandidateSession:
    """One inspected candidate, alive between the inspect call and the preview.

    `release()` is the only way anything leaves the store, and it is idempotent:
    the workspaces are deleted exactly once however many paths reach it.
    """

    id: str
    candidate: Candidate
    findings: tuple[Finding, ...]
    layer_id: str
    display_name: str
    highlight: tuple[int, ...]
    created_at: float
    released: bool = False

    def release(self) -> None:
        """Delete everything this session put on disk. Never raises."""
        if self.released:
            return
        self.released = True
        self.candidate.cleanup()


class SessionStore:
    """Inspected candidates by opaque id, under a cap and a TTL.

    Every exit is an eviction and every eviction calls `release()`. That is the
    whole design: there is no path by which a `Candidate` leaves this store with
    its workspace still on disk, including the process shutting down.

    Guarded by a lock because uvicorn runs FastAPI's synchronous endpoints on a
    thread pool, so two requests really can be in here at once.
    """

    def __init__(
        self,
        *,
        max_sessions: int = MAX_SESSIONS,
        ttl_seconds: float = SESSION_TTL_SECONDS,
    ) -> None:
        self.max_sessions = max(1, int(max_sessions))
        self.ttl_seconds = float(ttl_seconds)
        self._sessions: dict[str, CandidateSession] = {}
        self._lock = threading.Lock()

    def add(
        self,
        candidate: Candidate,
        *,
        findings: Sequence[Finding],
        layer_id: str,
        display_name: str,
        highlight: Sequence[int],
    ) -> CandidateSession:
        session = CandidateSession(
            id=secrets.token_urlsafe(16),
            candidate=candidate,
            findings=tuple(findings),
            layer_id=layer_id,
            display_name=display_name,
            highlight=tuple(highlight),
            created_at=time.monotonic(),
        )
        with self._lock:
            evicted = self._expired_locked()
            # Oldest first, until there is room for this one. `>=` because the
            # new session is about to be added.
            while len(self._sessions) >= self.max_sessions:
                oldest = min(
                    self._sessions.values(), key=lambda held: held.created_at
                )
                evicted.append(self._sessions.pop(oldest.id))
            self._sessions[session.id] = session
        # Outside the lock: deleting a directory tree is I/O, and no other
        # request needs to wait behind it.
        for stale in evicted:
            stale.release()
        return session

    def get(self, session_id: str) -> CandidateSession | None:
        with self._lock:
            expired = self._expired_locked()
            session = self._sessions.get(session_id)
        for stale in expired:
            stale.release()
        return session

    def release_all(self) -> None:
        """Empty the store, releasing every workspace. Called on shutdown."""
        with self._lock:
            held = list(self._sessions.values())
            self._sessions.clear()
        for session in held:
            session.release()

    def _expired_locked(self) -> list[CandidateSession]:
        """Remove everything past its TTL. The lock is already held."""
        cutoff = time.monotonic() - self.ttl_seconds
        stale = [
            session
            for session in self._sessions.values()
            if session.created_at < cutoff
        ]
        for session in stale:
            self._sessions.pop(session.id, None)
        return stale


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


class UploadRejected(Exception):
    """A request refused on its own shape, before any layer was read.

    Distinct from `CandidateError`, which is a refusal of the *file* and carries
    a `Finding` written for the operator. This one is about the request: too
    big, too many parts, not multipart at all. There is no registry code for
    "your HTTP request was malformed" and inventing one would put a plumbing
    failure in the same list as "this file has no coordinate system".
    """

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _error(
    status_code: int, code: str, message: str, *, finding: Finding | None = None
) -> JSONResponse:
    """The project's `{"error": {"code", "message"}}` envelope, plus the finding
    when there is one.

    `app.errors.error_response` is the one place that envelope is written, so it
    is the one place this builds on. A finding is attached *inside* the error
    object rather than beside it, so a caller that only knows the SPEC §4 shape
    reads exactly what it expects and the extra key is additive.
    """
    response = error_response(status_code, code, message)
    if finding is None:
        return response
    body = json.loads(response.body.decode("utf-8"))
    body["error"]["finding"] = finding.to_dict()
    return JSONResponse(status_code=status_code, content=body)


# --------------------------------------------------------------------------
# multipart, parsed here and streamed to disk
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadedPart:
    """One file out of a multipart body, already written to its own folder."""

    field_name: str
    filename: str
    path: Path


_BOUNDARY_PATTERN = re.compile(r'boundary="?([^";]+)"?', re.IGNORECASE)
_DISPOSITION_NAME = re.compile(r'\bname="((?:[^"\\]|\\.)*)"', re.IGNORECASE)
_DISPOSITION_FILENAME = re.compile(r'\bfilename="((?:[^"\\]|\\.)*)"', re.IGNORECASE)


def safe_upload_filename(raw: str, *, fallback: str) -> str:
    """The operator's filename reduced to a bare, harmless name.

    A multipart filename is written by whoever sent the request, which on a page
    reached by DNS rebinding is not the operator. `../../etc/passwd`,
    `C:\\windows\\system32\\x`, a name with a NUL in it and a name 4,000
    characters long are all things this has to survive, and the survival cannot
    depend on the caller remembering to sanitise: this returns a single path
    component or the fallback, and nothing else.

    Belt and braces, deliberately. `_read_multipart` also gives every part a
    folder of its own and joins only this name onto it, so even a name that
    escaped this function could reach nothing but its own directory. Two
    independent reasons the write lands where it should is the right number for
    a name a stranger chose.
    """
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    if len(name) > 1 and name[1] == ":":
        # `C:evil` on Windows is "relative to the current directory of drive C",
        # which is not this folder.
        name = name[2:]
    name = "".join(
        character
        for character in name
        if character.isprintable() and character not in "\x00/\\"
    ).strip()
    # A name that is only dots addresses a directory, not a file.
    if set(name) <= {"."}:
        name = ""
    name = name[:MAX_FILENAME_CHARS]
    return name or fallback


def _boundary_of(content_type: str) -> bytes:
    match = _BOUNDARY_PATTERN.search(content_type or "")
    if not match:
        raise UploadRejected(
            "invalid_request",
            "this endpoint takes a multipart/form-data body with a boundary",
        )
    return match.group(1).encode("latin-1", "replace")


async def _read_urlencoded(request: Request) -> dict[str, str]:
    """A form with no files in it, bounded at a field's worth of bytes.

    Inspecting a published map service sends an address and nothing else, and a
    request that carries no file has no business being multipart. This is the
    whole of the second body shape this endpoint accepts: `MAX_FIELD_BYTES` is
    already the most any single field may be, so it is the right ceiling for a
    body that is only fields.
    """
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_FIELD_BYTES:
            raise UploadRejected(
                "invalid_request", "that form is far longer than any address"
            )
    return {
        name: value
        for name, value in parse_qsl(
            bytes(body).decode("utf-8", "replace"), keep_blank_values=True
        )
    }


def _unquote(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


async def _read_multipart(
    request: Request, *, into: Path
) -> tuple[list[UploadedPart], dict[str, str]]:
    """Split a multipart body into files on disk and plain fields in memory.

    Streaming, and bounded while it streams. Every byte that arrives is counted
    against `MAX_UPLOAD_BYTES` as it arrives, so an upload that is too large is
    stopped partway through rather than after it has all been buffered — the
    point of a size cap being to not pay the cost, not to notice it afterwards.
    A `Content-Length` that already exceeds the cap is refused by `_guard`
    before this is entered at all; this is what happens when there is no such
    header, or when it lied.

    Each file goes into a numbered folder of its own under `into`, named with
    `safe_upload_filename`. The folder is why the reader still sees the
    operator's own filename — `app.admin.inspect` dispatches on the name the
    file came in as, and every message it writes says that name — while a
    collision between two parts called the same thing remains impossible.
    """
    boundary = _boundary_of(request.headers.get("content-type", ""))
    delimiter = b"--" + boundary
    terminator = b"\r\n" + delimiter

    files: list[UploadedPart] = []
    fields: dict[str, str] = {}

    buffer = bytearray()
    total = 0
    stream = request.stream().__aiter__()

    async def fill() -> bool:
        """Pull one more chunk into the buffer. False at the end of the body."""
        nonlocal total
        while True:
            try:
                chunk = await stream.__anext__()
            except StopAsyncIteration:
                return False
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise UploadRejected(
                    "upload_too_large",
                    f"this tool accepts uploads up to "
                    f"{MAX_UPLOAD_BYTES // (1024 * 1024):,} MB",
                    status_code=413,
                )
            buffer.extend(chunk)
            return True

    def malformed() -> UploadRejected:
        return UploadRejected(
            "invalid_request", "the upload was cut short or is not well formed"
        )

    # Skip whatever sits before the first delimiter.
    while True:
        at = buffer.find(delimiter)
        if at >= 0:
            del buffer[: at + len(delimiter)]
            break
        if len(buffer) > MAX_PART_HEADER_BYTES:
            raise malformed()
        if not await fill():
            raise malformed()

    while True:
        while len(buffer) < 2:
            if not await fill():
                raise malformed()
        if buffer[:2] == b"--":  # the closing delimiter: no more parts
            return files, fields
        if buffer[:2] != b"\r\n":
            raise malformed()
        del buffer[:2]

        while True:
            at = buffer.find(b"\r\n\r\n")
            if at >= 0:
                break
            if len(buffer) > MAX_PART_HEADER_BYTES:
                raise UploadRejected(
                    "invalid_request", "one part of the upload has oversized headers"
                )
            if not await fill():
                raise malformed()
        headers = bytes(buffer[:at]).decode("utf-8", "replace")
        del buffer[: at + 4]

        disposition = next(
            (
                line
                for line in headers.split("\r\n")
                if line.lower().startswith("content-disposition:")
            ),
            "",
        )
        name_match = _DISPOSITION_NAME.search(disposition)
        filename_match = _DISPOSITION_FILENAME.search(disposition)
        field_name = _unquote(name_match.group(1)) if name_match else ""

        if filename_match is not None:
            if len(files) >= MAX_UPLOAD_PARTS:
                raise UploadRejected(
                    "too_many_files",
                    f"this tool accepts at most {MAX_UPLOAD_PARTS} files in one "
                    f"upload; a shapefile set is five",
                )
            filename = safe_upload_filename(
                _unquote(filename_match.group(1)), fallback=f"upload-{len(files)}"
            )
            folder = into / str(len(files))
            folder.mkdir(parents=True, exist_ok=False)
            destination = folder / filename
            sink = open(destination, "xb")
            part = UploadedPart(
                field_name=field_name, filename=filename, path=destination
            )
        else:
            filename = ""
            destination = None
            sink = None
            part = None
            collected = bytearray()

        try:
            while True:
                at = buffer.find(terminator)
                if at >= 0:
                    piece = bytes(buffer[:at])
                    del buffer[: at + len(terminator)]
                    if sink is not None:
                        sink.write(piece)
                    else:
                        collected.extend(piece)
                    break
                # A partial terminator can only be the last len(terminator) - 1
                # bytes, so everything before that is safe to hand on.
                keep = len(terminator)
                if len(buffer) > keep:
                    piece = bytes(buffer[: len(buffer) - keep])
                    del buffer[: len(buffer) - keep]
                    if sink is not None:
                        sink.write(piece)
                    else:
                        collected.extend(piece)
                if sink is None and len(collected) > MAX_FIELD_BYTES:
                    raise UploadRejected(
                        "invalid_request",
                        "one of the form fields in the upload is far too long",
                    )
                if not await fill():
                    raise malformed()
        finally:
            if sink is not None:
                sink.close()

        if part is not None:
            files.append(part)
        elif field_name:
            fields[field_name] = collected.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# the app
# --------------------------------------------------------------------------


def mint_token() -> str:
    """A fresh token for one run of the installer.

    Minted, never configured. A token that lives in a file or an environment
    variable is a token that outlives the session it was for, gets committed, or
    gets shared; this one exists for as long as the process does.
    """
    return secrets.token_urlsafe(TOKEN_BYTES)


def _supplied_token(request: Request) -> bytes:
    """The token header exactly as it arrived, as bytes — never as `str`.

    A header is bytes on the wire. Starlette decodes it as latin-1 so that the
    decode itself can never fail, which means any byte from 0x80 up becomes a
    non-ASCII `str` — and `secrets.compare_digest` raises `TypeError` when asked
    to compare two `str`s that are not both ASCII. That `TypeError` would be
    raised *inside the guard and outside its `except`*, so a single stray byte
    in the header would turn the one pre-authentication code path into a 500
    with a traceback instead of a refusal. Comparing bytes makes the comparison
    total: every one of the 256 values a client can put in this header has an
    answer, and the answer is "no".

    The header name is matched against the raw list rather than the decoded
    mapping for the same reason — no decode step, no decode-shaped surprise.

    ArcGIS / ArcPy equivalent
        None. ArcGIS Server delegates authentication to the web adaptor / IIS,
        so a token comparison like this has no ArcPy analogue; it is the part of
        the stack a licensed deployment buys rather than writes.
    """
    for name, value in request.headers.raw:
        if name.lower() == _ADMIN_TOKEN_HEADER_RAW:
            return value
    return b""


def _declared_content_length(request: Request) -> int | None:
    """`Content-Length` as a number, or `None` when it is not one.

    `str.isdigit()` is true for characters `int()` refuses — `"²"` is a
    digit to Python and a `ValueError` to `int()`, and it is one latin-1 byte,
    so a client can send it. `str.isascii()` is what makes the two agree, and
    keeping the parse in one place keeps them from drifting apart again.
    """
    declared = request.headers.get("content-length")
    if declared is None or not (declared.isascii() and declared.isdigit()):
        return None
    return int(declared)


def allowed_hosts(port: int) -> frozenset[str]:
    """The exact `Host` values this run answers to.

    The port is part of every entry because it is part of the header a browser
    sends, and a bare name would let a rebinding page reach a run on port 80 —
    the one port a `Host` header may legally omit — by simply not naming one.
    A run on port 80 accepts both forms, because for that port both are the same
    address; every other run accepts only the explicit form.
    """
    names = {f"{name}:{port}" for name in LOOPBACK_HOST_NAMES}
    if port == 80:
        names.update(LOOPBACK_HOST_NAMES)
    return frozenset(names)


def _installed_for_validation(
    layers: Sequence[DrawableLayer],
) -> tuple[InstalledLayer, ...]:
    """The installed layers as PIP-L009 and PIP-L016 need to see them.

    Computed once, at startup, for the same reason `app.main` builds its lookup
    once: reading and projecting every serving layer on every request is work an
    operator waits through, and none of it changes while the tool is open.
    """
    described: list[InstalledLayer] = []
    for layer in layers:
        drawn, _rows = _drawn_shapes(_geometry_series(layer.frame))
        bounds = (
            _bounds_in_degrees(drawn, _declared_crs(layer.frame))
            if drawn is not None and len(drawn)
            else None
        )
        described.append(InstalledLayer(id=layer.id, name=layer.name, bounds=bounds))
    return tuple(described)


def _split_replaced(layers: Sequence[_L], layer_id: str) -> tuple[_L | None, tuple[_L, ...]]:
    """The installed layer this candidate replaces, and all the others.

    One rule, one place. Reinstalling `police_districts` means two things at
    once, and they used to be worked out independently:

    * the preview must draw the candidate over the *other* layers, not over the
      copy it supersedes, which would otherwise report a perfect overlap with
      itself;
    * the validator must be told the same thing, or PIP-L009 fires on the
      candidate's own name and `blocking` comes back true — and `blocking` is
      the single boolean F8-T5 puts the Install button behind, so the operator
      gets a preview built for a replacement and a button that can never be
      pressed. Updating a layer was impossible.

    The exclusion existed only on the preview side. Splitting here, and handing
    both halves to whoever needs them, is what stops the two from drifting
    apart again: there is no way to take `others` without also being handed the
    layer that was removed from it.

    Deliberately typed on `.id` alone, so the same call serves the validator's
    `InstalledLayer` and the preview's `DrawableLayer`. Two functions differing
    only in an annotation is exactly how the halves came apart the first time.

    ArcGIS / ArcPy equivalent
        The "overwrite the existing service" path of ArcGIS Pro's Share >
        Publish: the target is pulled out of the list of things the new layer is
        checked against, and named separately in the confirmation instead.
    """
    replaced = next((layer for layer in layers if layer.id == layer_id), None)
    others = tuple(layer for layer in layers if layer.id != layer_id)
    return replaced, others


def merge_findings(
    reader_findings: Sequence[Finding], validator_findings: Sequence[Finding]
) -> list[Finding]:
    """One list out of the reader's observations and the validator's checks.

    The two overlap on purpose — PIP-L017 fires from both — and
    `app.admin.inspect`'s docstring settles which copy wins: the reader's, which
    knows the .dbf's write date and whether a .shp.xml came with the file, where
    the validator only knows that no vintage arrived. So a code the reader
    produced suppresses the validator's copy of that code entirely, and what is
    left is sorted the one way `sort_findings` sorts, so the page renders the
    same list on every refresh.
    """
    reader_codes = {found.code for found in reader_findings}
    merged = list(reader_findings) + [
        found for found in validator_findings if found.code not in reader_codes
    ]
    seen: set[tuple[str, str]] = set()
    unique: list[Finding] = []
    for found in sort_findings(merged):
        key = (found.code, found.specifics)
        if key in seen:
            continue
        seen.add(key)
        unique.append(found)
    return unique


def _highlight_positions(findings: Iterable[Finding]) -> tuple[int, ...]:
    """The rows the preview map marks — PIP-L008's `broken_positions`."""
    positions: set[int] = set()
    for found in findings:
        if found.code != "PIP-L008":
            continue
        for position in found.detail.get("broken_positions", ()) or ():
            with contextlib.suppress(TypeError, ValueError):
                positions.add(int(position))
    return tuple(sorted(positions))


def create_admin_app(
    config: AppConfig | None = None,
    *,
    token: str,
    port: int,
    max_sessions: int = MAX_SESSIONS,
    session_ttl_seconds: float = SESSION_TTL_SECONDS,
) -> FastAPI:
    """Build the installer app.

    `token` and `port` are required and there is no default for either. A
    default token is a published token, and a Host allowlist that does not know
    its own port cannot tell a rebinding request from a real one — so both are
    the caller's to supply, and passing an empty token raises rather than
    silently building an unguarded app.

    `app.main` does not call this, must not call this, and does not import this
    module. See D22 in the module docstring and `tests/test_admin_isolation.py`.
    """
    if not token:
        raise ValueError(
            "the installer refuses to start without a token; call mint_token()"
        )

    sessions = SessionStore(
        max_sessions=max_sessions, ttl_seconds=session_ttl_seconds
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        # Whatever the operator left open. Every workspace goes with the
        # process; none of them outlive it in $TMPDIR.
        sessions.release_all()

    app = FastAPI(
        title="Point-in-Polygon Layer Installer",
        description=(
            "Local-only tool for installing a polygon layer. Not part of the "
            "public service and never served by it."
        ),
        lifespan=lifespan,
    )

    app_config = config if config is not None else load_config()
    app.state.config = app_config
    app.state.sessions = sessions
    app.state.allowed_hosts = allowed_hosts(port)
    app.state.installed = load_installed_layers(app_config)
    app.state.installed_context = _installed_for_validation(app.state.installed)
    # Kept off `app.state` under any name a template or a debug page might
    # render. The comparison closure below is the only thing that holds it.
    _expected_token = token.encode("utf-8")

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        """Host, then token, then size — and no traceback, ever.

        Order matters. The Host check is first because a request that lied about
        where it was going is refused before anything else looks at it, and
        because it is the check that costs nothing. The token is next, so that
        an unauthenticated request never reaches a body parser. The declared
        size is last of the three, because it is the only one a legitimate
        operator can trip.

        Every one of the three is *total* over what a client can send, which is
        the property that matters more than the order: they run before the
        `try`, so a check that can raise on a hostile header is a traceback on
        the one path that has not authenticated anything yet. The Host check is
        a set membership, which cannot raise. The token is compared as bytes
        (`_supplied_token`), not as a latin-1-decoded `str` that
        `secrets.compare_digest` would reject with a `TypeError`. The declared
        size is parsed by `_declared_content_length`, which does not hand
        `int()` a character `str.isdigit()` calls a digit and `int()` does not.

        The `except` is not defensive dressing. This tool's audience cannot act
        on a stack trace, a stack trace names paths on the operator's disk, and
        the project already decided this question elsewhere — `scripts/` exit 2
        with a sentence rather than printing a traceback. So an unexpected
        failure comes out as one clean sentence with the same envelope as every
        other error, and the traceback goes to the process's own log where the
        maintainer can read it.
        """
        host = request.headers.get("host", "")
        if host not in request.app.state.allowed_hosts:
            # Deliberately terse, and deliberately not naming what would have
            # been accepted. This is the DNS-rebinding refusal; the page that
            # triggered it is not the operator's.
            return _error(
                400,
                "bad_host",
                "this tool only answers requests addressed to it on this "
                "computer",
            )

        supplied = _supplied_token(request)
        if not supplied or not secrets.compare_digest(supplied, _expected_token):
            return _error(
                403,
                "forbidden",
                "this request did not carry the key this run of the installer "
                "printed when it started",
            )

        declared = _declared_content_length(request)
        if declared is not None and declared > MAX_UPLOAD_BYTES:
            return _error(
                413,
                "upload_too_large",
                f"this tool accepts uploads up to "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024):,} MB",
            )

        try:
            return await call_next(request)
        except Exception:  # noqa: BLE001 - the whole point; see the docstring
            _LOG.exception("the installer failed while handling %s", request.url.path)
            return _error(
                500,
                "internal_error",
                "something inside this tool went wrong while handling that "
                "request. Nothing was installed. The details are in the window "
                "you started the tool from.",
            )

    @app.post("/api/inspect")
    async def inspect_candidate(request: Request) -> Any:
        """Read what the operator sent, check it, and hold it for the preview.

        Read-only (D24): uploads go to a temporary folder that is deleted before
        this returns, and nothing is written to `data/` or to `config.toml`.

        The response is the facts, every finding from both the reader and the
        validator, and whether anything among them blocks installation — which
        is the one boolean F8-T5's page puts its Install button behind.

        Why this is `async` and yet does no blocking work itself
            It has to be `async`: `_read_multipart` consumes `request.stream()`,
            which is how the body is bounded *while* it arrives instead of after
            (see the module docstring), and only a coroutine can await a stream.

            But `read_candidate` and `validate_candidate` are ordinary
            synchronous functions doing seconds of work — geopandas opening a
            file, and for a web address a synchronous HTTP fetch with a
            multi-second timeout. Called directly from here they would run *on
            the event loop*, and an event loop that is running a function is not
            answering anybody: one inspect of a slow published service froze the
            whole tool, measured at a 404 on another route taking 11 seconds
            instead of 0.05. So they go through `run_in_threadpool`, which is
            what FastAPI does for a plain `def` endpoint anyway — `preview` and
            `commit` below are `def` for exactly this reason and were never
            affected. This endpoint cannot be `def`, so it does by hand what
            being `def` would have done for it.
        """
        upload_root = Path(tempfile.mkdtemp(prefix="pip-upload-"))
        candidate: Candidate | None = None
        try:
            content_type = request.headers.get("content-type", "")
            if content_type.lower().startswith("multipart/form-data"):
                files, fields = await _read_multipart(request, into=upload_root)
            elif content_type.lower().startswith(
                "application/x-www-form-urlencoded"
            ):
                files, fields = [], await _read_urlencoded(request)
            else:
                raise UploadRejected(
                    "invalid_request",
                    "send the file as a form upload, or the web address of a "
                    "published map service as a form field",
                )

            url = (fields.get("url") or "").strip()
            select = (fields.get("select") or "").strip() or None
            layer_id = (fields.get("layer_id") or "candidate").strip() or "candidate"
            display_name = (fields.get("display_name") or "").strip() or layer_id
            attributes = tuple(
                column.strip()
                for column in (fields.get("attributes") or "").split(",")
                if column.strip()
            )

            if files and url:
                raise UploadRejected(
                    "invalid_request",
                    "send either files or a web address, not both",
                )
            if not files and not url:
                raise UploadRejected(
                    "invalid_request",
                    "send a file to inspect, or the web address of a published "
                    "map service",
                )

            if url:
                source: Any = url
                source_files: list[str] | None = None
            else:
                source = [part.path for part in files]
                source_files = [part.filename for part in files]

            try:
                candidate = await run_in_threadpool(
                    read_candidate, source, source_files=source_files, select=select
                )
            except CandidateError as refused:
                # The reader's refusal is the operator's answer: a finding
                # written for them, in the same envelope as everything else.
                # There is no frame behind it, so there is nothing to preview
                # and no session to make.
                return _error(
                    400,
                    "layer_not_readable",
                    refused.finding.message,
                    finding=refused.finding,
                )

            # The same split the preview makes, made once, here — see
            # `_split_replaced`. Reinstalling a layer is compared against the
            # other layers, and the fact that it *is* a replacement is carried
            # separately so PIP-L020 can say so.
            replacing, other_installed = _split_replaced(
                request.app.state.installed_context, layer_id
            )
            findings = merge_findings(
                candidate.findings,
                await run_in_threadpool(
                    validate_candidate,
                    candidate.frame,
                    context=candidate.to_context(
                        layer_id=layer_id,
                        display_name=display_name,
                        attribute_columns=attributes,
                        installed_layers=other_installed,
                        replacing=replacing,
                    ),
                ),
            )
            highlight = _highlight_positions(findings)
            session = request.app.state.sessions.add(
                candidate,
                findings=findings,
                layer_id=layer_id,
                display_name=display_name,
                highlight=highlight,
            )
            # The session owns the candidate from here; it must not be cleaned
            # up on the way out of this function.
            candidate = None
            return {
                "session_id": session.id,
                "layer_id": layer_id,
                "display_name": display_name,
                "source_kind": session.candidate.source_kind,
                "source_files": list(session.candidate.source_files),
                "vintage": session.candidate.vintage,
                "facts": _json_safe(session.candidate.facts),
                "findings": [found.to_dict() for found in findings],
                "blocking": has_blocking(list(findings)),
                "highlight": list(highlight),
            }
        except UploadRejected as rejected:
            return _error(rejected.status_code, rejected.code, rejected.message)
        finally:
            # The upload folder has done its job the moment the reader has a
            # frame: `read_candidate` loads the whole layer into memory and
            # stages anything it needs to keep into a workspace of its own,
            # which the session owns. Anything still here is the raw upload.
            shutil.rmtree(upload_root, ignore_errors=True)
            if candidate is not None:
                # Something failed between reading and storing. Nothing owns
                # this candidate, so nothing else will ever delete its
                # workspace.
                candidate.cleanup()

    @app.get("/api/preview/{session_id}")
    def preview_candidate(session_id: str, request: Request) -> Any:
        """The candidate drawn over the layers this instance already serves.

        The rows PIP-L008 called broken are passed as `highlight=`, so the map
        marks exactly the shapes the findings list is talking about.

        The layer being replaced is excluded from the comparison: reinstalling
        `police_districts` should be compared against the *other* layers, not
        against the copy it is about to supersede, which would otherwise report
        a perfect overlap with itself. `_split_replaced` is that exclusion, and
        `/api/inspect` calls the same function on the same session's `layer_id`
        — the two used to disagree, and the disagreement made reinstalling any
        layer impossible.
        """
        session = request.app.state.sessions.get(session_id)
        if session is None:
            return _error(
                404,
                "unknown_session",
                "that inspection is no longer being held — inspect the file "
                "again",
            )

        _replaced, installed = _split_replaced(
            request.app.state.installed, session.layer_id
        )
        preview = build_preview(
            session.candidate.frame,
            layer_id=session.layer_id,
            display_name=session.display_name,
            installed=installed,
            highlight=session.highlight,
        )
        return {"session_id": session.id, "preview": preview.to_dict()}

    @app.post("/api/commit/{session_id}")
    def commit_candidate(session_id: str, request: Request) -> Any:
        """F8-T6 — write the layer and register it in config.toml.

        Present as a route so the shape of the app is settled, and answering
        plainly rather than 404 so the page can tell "not built yet" from "that
        session is gone". D24: nothing in this app writes anything outside a
        temporary folder until this is implemented and the operator has
        confirmed.
        """
        return _error(
            501,
            "not_implemented",
            "installing a layer is not built yet — this build of the tool "
            "inspects and previews only",
        )

    def _page(route: str) -> Any:
        """One of the four files the page is made of, or a plain 404.

        Behind `_guard` like everything else in this app — the middleware is
        registered on the application, not on the API routes, so the document
        itself is refused without this run's token exactly as `/api/inspect`
        is. That is deliberate: a page reachable without the token is a page a
        DNS-rebinding tab can read, and reading it tells an attacker the shape
        of every request this tool accepts.

        It also means the token cannot arrive with the first navigation, since a
        browser sends no custom header when it follows a link and a URL fragment
        never reaches a server at all. Delivering this first document is F8-T7's
        problem and it is called out here rather than solved quietly: nothing in
        this task loosens the guard to make a demo work.

        `no-store` because the page carries no token but the browser cache
        outlives the run that served it, and a stale copy of this page pointed
        at a later run's port is a confusing failure.
        """
        name, media_type = ADMIN_PAGE_FILES[route]
        path = ADMIN_PAGE_DIR / name
        if not path.is_file():
            return _error(
                404,
                "page_missing",
                "this build of the tool is missing its own page files",
            )
        return FileResponse(
            path, media_type=media_type, headers={"Cache-Control": "no-store"}
        )

    # Registered from the table so that the routes and the table cannot drift:
    # a file added to one is served by the other or by neither.
    for _route in ADMIN_PAGE_FILES:
        app.add_api_route(
            _route,
            (lambda route: lambda: _page(route))(_route),
            methods=["GET"],
            include_in_schema=False,
        )

    return app
