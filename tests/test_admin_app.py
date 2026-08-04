"""F8-T4 — tests for the installer app.

Two things are being tested here and they are not the same thing.

The first is the ordinary work: a real GeoJSON goes in, facts and findings come
out, the preview draws it over the layers this instance already serves.

The second is the boundary (D23), and it is the reason this file is long. The
installer accepts uploads and will shortly rewrite `config.toml`, it runs with
no authentication of its own beyond a per-run token, and the browser is capable
of pointing a hostile page's own hostname at 127.0.0.1 and posting to it. So
every refusal is tested as carefully as every acceptance: no token, wrong token,
a `Host` that is not this run, a body larger than the cap, a filename built to
escape the folder it is written into. A guard nobody tested is a guard nobody
has.

Offline throughout. Nothing here opens a socket that leaves the machine, and the
one network-shaped input — an ArcGIS address — is not exercised here because
`tests/test_admin_inspect.py` already does it against a mocked service.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import geopandas as gpd
import httpx
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Polygon

from app.admin import main as installer
from app.admin.codes import build_finding
from app.admin.main import (
    ADMIN_TOKEN_HEADER,
    allowed_hosts,
    create_admin_app,
    mint_token,
    safe_upload_filename,
)
from app.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_LAYERS = REPO_ROOT / "data" / "layers.gpkg"
WARD_25_PRECINCTS = REPO_ROOT / "shapefiles" / "ward25_precincts.geojson"

PORT = 8765
BASE_URL = f"http://127.0.0.1:{PORT}"

needs_shipped_layers = pytest.mark.skipif(
    not SHIPPED_LAYERS.exists(),
    reason="data/layers.gpkg not built (run scripts/build_data.py)",
)
needs_ward_25 = pytest.mark.skipif(
    not WARD_25_PRECINCTS.exists(), reason="shapefiles/ward25_precincts.geojson absent"
)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def token() -> str:
    return mint_token()


def build_client(token: str, **kwargs) -> TestClient:
    """A client whose Host header is a real one for this run.

    `base_url` is what sets it: httpx derives `Host` from the address, so a test
    that forgets this gets `testserver` and is refused — which is itself the
    behaviour `test_a_foreign_host_is_refused` relies on.
    """
    app = create_admin_app(load_config(), token=token, port=PORT, **kwargs)
    client = TestClient(app, base_url=BASE_URL)
    client.headers[ADMIN_TOKEN_HEADER] = token
    return client


@pytest.fixture
def client(token: str) -> TestClient:
    with build_client(token) as ready:
        yield ready


def square(offset: float) -> Polygon:
    return Polygon(
        [
            (offset, 41.8),
            (offset, 41.81),
            (offset + 0.01, 41.81),
            (offset + 0.01, 41.8),
        ]
    )


def zipped_shapefile(tmp_path: Path, stem: str = "wards") -> bytes:
    """A real shapefile set, written by GDAL and zipped — the thing a portal
    hands out, and the one upload that makes the reader unpack to disk."""
    staging = tmp_path / f"staging-{stem}"
    staging.mkdir(parents=True, exist_ok=True)
    frame = gpd.GeoDataFrame(
        {"ward": ["1", "2"], "geometry": [square(-87.6), square(-87.58)]},
        geometry="geometry",
        crs="EPSG:4326",
    )
    frame.to_file(staging / f"{stem}.shp")
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(staging.iterdir()):
            archive.write(item, item.name)
    return packed.getvalue()


def inspect_geojson(client: TestClient, *, filename: str = "precincts.geojson"):
    return client.post(
        "/api/inspect",
        files=[
            (
                "files",
                (filename, WARD_25_PRECINCTS.read_bytes(), "application/geo+json"),
            )
        ],
        data={"layer_id": "ward25_precincts", "display_name": "Ward 25 precincts"},
    )


def temp_workspaces() -> set[Path]:
    """Every workspace this feature has left in $TMPDIR right now."""
    root = Path(tempfile.gettempdir())
    return {
        path
        for path in root.glob("pip-*")
        if path.is_dir() and path.name.startswith(("pip-layer-", "pip-upload-"))
    }


# --------------------------------------------------------------------------
# the ordinary work
# --------------------------------------------------------------------------


@needs_shipped_layers
@needs_ward_25
def test_inspect_reads_a_real_layer_and_reports_findings(client: TestClient) -> None:
    """End to end on a file that ships with this repo: facts, findings, a session."""
    response = inspect_geojson(client)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["session_id"]
    assert body["source_kind"] == "geojson"
    assert body["source_files"] == ["precincts.geojson"]
    assert body["facts"]["feature_count"] > 0
    assert isinstance(body["blocking"], bool)

    codes = [found["code"] for found in body["findings"]]
    # A GeoJSON has nowhere to record how old it is, so PIP-L017 always fires —
    # and it fires from the reader, whose copy of it is the one that survives
    # the merge.
    assert "PIP-L017" in codes
    assert all(found["message"] for found in body["findings"])
    # Sorted by severity then code, so the page does not reshuffle on refresh.
    severities = [found["severity"] for found in body["findings"]]
    assert severities == sorted(severities, key=lambda name: name != "blocking")


@needs_shipped_layers
@needs_ward_25
def test_preview_draws_the_candidate_over_the_installed_layers(
    client: TestClient,
) -> None:
    session_id = inspect_geojson(client).json()["session_id"]

    response = client.get(f"/api/preview/{session_id}")
    assert response.status_code == 200, response.text
    preview = response.json()["preview"]

    assert preview["candidate"]["feature_count"] > 0
    assert preview["installed"], "the layers already serving must be in the payload"
    assert {layer["role"] for layer in preview["installed"]} == {"installed"}
    assert preview["viewport"] is not None
    # The whole payload has to survive json.dumps unaided — the page is the only
    # consumer and it speaks nothing else.
    json.dumps(preview)


@needs_shipped_layers
@needs_ward_25
def test_reinstalling_a_layer_previews_it_and_stays_installable(
    client: TestClient,
) -> None:
    """Reinstalling a layer compares it against the others — and is allowed.

    Two halves of one rule, and this test used to assert only the first. The
    preview excluded the layer being replaced; `/api/inspect` did not, so it
    handed the validator every installed layer including the one being
    superseded, PIP-L009 fired on the candidate's own name, and `blocking` came
    back true. `blocking` is the single boolean F8-T5 puts the Install button
    behind, so the operator got a preview built for a replacement and a button
    that could never be pressed: updating a layer was impossible, which is the
    commonest reason to open this tool after the first use.

    Reproduced before the fix: this same request returned 200 with
    `blocking: true` and PIP-L009 saying "the name 'police_districts' already
    belongs to the installed layer 'Chicago Police Districts'".
    """
    installed = {layer.id: layer for layer in client.app.state.installed}
    replaced = sorted(installed)[0]

    response = client.post(
        "/api/inspect",
        files=[("files", ("precincts.geojson", WARD_25_PRECINCTS.read_bytes()))],
        data={"layer_id": replaced},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    codes = {found["code"]: found for found in body["findings"]}

    # The half that was already true.
    session_id = body["session_id"]
    preview = client.get(f"/api/preview/{session_id}").json()["preview"]
    assert replaced not in {layer["id"] for layer in preview["installed"]}

    # The half that was not: the same response has to be installable.
    assert "PIP-L009" not in codes
    assert body["blocking"] is False, codes

    # And the replacement is still visible — a warning, which is this
    # registry's word for "you may install this once you have looked at it".
    assert "PIP-L020" in codes
    assert codes["PIP-L020"]["severity"] == "warning"
    assert codes["PIP-L020"]["detail"]["replacing_id"] == replaced
    assert installed[replaced].name in codes["PIP-L020"]["specifics"]


@needs_shipped_layers
@needs_ward_25
def test_only_the_layer_being_replaced_is_dropped_from_the_comparison(
    client: TestClient,
) -> None:
    """The control: the exclusion is one layer wide, not a switch that turns the
    installed layers off.

    "Compare against the others" is only worth anything if the others are still
    there. A `_split_replaced` that dropped everything — or a caller that passed
    the empty tuple — would make the test above pass and quietly disable
    PIP-L009 and PIP-L016 for every install there will ever be.
    """
    all_ids = {layer.id for layer in client.app.state.installed}
    replaced = sorted(all_ids)[0]

    body = client.post(
        "/api/inspect",
        files=[("files", ("precincts.geojson", WARD_25_PRECINCTS.read_bytes()))],
        data={"layer_id": replaced},
    ).json()
    preview = client.get(f"/api/preview/{body['session_id']}").json()["preview"]
    assert {layer["id"] for layer in preview["installed"]} == all_ids - {replaced}

    # And a name that belongs to nobody replaces nothing: every installed layer
    # is compared against, and there is no replacement warning at all.
    fresh = client.post(
        "/api/inspect",
        files=[("files", ("precincts.geojson", WARD_25_PRECINCTS.read_bytes()))],
        data={"layer_id": "a_name_no_installed_layer_uses"},
    ).json()
    assert "PIP-L020" not in {found["code"] for found in fresh["findings"]}
    fresh_preview = client.get(f"/api/preview/{fresh['session_id']}").json()["preview"]
    assert {layer["id"] for layer in fresh_preview["installed"]} == all_ids


@needs_shipped_layers
def test_preview_of_an_unknown_session_is_a_clean_404(client: TestClient) -> None:
    response = client.get("/api/preview/nothing-was-ever-here")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_session"


@needs_shipped_layers
def test_commit_is_not_built_yet_and_says_so(client: TestClient) -> None:
    """D24 — the route exists so the shape is settled; it writes nothing."""
    response = client.post("/api/commit/whatever")
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "not_implemented"


@needs_shipped_layers
def test_loose_shapefile_parts_arrive_as_one_set(
    client: TestClient, tmp_path: Path
) -> None:
    """The multipart body this module parses by hand, at its most demanding.

    Five binary parts in one request, one of them tens of kilobytes, so the
    body crosses chunk boundaries and the parser has to hold a partial boundary
    across a read without either losing bytes or splicing them into the file.
    A shapefile is exactly the input that notices: a single wrong byte in the
    .shp and GDAL refuses the whole set.
    """
    staging = tmp_path / "loose"
    staging.mkdir()
    gpd.GeoDataFrame(
        {
            "ward": [str(index) for index in range(200)],
            "geometry": [square(-87.6 + index * 0.001) for index in range(200)],
        },
        geometry="geometry",
        crs="EPSG:4326",
    ).to_file(staging / "wards.shp")

    parts = [
        ("files", (item.name, item.read_bytes(), "application/octet-stream"))
        for item in sorted(staging.iterdir())
    ]
    assert len(parts) >= 4
    assert max(len(payload) for _, (_, payload, _) in parts) > 16 * 1024

    response = client.post("/api/inspect", files=parts, data={"layer_id": "wards"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source_kind"] == "shapefile"
    assert "wards.shp" in body["source_files"]
    assert body["facts"]["feature_count"] == 200


@needs_shipped_layers
def test_a_web_address_this_tool_will_not_fetch_is_a_finding(
    client: TestClient,
) -> None:
    """The `url` field reaches the reader, and its refusal reaches the operator.

    `file:` is the one worth naming: an address field on a local tool is a
    request to read the machine it runs on, and the answer is a finding rather
    than a fetch. No socket is opened by this test.
    """
    response = client.post(
        "/api/inspect", data={"url": "file:///etc/passwd"}
    )
    assert response.status_code == 400
    finding = response.json()["error"]["finding"]
    assert finding["code"] == "PIP-L014"


# --------------------------------------------------------------------------
# D23 — the token
# --------------------------------------------------------------------------


def test_an_app_cannot_be_built_without_a_token() -> None:
    """No insecure default. A token that has a default is a published token."""
    with pytest.raises(ValueError):
        create_admin_app(token="", port=PORT)


@needs_shipped_layers
def test_a_request_with_no_token_is_refused(client: TestClient) -> None:
    response = client.get("/api/preview/anything", headers={ADMIN_TOKEN_HEADER: ""})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@needs_shipped_layers
def test_a_request_with_the_wrong_token_is_refused(client: TestClient) -> None:
    response = client.get(
        "/api/preview/anything", headers={ADMIN_TOKEN_HEADER: "not-the-token"}
    )
    assert response.status_code == 403


@needs_shipped_layers
def test_the_token_is_never_echoed_back(client: TestClient, token: str) -> None:
    """It is a secret for as long as the process lives; nothing repeats it."""
    responses = [
        client.post("/api/commit/x"),
        client.get("/api/preview/x"),
        client.get("/api/preview/x", headers={ADMIN_TOKEN_HEADER: "wrong"}),
    ]
    for response in responses:
        assert token not in response.text
        assert token not in json.dumps(dict(response.headers))


@needs_shipped_layers
def test_the_token_is_not_accepted_in_the_query_string(client: TestClient) -> None:
    """A URL-borne token leaks through Referer, history and every access log.

    So the query string is not a place this app looks, and a request that puts
    it there is refused exactly as if it had carried nothing at all.
    """
    response = client.get(
        f"/api/preview/x?token={client.headers[ADMIN_TOKEN_HEADER]}",
        headers={ADMIN_TOKEN_HEADER: ""},
    )
    assert response.status_code == 403


@needs_shipped_layers
@pytest.mark.parametrize(
    "raw_token",
    [
        b"caf\xe9",  # one byte over 0x7f, the minimal case
        b"\xff" * 43,  # the right length, none of it ASCII
        b"\x00abc",  # a NUL, which is also not what a str comparison expects
    ],
    ids=["one-non-ascii-byte", "all-high-bytes", "embedded-nul"],
)
def test_a_token_header_that_is_not_ascii_is_refused_not_a_500(
    client: TestClient, raw_token: bytes
) -> None:
    """A byte over 0x7f in the token header is a refusal, not a traceback.

    Starlette decodes headers as latin-1, so any such byte becomes a non-ASCII
    `str` and `secrets.compare_digest` raises `TypeError` on it — raised inside
    the guard and *outside* its `except`, so the module's "no traceback, ever"
    contract would not have caught it. That made the one pre-authentication
    path the only one in the app that answered a hostile local process with a
    500, a `text/plain` body and a full traceback in the log.

    The headers here are bytes on purpose: httpx refuses a non-ASCII `str`
    header value, which is exactly why no test written the ordinary way ever
    reached this.
    """
    response = client.post(
        "/api/commit/x",
        headers=[
            (b"host", BASE_URL.removeprefix("http://").encode("ascii")),
            (ADMIN_TOKEN_HEADER.lower().encode("ascii"), raw_token),
        ],
    )
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "forbidden"


@needs_shipped_layers
def test_the_right_token_sent_as_bytes_is_still_accepted(
    client: TestClient, token: str
) -> None:
    """The bytes comparison did not break the accepting half of the guard.

    A 403 for everything would satisfy the test above and be useless, so this
    is the other side: the real token, sent as raw bytes, still gets past the
    guard and reaches the route (which 404s on an unknown session — a routed
    answer, not a refusal).
    """
    response = client.get(
        "/api/preview/no-such-session",
        headers=[
            (b"host", BASE_URL.removeprefix("http://").encode("ascii")),
            (ADMIN_TOKEN_HEADER.lower().encode("ascii"), token.encode("ascii")),
        ],
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_session"


def asgi_request(app, raw_headers: list[tuple[bytes, bytes]], path: str = "/api/commit/x"):
    """One request put straight into the ASGI app, headers untouched.

    `TestClient` cannot be used for this: httpx re-encodes a header value it was
    given as bytes, so `b"\\xb2"` arrives as `b"\\xc2\\xb2"` and the byte under
    test is gone before the app sees it. This is the interface uvicorn calls,
    called the way uvicorn calls it.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", PORT),
    }
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(item for item in sent if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body", b"") for item in sent if item["type"] == "http.response.body"
    )
    headers = {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in start["headers"]
    }
    return start["status"], headers, body


