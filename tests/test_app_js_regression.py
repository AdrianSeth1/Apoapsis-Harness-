from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "apoapsis"
    / "ui"
    / "static"
    / "app.js"
)

_TOP_LEVEL_FUNCTION = re.compile(
    r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.MULTILINE
)


def _top_level_function_names(source: str) -> list[str]:
    return _TOP_LEVEL_FUNCTION.findall(source)


class AppJsStaticRegressionTests(unittest.TestCase):
    """Deterministic, Node-independent static checks -- always run,
    regardless of whether Node is installed on the machine running the
    suite. These specifically target the exact bug class a previous
    live-browser pass found and the deterministic suite could not:
    ``app.js`` once had two functions both named ``reviewView``, and
    JavaScript's silent last-declaration-wins semantics meant the
    top-level review route always executed the wrong one."""

    def setUp(self) -> None:
        self.source = APP_JS.read_text(encoding="utf-8")

    def test_no_duplicate_top_level_function_declarations(self) -> None:
        names = _top_level_function_names(self.source)
        seen: dict[str, int] = {}
        duplicates: list[str] = []
        for name in names:
            seen[name] = seen.get(name, 0) + 1
            if seen[name] == 2:
                duplicates.append(name)
        self.assertEqual(
            duplicates,
            [],
            f"app.js declares the following function name(s) more than "
            f"once at the top level -- JavaScript silently keeps only the "
            f"last declaration, so every earlier caller of that name "
            f"executes the wrong body: {duplicates}",
        )

    def test_route_dispatch_targets_exist_and_are_unique(self) -> None:
        """Cross-references ``render()``'s route dispatch table against
        the set of declared function names -- a route wired to a typo'd
        or removed function name would be a ``ReferenceError`` at runtime,
        caught here without needing a live browser."""

        dispatch = re.findall(
            r'store\.route\.name === "(\w+)"\)\s*view = (\w+)\(\)',
            self.source,
        )
        self.assertGreater(
            len(dispatch), 0, "could not find render()'s route dispatch table"
        )
        fallback = re.search(r"else view = (\w+)\(\);", self.source)
        self.assertIsNotNone(fallback, "could not find render()'s fallback view")
        declared = set(_top_level_function_names(self.source))
        for route_name, view_function in dispatch:
            self.assertIn(
                view_function,
                declared,
                f"route {route_name!r} dispatches to {view_function}(), "
                "which is not declared anywhere in app.js",
            )
        assert fallback is not None
        self.assertIn(fallback.group(1), declared)


@unittest.skipUnless(shutil.which("node"), "Node.js is not available on this machine")
class AppJsNodeSmokeTests(unittest.TestCase):
    """Real-JavaScript-engine smoke coverage -- skipped with a clear
    reason (not silently passed) when Node is not installed. Does not
    add a production runtime dependency: Node is only ever invoked from
    the test suite, never from ``apoapsis`` itself."""

    def test_syntax_is_valid(self) -> None:
        result = subprocess.run(
            ["node", "--check", str(APP_JS)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            result.returncode, 0, f"node --check failed:\n{result.stderr}"
        )

    def test_boots_without_a_session_token_and_shows_the_boot_screen(self) -> None:
        """Runs the real ``app.js`` in a minimal, hand-rolled DOM stub
        (no npm dependency) via Node's built-in ``vm`` module, letting
        ``boot()`` execute its real no-session-token fast path. Exercises
        every top-level statement (route parsing setup, ``store``
        construction, every function declaration) and one real ``render()``
        call without needing to fake a live API."""

        harness = f"""
        const vm = require('vm');
        const fs = require('fs');
        const source = fs.readFileSync({str(APP_JS)!r}, 'utf-8');

        let renderedHtml = null;
        const rootElement = {{
          set innerHTML(value) {{ renderedHtml = value; }},
          get innerHTML() {{ return renderedHtml; }},
          addEventListener() {{}},
        }};
        const storageBacking = {{}};
        const sessionStorageStub = {{
          getItem: (key) => (key in storageBacking ? storageBacking[key] : null),
          setItem: (key, value) => {{ storageBacking[key] = String(value); }},
          removeItem: (key) => {{ delete storageBacking[key]; }},
        }};
        const documentStub = {{
          getElementById: (id) => (id === 'app' ? rootElement : null),
        }};
        const windowStub = {{
          location: {{ search: '', hash: '', pathname: '/' }},
          history: {{ replaceState: () => {{}} }},
          sessionStorage: sessionStorageStub,
          crypto: globalThis.crypto,
          addEventListener: () => {{}},
        }};
        const sandbox = {{
          document: documentStub,
          window: windowStub,
          URLSearchParams: URLSearchParams,
          console: console,
          setTimeout: setTimeout,
          clearTimeout: clearTimeout,
          fetch: () => Promise.reject(new Error('network is not stubbed')),
        }};
        vm.createContext(sandbox);
        new vm.Script(source, {{ filename: 'app.js' }}).runInContext(sandbox);

        setTimeout(() => {{
          if (renderedHtml === null) {{
            console.error('SMOKE_FAIL: render() never set root.innerHTML');
            process.exit(1);
          }}
          if (!renderedHtml.includes('SESSION REQUIRED')) {{
            console.error('SMOKE_FAIL: unexpected boot screen: ' + renderedHtml);
            process.exit(1);
          }}
          console.log('SMOKE_OK');
        }}, 50);
        """
        result = subprocess.run(
            ["node", "-e", harness],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"Node boot smoke failed.\nstdout: {result.stdout}\nstderr: {result.stderr}",
        )
        self.assertIn("SMOKE_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
