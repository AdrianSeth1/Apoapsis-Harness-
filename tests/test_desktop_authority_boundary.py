"""Static, source-scanning regression coverage for the desktop authority
boundary (ADR 0050-0054, Phase 7).

`HANDOFF.md`'s authority boundary says models never receive direct
filesystem, shell, Git, network, or workflow authority, and ADR 0050-0053
repeatedly emphasize that the *browser-facing* loopback surface
(`apoapsis.ui.server`/`apoapsis.ui.application`) must never gain
filesystem-adjacent capability either -- only the desktop layer
(`apoapsis.desktop`), reachable only through the privileged, non-browser
IPC channel (ADR 0053), may hold it.

These tests do not exercise any runtime behavior; they scan source text,
the same way `tests/test_launcher.py` and `tests/test_app_js_regression.py`
already do for their own invariants, so the check works without a GUI,
without Docker, and without a live model -- and so a future change that
quietly adds an `import apoapsis.desktop` to a model-facing or
browser-facing module fails a test immediately, rather than only being
caught in review.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src" / "apoapsis"

# Every package that can, directly or transitively, put content in front of
# a model or accept a model's typed action. If any of these ever imports
# `apoapsis.desktop`, a model could end up with a transitive path to native
# filesystem capability -- exactly what the authority boundary forbids.
_MODEL_FACING_PACKAGES = (
    "agent",
    "architect",
    "discovery",
    "research",
    "specification",
    "manual_frontier",
    "intake",
    "review",
    "execution",
    "workflow",
    "patches",
    "verification",
    "context",
    "models",
)

_DESKTOP_IMPORT_PATTERN = re.compile(
    r"^\s*(from\s+apoapsis\.desktop|import\s+apoapsis\.desktop)\b", re.MULTILINE
)


def _python_files_under(*relative_dirs: str) -> list[Path]:
    files: list[Path] = []
    for relative_dir in relative_dirs:
        package_root = _SRC_ROOT / relative_dir
        if package_root.is_dir():
            files.extend(sorted(package_root.rglob("*.py")))
    return files


class ModelFacingCodeNeverImportsDesktopTests(unittest.TestCase):
    """No model-facing package may import `apoapsis.desktop` -- a model
    must never gain a transitive path to project selection, import, or
    reference-project capability."""

    def test_no_model_facing_module_imports_desktop_package(self) -> None:
        offending: list[str] = []
        for path in _python_files_under(*_MODEL_FACING_PACKAGES):
            text = path.read_text(encoding="utf-8")
            if _DESKTOP_IMPORT_PATTERN.search(text):
                offending.append(str(path.relative_to(_REPO_ROOT)))
        self.assertEqual(
            offending,
            [],
            "model-facing modules must never import apoapsis.desktop: "
            f"{offending}",
        )

    def test_model_facing_packages_exist_and_are_not_accidentally_empty(self) -> None:
        # Guards the test above against silently passing merely because a
        # package name was mistyped and `_python_files_under` found nothing.
        found_any = any(
            (_SRC_ROOT / package).is_dir() for package in _MODEL_FACING_PACKAGES
        )
        self.assertTrue(found_any, "expected at least one real model-facing package")
        total_files = len(_python_files_under(*_MODEL_FACING_PACKAGES))
        self.assertGreater(
            total_files, 10, "expected substantially more than 10 source files "
            "across the model-facing packages -- the scan may be misconfigured"
        )


class BrowserFacingUiNeverImportsDesktopTests(unittest.TestCase):
    """The existing browser-facing loopback surface
    (`apoapsis.ui.server`/`apoapsis.ui.application`) must remain completely
    decoupled from `apoapsis.desktop` -- adding an HTTP route there that
    forwards to a desktop-layer method would let ordinary browser
    JavaScript request filesystem-adjacent operations, exactly the
    regression ADR 0051/0052/0053 all repeat must not happen. (The reverse
    dependency -- `apoapsis.desktop.home_service` importing
    `apoapsis.ui.application.ApoapsisUIService` to reuse its existing,
    already-safe `doctor()`/`overview()` reads -- is fine and expected;
    this test only forbids the direction that would widen browser
    authority.)"""

    def test_ui_server_never_imports_desktop_package(self) -> None:
        path = _SRC_ROOT / "ui" / "server.py"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIsNone(
            _DESKTOP_IMPORT_PATTERN.search(text),
            "apoapsis.ui.server must never import apoapsis.desktop",
        )
        self.assertNotIn(
            "/desktop/",
            text,
            "apoapsis.ui.server must not proxy or reference any /desktop/ route",
        )

    def test_ui_application_never_imports_desktop_package(self) -> None:
        path = _SRC_ROOT / "ui" / "application.py"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIsNone(
            _DESKTOP_IMPORT_PATTERN.search(text),
            "apoapsis.ui.application must never import apoapsis.desktop",
        )

    def test_browser_static_assets_never_reference_desktop_routes(self) -> None:
        # `app.js` is the only browser-executed code in this codebase; it
        # must never be given a path to the privileged channel to call.
        app_js = _SRC_ROOT / "ui" / "static" / "app.js"
        self.assertTrue(app_js.is_file())
        text = app_js.read_text(encoding="utf-8")
        self.assertNotIn("/desktop/", text)
        self.assertNotIn("X-Apoapsis-Desktop-Token", text)


class DesktopIpcServerRouteAllowlistTests(unittest.TestCase):
    """The privileged channel itself (ADR 0053) must only ever expose the
    fourteen named typed operations ADR 0050 Phase 6 specified -- not an
    arbitrary-path filesystem endpoint. This is a whitebox structural check
    on the route table, independent of `tests/test_desktop_ipc_server.py`'s
    live-HTTP behavioral coverage."""

    def test_ipc_server_route_table_matches_the_documented_allowlist(self) -> None:
        from apoapsis.desktop.ipc_server import _ROUTES

        expected = {
            "validate_project",
            "select_project",
            "initialize_project",
            "list_recent_projects",
            "forget_recent_project",
            "close_session",
            "preview_import",
            "approve_import",
            "execute_import",
            "attach_reference_project",
            "select_reference_evidence",
            "list_reference_evidence",
            "detach_reference_project",
            "home_summary",
        }
        self.assertEqual(set(_ROUTES), expected)

    def test_every_route_has_exactly_one_handler(self) -> None:
        from apoapsis.desktop.ipc_server import _ROUTES, DesktopIPCRequestHandler

        for route in _ROUTES:
            self.assertTrue(
                hasattr(DesktopIPCRequestHandler, f"_handle_{route}"),
                f"route {route!r} has no corresponding _handle_{route} method",
            )


if __name__ == "__main__":
    unittest.main()
