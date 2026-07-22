from __future__ import annotations

import re
import unittest
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "apoapsis" / "ui" / "static"
APP_JS = STATIC_DIR / "app.js"
INDEX_HTML = STATIC_DIR / "index.html"
STYLES_CSS = STATIC_DIR / "styles.css"

# Words/phrases the product-design handoff explicitly asks the interface to
# avoid: sales copy, hype, anthropomorphism, dramatic AI language, and
# unsupported confidence (docs/product-design-handoff.md's "Content style"
# section; D5c website-audit pass).
_BANNED_PHRASES = (
    "seamless",
    "effortless",
    "revolutionary",
    "cutting-edge",
    "cutting edge",
    "supercharge",
    "game-changing",
    "game changing",
    "unleash",
    "empower your",
    "next-gen",
    "state-of-the-art",
    "world-class",
    "blazing fast",
    "delightful",
    "magic",
    "amazing",
    "awesome",
    "congratulations",
    "thinking...",
    "i'm thinking",
    "let me think",
    "our ai",
    "powered by ai",
)


class UiCopyStyleTests(unittest.TestCase):
    """Static scan for hype/anthropomorphic language across every shipped
    UI asset. Mirrors docs/product-design-handoff.md's calm, specific
    "Content style" guidance (e.g. "Verification failed" rather than
    "Something went wrong")."""

    def test_no_banned_hype_or_anthropomorphic_language(self) -> None:
        for path in (APP_JS, INDEX_HTML, STYLES_CSS):
            lowered = path.read_text(encoding="utf-8").lower()
            for phrase in _BANNED_PHRASES:
                self.assertNotIn(
                    phrase,
                    lowered,
                    f"{path.name} contains banned hype/anthropomorphic phrase {phrase!r}",
                )

    def test_home_navigation_label_is_calm_and_literal(self) -> None:
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn('href="#/home"><span class="nav-dot"></span><span>Home</span>', source)
        self.assertNotIn(
            'href="#/home"><span class="nav-dot"></span><span>Projects</span>',
            source,
            "personal-first, single-repository product should not use a "
            "multi-project navigation label",
        )

    def test_home_hero_avoids_slogan_style_copy(self) -> None:
        source = APP_JS.read_text(encoding="utf-8")
        self.assertNotIn(
            "without the guesswork",
            source.lower(),
            "the home hero heading should read as a direct status statement, "
            "not a marketing tagline",
        )

    def test_home_explains_the_two_supported_starting_paths(self) -> None:
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn("What would you like to do?", source)
        self.assertIn("Quick change", source)
        self.assertIn("Plan a larger change", source)
        self.assertIn("one Git project", source)
        self.assertIn("OPEN_APOAPSIS.cmd", source)
        self.assertIn(r'C:\\path\\to\\project', source)

    def test_guided_workflows_explain_recovery_and_planning_research(self) -> None:
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn("Continue with ChatGPT or Claude", source)
        self.assertIn("Optional planning research", source)
        self.assertIn("Skip research and continue", source)
        self.assertIn("Waiting for dependencies", source)
        self.assertIn("Packaging the next slice will checkpoint and inherit", source)

    def test_plan_validation_and_failed_verification_have_ui_buttons(self) -> None:
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn("Verify plan →", source)
        self.assertIn("Repair and verify →", source)
        self.assertIn(
            'const repairEligible = eligible.includes("local_continuation");',
            source,
        )
        self.assertIn("continue the approved implementation", source)
        self.assertIn('authorize_local_stage: "Run locally"', source)
        self.assertIn('authorize_frontier_run: "Run with frontier"', source)
        self.assertIn("Routing stopped before any coding agent ran", source)
        self.assertIn("Local run incomplete", source)
        self.assertIn("Prepare finished project", source)
        self.assertIn("Download finished project", source)
        self.assertIn("Whole-project frontier review handoff", source)
        self.assertNotIn("apoapsis plan validate", source)
        self.assertIn("Repair incomplete", source)
        self.assertIn("Required verification has not passed", source)
        self.assertIn("Verification still failing", source)
        self.assertIn('verification_only_retry: "Verify current changes"', source)
        self.assertIn('taskDetail.task?.state === "COMPLETE"', source)
        self.assertIn("/report`", source)

    def test_changes_view_never_asserts_a_completion_policy_before_a_report_exists(
        self,
    ) -> None:
        """Regression guard for a real bug found in D5c live browser QA:
        ``changesView`` used to fall back to the literal string
        ``"baseline"`` whenever no report existed yet, then rendered
        "The baseline completion policy does not gate on acceptance
        coverage" -- an unsupported, specific claim about a policy that
        might actually be ``strict`` for the project (this is exactly the
        false-confidence pattern the product-design handoff forbids)."""

        source = APP_JS.read_text(encoding="utf-8")
        self.assertNotIn(
            'report?.completion_policy || "baseline"',
            source,
            "changesView must not assume 'baseline' when no report exists yet",
        )
        self.assertIn(
            "No final report exists yet, so acceptance coverage has not been computed.",
            source,
        )

    def test_a_verification_pass_is_never_called_complete_without_review(self) -> None:
        """The STRICT completion policy (ADR 0015/0016/0017) never reaches
        COMPLETE while acceptance coverage still requires Human Review --
        the UI must not describe that state as complete anywhere."""

        source = APP_JS.read_text(encoding="utf-8")
        # "complete" only ever appears as a literal status/outcome value or
        # inside otherwise-covered strings; it must never be paired with
        # "Human Review" as if both were simultaneously true.
        self.assertNotIn("Complete, pending human review", source)
        self.assertNotIn("Completed -- awaiting review", source)


