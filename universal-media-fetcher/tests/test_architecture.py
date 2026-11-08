"""Architectural invariants.

§4 requires that no platform-specific logic lives in the main application. That
is easy to state and easy to erode: one ``if platform == "youtube"`` in a route
handler, and the adapter abstraction has a hole in it.

These tests are cheap and they fail loudly at the moment of erosion.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
UMF = APP_ROOT / "umf"

#: Platform identifiers that must not appear in core or api code.
PLATFORM_NAMES = {
    "youtube", "youtu", "vimeo", "dailymotion", "twitch", "archive_org",
    "internet_archive", "peertube", "instagram", "facebook", "twitter",
    "reddit", "tiktok", "pinterest", "netflix", "prime_video", "jiohotstar",
    "sony_liv", "zee5", "apple_tv", "hulu", "paramount",
}


def _iter_python(root: Path):
    yield from sorted(root.rglob("*.py"))


def _strings_and_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value.lower())
        elif isinstance(node, ast.Name):
            found.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            found.add(node.attr.lower())
    return found


@pytest.mark.parametrize("package", ["core", "api", "security"])
def test_core_does_not_know_platform_names(package: str) -> None:
    """A platform name in core/api/security means the abstraction has leaked."""
    offenders: list[str] = []
    for path in _iter_python(UMF / package):
        tokens = _strings_and_names(path)
        hits = {name for name in PLATFORM_NAMES if name in tokens}
        if hits:
            offenders.append(f"{path.relative_to(APP_ROOT)}: {sorted(hits)}")
    assert not offenders, "platform-specific logic leaked outside adapters:\n" + "\n".join(offenders)


def _imports(path: Path) -> set[str]:
    """Collect actual import targets, ignoring docstrings that mention them."""
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_core_does_not_import_adapters() -> None:
    """The registry may import adapters; the engine and security layers may not.

    Checked through the AST rather than by substring: a module that merely
    *mentions* ``umf.adapters`` in a docstring is not violating the boundary,
    and a test that cannot tell the difference would be deleted by the first
    person it inconvenienced.
    """
    violations: list[str] = []
    for package in ("core", "security"):
        for path in _iter_python(UMF / package):
            if path.name == "registry.py":
                continue  # the one place allowed to assemble adapters
            leaked = {m for m in _imports(path) if m.startswith("umf.adapters")}
            if leaked:
                violations.append(f"{path.relative_to(APP_ROOT)}: {sorted(leaked)}")
    assert not violations, f"unexpected adapter imports: {violations}"


def test_every_adapter_module_defines_a_class() -> None:
    for path in _iter_python(UMF / "adapters"):
        if path.name in {"__init__.py", "base.py"}:
            continue
        assert "class " in path.read_text(), f"{path.name} defines no adapter"


def test_security_module_has_no_network_imports() -> None:
    """The policy layer must not be able to open a socket itself.

    Resolution is done through the ``socket`` module deliberately, but no HTTP
    client may appear here -- that belongs to the fetcher, behind the guard.
    """
    forbidden = ("httpx", "requests", "aiohttp", "urllib.request")
    for path in _iter_python(UMF / "security"):
        source = path.read_text()
        for token in forbidden:
            assert token not in source, f"{path.name} must not import {token}"


def test_no_placeholder_download_implementation() -> None:
    """§16 forbids simulated progress and hardcoded success.

    Phase 0 has no download path at all, and this test makes sure a stub has
    not been quietly added to satisfy a route.
    """
    forbidden = ("time.sleep(", "random.random()", "# TODO: implement download")
    for path in _iter_python(UMF):
        source = path.read_text()
        for token in forbidden:
            assert token not in source, f"{path.relative_to(APP_ROOT)} contains {token!r}"


def test_adapters_declare_an_honest_capability_profile() -> None:
    """Every adapter must declare capabilities; there is no default that lies."""
    from umf.core.registry import registry

    for adapter in registry.adapters:
        capabilities = adapter.capabilities
        assert isinstance(capabilities.notes, tuple)
        if not capabilities.authorized_download:
            assert capabilities.reasons.get("authorized_download"), (
                f"{adapter.slug} refuses download without a reason code"
            )
