"""F8-T4 / H10 — the installer is not part of the public service, mechanically.

D22 says the layer installer lives in an application `app.main` never imports.
That is not a stylistic preference and it is not a comment: production runs
`uvicorn app.main:app`, the live instance is public, SPEC §3 defers
authentication out of v1, and the installer accepts file uploads and will
shortly rewrite `config.toml`. Anything reachable from `app.main` is served to
the internet.

The invariant holds today. This file is what stops a later refactor from
breaking it with the best of intentions — a shared helper moved "somewhere
sensible", a convenience import at the top of `app/main.py`, a router mounted
"just for local development". Each of those is a one-line change, none of them
looks like a security decision, and all of them put upload endpoints on the
public app.

Three claims, and they are different claims
    1. Nothing a request can *reach* on the public app comes from `app.admin` —
       endpoints on the app itself and endpoints inside anything mounted on it,
       walked recursively. This catches the router or sub-application that was
       mounted. It is written as a walk into the mounted app's own routes on
       purpose: an earlier version asked what module the mounted *object's class*
       came from, which is `fastapi.applications` for every FastAPI app there has
       ever been, so it was a branch that could not fire.
       `test_the_route_walk_can_actually_fail` mounts the real installer and
       asserts the walk finds it, so this claim is known to be falsifiable rather
       than assumed to be.
    2. Importing `app.main` — and calling `create_app()` — does not import
       `app.admin` **at all**, checked in a fresh interpreter, because by the
       time this test suite runs, other tests have long since imported
       `app.admin` and an in-process check would pass no matter what. This
       catches the import that has not been wired to a route *yet*, which is the
       state a mistake spends its first day in, and building the app catches the
       import written inside `create_app` rather than at the top of the file.
    3. `app/main.py` does not name the installer, read as text. The cheapest
       check and the only one that sees an import behind an environment-variable
       switch, which by construction is off in the probe interpreter and would
       be on in exactly the deployment nobody meant to change.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_LAYERS = REPO_ROOT / "data" / "layers.gpkg"

needs_shipped_layers = pytest.mark.skipif(
    not SHIPPED_LAYERS.exists(),
    reason="data/layers.gpkg not built (run scripts/build_data.py)",
)


def _is_installer_module(module: str) -> bool:
    return module == "app.admin" or module.startswith("app.admin.")


def reachable_endpoints(app: object, prefix: str = "", depth: int = 0) -> list[tuple[str, str]]:
    """Every `(path, defining module)` a request can reach from `app`.

    Mounts are followed, because a mount is the whole point: `app.mount("/x",
    create_admin_app(...))` puts every installer endpoint on the public app
    while adding exactly one route object to it.

    Asking what *module the mounted object's class* came from — the shape this
    test used to have — cannot answer that. `create_admin_app` returns a
    `FastAPI`, so that module is `fastapi.applications` no matter which app it
    is, and the check was a branch that could not fire. The question with an
    answer is what the mounted app *routes to*: its endpoint functions were
    defined inside `app/admin/main.py` and carry `__module__ == "app.admin.main"`
    wherever the object they hang off ends up.

    `depth` bounds the walk rather than tracking visited objects: an ASGI app
    can legitimately appear under two mounts, and 12 levels is far past any
    honest nesting while still terminating on a cycle.
    """
    if depth > 12:
        return []
    found: list[tuple[str, str]] = []
    for route in getattr(app, "routes", ()) or ():
        path = prefix + (getattr(route, "path", "") or repr(route))
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None:
            found.append((path, getattr(endpoint, "__module__", "") or ""))
        mounted = getattr(route, "app", None)
        if mounted is not None and mounted is not app:
            # The mounted object itself, and then everything it routes to.
            found.append((path, type(mounted).__module__))
            found.extend(reachable_endpoints(mounted, prefix=path, depth=depth + 1))
    return found


@needs_shipped_layers
def test_no_public_route_comes_from_the_installer() -> None:
    """Every endpoint reachable from the public app, asked where its code lives.

    Reachable, not listed: the walk descends into mounted sub-applications, so
    mounting the whole installer under a path is caught by the same assertion
    that catches a single stray `@app.get` copied out of it.
    """
    from app.main import create_app

    app = create_app()
    reached = reachable_endpoints(app)
    offenders = sorted(
        {path for path, module in reached if _is_installer_module(module)}
    )

    assert not offenders, (
        f"these public routes come from the installer: {offenders}. The "
        f"installer is a separate app for a reason — see D22."
    )


@needs_shipped_layers
def test_the_route_walk_can_actually_fail() -> None:
    """The assertion above, proved to have teeth.

    A test that guards an invariant is worth exactly what it is worth when the
    invariant is broken, and the previous version of the route walk asked a
    question (`type(mounted).__module__`) whose answer was `fastapi.applications`
    for every possible mount — a branch that could not fire. So this mounts the
    real installer on a real public app and asserts the walk finds it, and names
    the endpoints it found.

    Nothing here touches `app/main.py`. The sabotage is applied to an app object
    in this process, which is the same thing a one-line `app.mount(...)` in
    `create_app` would produce.
    """
    from app.admin.main import create_admin_app, mint_token
    from app.main import create_app

    public = create_app()
    assert not [
        path for path, module in reachable_endpoints(public) if _is_installer_module(module)
    ]

    public.mount("/admin", create_admin_app(token=mint_token(), port=8765))
    caught = sorted(
        {path for path, module in reachable_endpoints(public) if _is_installer_module(module)}
    )
    assert "/admin/api/inspect" in caught, caught
    assert "/admin/api/commit/{session_id}" in caught, caught


def test_importing_the_public_app_does_not_import_the_installer() -> None:
    """A clean interpreter, because this process is not one.

    `app.admin` is already in `sys.modules` by the time pytest reaches this
    file — every other F8 test imported it — so the only honest place to ask
    the question is a subprocess that has imported nothing else.
    """
    probe = (
        "import sys\n"
        "import app.main\n"
        "leaked = sorted(name for name in sys.modules "
        "if name == 'app.admin' or name.startswith('app.admin.'))\n"
        "print(','.join(leaked))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    leaked = result.stdout.strip()
    assert leaked == "", (
        f"importing app.main pulled in {leaked}. The public deployment must not "
        f"be able to serve what it has never imported — see D22."
    )


@needs_shipped_layers
def test_building_the_public_app_does_not_import_the_installer() -> None:
    """The same question, asked of `create_app()` and not just of the import.

    Importing a module runs its top level and nothing else, so a probe that only
    imports cannot see an import written *inside* `create_app` — and that is
    where a "just for local development" mount would be written, because that is
    where the app object is. Building the app in the fresh interpreter runs that
    code, so the function-body import is caught here rather than left to the text
    scan below.

    Today the test above catches that case too, but only incidentally: `app/main.py`
    ends with a module-level `app = create_app()`, so importing it happens to run
    the function body. That is a line someone could reasonably delete — moving to
    a factory-only `uvicorn --factory app.main:create_app` would do it — and the
    coverage would go with it silently. This test does not depend on it.

    Still not the whole answer: an import behind an environment-variable switch
    stays invisible to this, which is why the text scan exists and why it is not
    redundant with this test.
    """
    probe = (
        "import sys\n"
        "import app.main\n"
        "app.main.create_app()\n"
        "leaked = sorted(name for name in sys.modules "
        "if name == 'app.admin' or name.startswith('app.admin.'))\n"
        "print(','.join(leaked))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    leaked = result.stdout.strip()
    assert leaked == "", (
        f"building the public app pulled in {leaked}. See D22."
    )


def test_the_installer_is_not_named_anywhere_in_the_public_app() -> None:
    """The source itself, read as text.

    Cheap, and it catches the case the two tests above cannot: an import written
    inside a function, or behind an environment-variable switch, which neither a
    route walk nor a module-import probe would see until the day it fired. An
    environment variable is not a boundary — it is a boundary-shaped thing that
    is one stray deployment line away from being off.
    """
    source = (REPO_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "app.admin" not in source
    assert "admin" not in source.lower().replace("administrator", "")