class UiDocumentTitleTests(unittest.TestCase):
    """The browser tab title must change with navigation (D5c polish pass)
    -- previously it never did, since nothing ever assigned
    ``document.title``."""

    def setUp(self) -> None:
        self.source = APP_JS.read_text(encoding="utf-8")

    def test_update_document_title_function_exists(self) -> None:
        self.assertIn("function updateDocumentTitle()", self.source)
        self.assertIn("document.title = label", self.source)

    def test_render_calls_update_document_title(self) -> None:
        render_index = self.source.index("function render() {")
        following = self.source[render_index : render_index + 200]
        self.assertIn("updateDocumentTitle()", following)

    def test_every_top_level_route_has_a_title_mapping(self) -> None:
        allowed = re.search(
            r'const allowed = new Set\(\[(.*?)\]\);', self.source, re.DOTALL
        )
        self.assertIsNotNone(allowed)
        route_names = re.findall(r'"(\w+)"', allowed.group(1))
        route_titles = re.search(
            r"const ROUTE_TITLES = \{(.*?)\};", self.source, re.DOTALL
        )
        self.assertIsNotNone(route_titles)
        for name in route_names:
            self.assertIn(
                name,
                route_titles.group(1),
                f"route {name!r} has no entry in ROUTE_TITLES",
            )
        self.assertIn('new: "Quick change"', self.source)
        self.assertIn('reviews: "Needs attention"', self.source)
        self.assertIn('discover: "Plan a larger change"', self.source)
        self.assertIn('return ROUTE_TITLES[store.route.name] || titleCase(store.route.name);', self.source)


class UiAccessibilityInvariantTests(unittest.TestCase):
    """Deterministic, browser-independent checks for accessibility
    invariants called for by the product-design handoff and D5c polish
    pass: visible focus, reduced-motion respect, and a real page title/
    language attribute."""

    def test_index_declares_language_and_viewport(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn('lang="en"', html)
        self.assertIn('name="viewport"', html)
        self.assertIn("<title>", html)

    def test_focus_visible_styles_exist_for_interactive_elements(self) -> None:
        css = STYLES_CSS.read_text(encoding="utf-8")
        self.assertIn(":focus-visible", css)
        self.assertIn("outline", css)

    def test_reduced_motion_preference_is_respected(self) -> None:
        css = STYLES_CSS.read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion", css)
        # Transitions must be gated behind the no-preference query, never
        # applied unconditionally -- otherwise a reduced-motion user still
        # gets the animated transitions.
        motion_block = re.search(
            r"@media \(prefers-reduced-motion: no-preference\) \{(.*?)\n\}",
            css,
            re.DOTALL,
        )
        self.assertIsNotNone(motion_block)
        self.assertIn("transition", motion_block.group(1))

    def test_detail_routes_clear_stale_state_before_refetching(self) -> None:
        """Regression guard for a real bug found in D5c live browser QA:
        navigating to a task/plan/review/discovery-session id that fails to
        load (e.g. a stale link to a deleted or nonexistent record) used to
        leave the *previous* record's full detail rendered underneath a
        small error banner -- a user could easily miss the banner and act
        on the wrong task's specification. Each detail store field must now
        be nulled out before the new fetch starts, so a failed lookup shows
        the existing loading/empty state rather than stale, wrong content."""

        source = APP_JS.read_text(encoding="utf-8")
        for pattern in (
            r"store\.task = null;\s*\n\s*store\.busy = true;\s*\n\s*render\(\);\s*\n\s*store\.executionOperation = null;",
            r"store\.plan = null;\s*\n\s*store\.busy = true;\s*\n\s*render\(\);\s*\n\s*store\.plan = await api",
            r"store\.planSlice = null;\s*\n\s*store\.busy = true;",
            r"store\.review = null;\s*\n\s*store\.busy = true;\s*\n\s*render\(\);\s*\n\s*store\.reviewOperation = null;",
            r"store\.discoverSession = null;\s*\n\s*store\.busy = true;\s*\n\s*render\(\);\s*\n\s*store\.discoverOperation = null;",
        ):
            self.assertRegex(source, pattern)

    def test_destructive_review_actions_still_require_two_step_confirmation(
        self,
    ) -> None:
        """Regression guard: the review confirm-panel mechanism
        (``store.reviewConfirm`` -> ``reviewConfirmPanel``) must remain
        wired, since this is the only thing standing between one click and
        an irreversible worktree rollback."""

        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn("store.reviewConfirm = { action:", source)
        self.assertIn("function reviewConfirmPanel(", source)
        self.assertIn("This cannot be undone.", source)


if __name__ == "__main__":
    unittest.main()