@needs_shipped_layers
def test_a_content_length_of_unicode_digits_is_not_a_500(token: str) -> None:
    """`"²".isdigit()` is True and `int("²")` is a ValueError.

    Same class of defect as the token header, one check further down: the guard
    ran `int(declared)` on anything `str.isdigit()` liked, and a superscript two
    is a single latin-1 byte (0xb2), so nothing about the decode stops it from
    reaching that `int()`. Like the token comparison it would have raised
    outside the guard's `except`.

    A production `h11` server rejects a non-numeric `Content-Length` itself, so
    this one is depth rather than a live hole — which is exactly why it is
    tested at the ASGI boundary and not through a client: the guard's job is to
    be total over its own inputs, not to assume the layer above it filtered
    them.
    """
    app = create_admin_app(load_config(), token=token, port=PORT)
    status, headers, body = asgi_request(
        app,
        [
            (b"host", BASE_URL.removeprefix("http://").encode("ascii")),
            (ADMIN_TOKEN_HEADER.lower().encode("ascii"), token.encode("ascii")),
            (b"content-length", "²".encode("latin-1")),
        ],
    )
    assert status != 500
    assert headers["content-type"].startswith("application/json")
    assert json.loads(body)["error"]["code"] == "not_implemented"


@needs_shipped_layers
def test_the_token_guard_is_total_over_every_single_byte(token: str) -> None:
    """Every one of the 256 values a header byte can hold, answered with a 403.

    The point of comparing bytes rather than a decoded `str` is that there is no
    value left that has no answer. This is the assertion that says so literally,
    at the ASGI boundary so that no client library normalises the byte away.
    """
    app = create_admin_app(load_config(), token=token, port=PORT)
    for byte in range(256):
        status, _headers, body = asgi_request(
            app,
            [
                (b"host", BASE_URL.removeprefix("http://").encode("ascii")),
                (ADMIN_TOKEN_HEADER.lower().encode("ascii"), bytes([byte]) * 4),
            ],
        )
        assert status == 403, f"byte {byte:#04x} was not a clean refusal"
        assert json.loads(body)["error"]["code"] == "forbidden"


# --------------------------------------------------------------------------
# D23 — the Host allowlist (DNS rebinding)
# --------------------------------------------------------------------------


def test_the_allowlist_is_the_loopback_names_with_this_port() -> None:
    assert allowed_hosts(8765) == {
        "localhost:8765",
        "127.0.0.1:8765",
        "[::1]:8765",
    }
    # Port 80 is the one port a Host header may legally leave out, so both forms
    # name the same address there and both are accepted.
    assert "localhost" in allowed_hosts(80)
    assert "localhost" not in allowed_hosts(8765)


@needs_shipped_layers
def test_a_foreign_host_is_refused(client: TestClient) -> None:
    """The DNS-rebinding case: the request reached the right socket, and the
    browser is still calling it by the attacker's name."""
    response = client.post(
        "/api/commit/x", headers={"Host": "evil.example.com"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_host"


@needs_shipped_layers
def test_the_host_is_checked_before_the_token(client: TestClient) -> None:
    """A rebinding page must not be able to use this app as an oracle for
    anything, including whether a token it guessed was the right one."""
    response = client.post(
        "/api/commit/x",
        headers={"Host": "evil.example.com", ADMIN_TOKEN_HEADER: "wrong"},
    )
    assert response.json()["error"]["code"] == "bad_host"


@needs_shipped_layers
def test_a_loopback_host_on_the_wrong_port_is_refused(client: TestClient) -> None:
    response = client.post("/api/commit/x", headers={"Host": "127.0.0.1:9999"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_host"


# --------------------------------------------------------------------------
# clean errors, never a traceback
# --------------------------------------------------------------------------


@needs_shipped_layers
def test_a_file_that_is_not_map_data_comes_back_as_a_finding(
    client: TestClient,
) -> None:
    """A CandidateError is the operator's answer, not an exception to leak."""
    response = client.post(
        "/api/inspect",
        files=[("files", ("notes.txt", b"this is not a map", "text/plain"))],
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "layer_not_readable"
    finding = body["error"]["finding"]
    assert finding["code"] == "PIP-L001"
    assert finding["severity"] == "blocking"
    assert "Traceback" not in response.text


@needs_shipped_layers
def test_an_incomplete_shapefile_set_names_the_missing_pieces(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/inspect",
        files=[("files", ("wards.shp", b"\x00\x00\x27\x0a" + b"\x00" * 96))],
    )
    assert response.status_code == 400
    finding = response.json()["error"]["finding"]
    assert finding["code"] == "PIP-L002"
    assert ".dbf" in finding["message"]


@needs_shipped_layers
@needs_ward_25
def test_an_unexpected_failure_is_one_clean_sentence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI in this project exits 2 rather than printing a stack trace, and
    this is the same decision: the operator cannot act on a traceback, and a
    traceback names paths on their disk."""
    session_id = inspect_geojson(client).json()["session_id"]

    def explode(*args, **kwargs):
        raise RuntimeError("a bug nobody anticipated")

    monkeypatch.setattr(installer, "build_preview", explode)
    response = client.get(f"/api/preview/{session_id}")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "Nothing was installed" in body["error"]["message"]
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


# --------------------------------------------------------------------------
# upload limits
# --------------------------------------------------------------------------


@needs_shipped_layers
def test_an_upload_over_the_cap_is_refused_on_its_declared_size(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused before it is read: the cap exists to not pay the cost."""
    monkeypatch.setattr(installer, "MAX_UPLOAD_BYTES", 2048)
    response = client.post(
        "/api/inspect",
        files=[("files", ("big.geojson", b"x" * 8192))],
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_too_large"


@needs_shipped_layers
def test_an_upload_over_the_cap_is_refused_without_a_declared_size(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `Content-Length` is a claim by the sender, and a chunked body makes no
    claim at all — so the bytes are counted as they arrive."""
    monkeypatch.setattr(installer, "MAX_UPLOAD_BYTES", 2048)
    boundary = "----pip"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="big.geojson"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    def body():
        yield head
        for _ in range(8):
            yield b"x" * 1024
        yield tail

    response = client.post(
        "/api/inspect",
        content=body(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert "content-length" not in {
        name.lower() for name in response.request.headers
    }
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_too_large"


@needs_shipped_layers
def test_too_many_parts_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer, "MAX_UPLOAD_PARTS", 3)
    response = client.post(
        "/api/inspect",
        files=[("files", (f"part{index}.shp", b"x")) for index in range(6)],
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "too_many_files"


@needs_shipped_layers
def test_an_empty_request_says_what_to_send(client: TestClient) -> None:
    response = client.post("/api/inspect", data={"layer_id": "wards"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@needs_shipped_layers
def test_a_body_that_is_neither_form_shape_is_refused(client: TestClient) -> None:
    response = client.post("/api/inspect", json={"url": "https://example.gov/x"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@needs_shipped_layers
def test_files_and_a_url_together_are_refused(client: TestClient) -> None:
    response = client.post(
        "/api/inspect",
        files=[("files", ("a.geojson", b"{}"))],
        data={"url": "https://gis.example.gov/rest/services/x/MapServer/0"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


# --------------------------------------------------------------------------
# the filename is attacker-controlled
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\config", "config"),
        ("/etc/shadow", "shadow"),
        ("C:evil.geojson", "evil.geojson"),
        ("..", "fallback"),
        (".", "fallback"),
        ("", "fallback"),
        ("wards\x00.shp", "wards.shp"),
        ("w" * 400 + ".shp", "w" * 255),
        ("wards.geojson", "wards.geojson"),
    ],
)
def test_a_filename_is_reduced_to_one_harmless_component(
    raw: str, expected: str
) -> None:
    assert safe_upload_filename(raw, fallback="fallback") == expected


@needs_shipped_layers
@needs_ward_25
def test_a_traversing_filename_does_not_escape_the_workspace(
    client: TestClient,
) -> None:
    """`../../etc/passwd.geojson` is a perfectly ordinary thing to receive, and
    the only thing that may come of it is a file called `passwd.geojson` inside
    this request's own folder."""
    marker = REPO_ROOT / "passwd.geojson"
    assert not marker.exists()
    before = temp_workspaces()

    response = client.post(
        "/api/inspect",
        files=[
            (
                "files",
                ("../../etc/passwd.geojson", WARD_25_PRECINCTS.read_bytes()),
            )
        ],
    )

    assert response.status_code == 200, response.text
    # The operator is told the name their file had, reduced to a bare one.
    assert response.json()["source_files"] == ["passwd.geojson"]
    assert not marker.exists()
    assert not (REPO_ROOT.parent / "passwd.geojson").exists()
    # Nothing was left in $TMPDIR outside the session's own workspace, and a
    # GeoJSON needs no workspace at all.
    assert temp_workspaces() == before


# --------------------------------------------------------------------------
# sessions, and the temporary folders they own
# --------------------------------------------------------------------------


@needs_shipped_layers
def test_eviction_releases_the_evicted_workspace(
    token: str, tmp_path: Path
) -> None:
    """The cap is a promise about disk, not merely about memory.

    A zipped shapefile is the upload that makes the reader unpack to disk, so
    this is the case where an evicted session that forgot to clean up leaves a
    full copy of somebody's data in $TMPDIR forever.
    """
    archive = zipped_shapefile(tmp_path)
    with build_client(token, max_sessions=1) as client:
        first = client.post(
            "/api/inspect", files=[("files", ("wards.zip", archive))]
        )
        assert first.status_code == 200, first.text
        first_id = first.json()["session_id"]
        held = client.app.state.sessions.get(first_id)
        workspace = held.candidate.workspace
        assert workspace is not None and workspace.exists()

        second = client.post(
            "/api/inspect", files=[("files", ("wards.zip", archive))]
        )
        assert second.status_code == 200, second.text

        assert not workspace.exists(), "the evicted session left its files behind"
        assert client.app.state.sessions.get(first_id) is None
        assert client.get(f"/api/preview/{first_id}").status_code == 404


@needs_shipped_layers
def test_an_expired_session_is_released(token: str, tmp_path: Path) -> None:
    archive = zipped_shapefile(tmp_path)
    with build_client(token, session_ttl_seconds=0.05) as client:
        response = client.post(
            "/api/inspect", files=[("files", ("wards.zip", archive))]
        )
        session_id = response.json()["session_id"]
        workspace = client.app.state.sessions.get(session_id).candidate.workspace
        assert workspace.exists()

        time.sleep(0.1)

        assert client.app.state.sessions.get(session_id) is None
        assert not workspace.exists()


@needs_shipped_layers
def test_shutdown_releases_everything(token: str, tmp_path: Path) -> None:
    """A tool left open all afternoon must not outlive itself in $TMPDIR."""
    archive = zipped_shapefile(tmp_path)
    with build_client(token) as client:
        response = client.post(
            "/api/inspect", files=[("files", ("wards.zip", archive))]
        )
        session_id = response.json()["session_id"]
        workspace = client.app.state.sessions.get(session_id).candidate.workspace
        assert workspace.exists()

    assert not workspace.exists()


@needs_shipped_layers
def test_the_upload_folder_is_deleted_even_when_the_read_fails(
    client: TestClient,
) -> None:
    """The raw upload is temporary in every path out of the endpoint."""
    before = temp_workspaces()
    client.post("/api/inspect", files=[("files", ("notes.txt", b"not a map"))])
    assert temp_workspaces() == before


# --------------------------------------------------------------------------
# the event loop — one slow inspect must not stop the tool answering
# --------------------------------------------------------------------------

# How long the stand-in reader takes. Generous next to a threadpool hop and far
# under the several seconds a real fetch of a published map service costs.
SLOW_READ_SECONDS = 1.0


@needs_shipped_layers
def test_a_slow_inspect_does_not_freeze_the_rest_of_the_tool(
    token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/api/inspect` is `async`, and everything slow in it must leave the loop.

    `read_candidate` and `validate_candidate` are ordinary synchronous
    functions — geopandas opening a file, and for a web address a synchronous
    HTTP fetch with a multi-second timeout. Awaited-into directly from an
    `async def` endpoint they run *on the event loop*, and a loop that is
    running a function is not answering anybody.

    Measured against a real uvicorn before the fix, with a stand-in host that
    slept 6 seconds: a `GET /api/preview/nope` that takes 0.05 s on an idle
    tool took 11.04 s while one inspect was in flight — 220x, on a route that
    touches nothing the inspect touches. The operator's own page, polling, is
    the first thing it stalls.

    So this drives the app through its ASGI interface on one event loop, starts
    an inspect whose reader sleeps, and times an unrelated request issued while
    that inspect is in flight. The stand-in sleeps with `time.sleep`, not
    `asyncio.sleep`, because blocking the thread is the property under test.
    """
    started = threading.Event()

    def slow_read(*_args, **_kwargs):
        started.set()
        time.sleep(SLOW_READ_SECONDS)
        raise installer.CandidateError(
            build_finding("PIP-L001", specifics="A stand-in for a slow read.")
        )

    monkeypatch.setattr(installer, "read_candidate", slow_read)
    app = create_admin_app(load_config(), token=token, port=PORT)
    headers = {ADMIN_TOKEN_HEADER: token}

    # When the second request is issued, measured from the moment the first one
    # is. Comfortably after the reader has begun sleeping and comfortably before
    # it stops.
    ISSUE_AFTER = SLOW_READ_SECONDS / 5

    async def scenario() -> tuple[float, int, bool, int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, headers=headers
        ) as caller:
            origin = time.monotonic()

            async def inspect_slowly():
                return await caller.post(
                    "/api/inspect",
                    files=[("files", ("wards.geojson", b"{}"))],
                    timeout=30.0,
                )

            async def ask_something_else():
                # The wait is armed now, off the same clock, so a frozen loop
                # cannot postpone the *decision* to ask — only the answer. That
                # is what makes the elapsed time below mean something: it is
                # measured from a fixed origin, not from a resumed coroutine.
                await asyncio.sleep(ISSUE_AFTER)
                response = await caller.get("/api/preview/nope", timeout=30.0)
                return time.monotonic() - origin, response

            slow = asyncio.ensure_future(inspect_slowly())
            answered_at, other = await ask_something_else()
            # Was the inspect still in flight when that answer arrived? With
            # the loop free it must be; with the loop blocked it cannot be,
            # because nothing else ran until it finished.
            still_running = not slow.done()
            slow_response = await slow
            assert started.is_set(), "the stand-in reader never ran"
            return (
                answered_at,
                other.status_code,
                still_running,
                slow_response.status_code,
            )

    answered_at, status, still_running, slow_status = asyncio.run(scenario())

    assert status == 404, "the unrelated request must be served, not queued away"
    # The whole claim. With the blocking read on the event loop this cannot be
    # less than SLOW_READ_SECONDS; with the hop it is ISSUE_AFTER plus
    # milliseconds. Half the sleep is a wide margin either side.
    assert answered_at < SLOW_READ_SECONDS / 2, (
        f"an unrelated request was not answered until {answered_at:.2f}s, "
        f"behind a {SLOW_READ_SECONDS:.0f}s inspect — the blocking read is "
        f"running on the event loop"
    )
    # Belt and braces on the same fact, without a clock: the slow request was
    # genuinely still in flight, so this is concurrency and not a race that let
    # the inspect finish first. And it still ends in the ordinary refusal.
    assert still_running, "the inspect had already finished — nothing was proved"
    assert slow_status == 400


@needs_shipped_layers
@needs_ward_25
def test_session_ids_are_opaque_and_unguessable(client: TestClient) -> None:
    ids = {inspect_geojson(client).json()["session_id"] for _ in range(3)}
    assert len(ids) == 3
    for session_id in ids:
        assert len(session_id) >= 16
        assert re.fullmatch(r"[A-Za-z0-9_-]+", session_id)
