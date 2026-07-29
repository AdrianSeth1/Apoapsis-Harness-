"""A real, configurable check for dependency-free browser products (ADR 0069).

`apoapsis.verification.contract` reports that a contract is weak. This module
exists so an owner has something stronger to configure instead.

The motivating failure is precise. On TASK-33E0EB6476C4 the model produced
`index.html`, `styles.css`, and `app.js`. Seven owner-written checks confirmed
that each file contained the expected fragments: the three mode labels, a
`localStorage` call, a `setInterval`, a circular-progress technique, media
queries, accessibility attributes. Every check passed. The application was
inert, because `app.js` attached listeners to `mode-focus`, `mode-short-break`,
`mode-long-break`, and `status-live`, and `index.html` defined none of those
ids; `styles.css` styled `.progress-ring`, `.btn`, `.btn-primary`, and
`.btn-secondary`, and the markup carried none of those classes. The browser
console said `TypeError: Cannot read properties of null (reading
'addEventListener')`. The test suite said nothing, because it had asked whether
the fragments existed, never whether they referred to one another.

That is the gap this closes: not "is the code good", which no static tool can
answer, but "do these three files actually describe the same application".
It is a cross-reference check, and it is deterministic, dependency-free, and
offline -- the same properties the products it verifies are usually required
to have.

Honest limits, stated up front:

* This does not execute anything. A product can pass every check here and
  still be wrong, because correct wiring is necessary and not sufficient.
  `run_behavioral_probe` exists for the part that needs a real browser, and
  it fails loudly rather than passing when no probe provider is configured --
  an unproven behavioral claim must never read as a proven one.
* The JavaScript and CSS analysis is lexical, not a parser. Selectors it
  cannot analyze with confidence are counted and reported as unanalyzed
  rather than silently assumed fine.
* Elements a product deliberately creates only at runtime are legitimate;
  `optional_elements` is the way to say so, and saying so is a visible,
  owner-made decision rather than a silent heuristic.

ADR 0073 added two things to the above.

First, "no external resources" and "no network calls" are no longer the same
rule. They never were the same rule; treating them as one cost Crisis Atlas
its product. That plan required the dashboard to talk to a local HTTP API,
and its configured check flagged `fetch` unconditionally, so the only way to
make the check green was to delete the integration -- which is what happened.
`forbid_external_resources` now means what its name says: no third-party
origin. `forbid_runtime_network_apis` is a separate, separately named option
carrying the old blanket meaning for owners who genuinely want it.

Second, this module now counts its own evidence. A check that passes having
cross-referenced nothing is a valid static result and a nearly worthless one,
and it should not look identical to one that examined forty element
references. `WebCheckEvidence` reports what was examined and what could not
be reached, and `ceiling_statement()` says in one sentence what the result
can and cannot support.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path

from pydantic import Field

from apoapsis.specification.schema import StrictModel


class WebProductFindingSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class WebProductFindingCode(StrEnum):
    NO_ENTRY_DOCUMENT = "no_entry_document"
    UNRESOLVED_ELEMENT_ID = "unresolved_element_id"
    UNRESOLVED_ELEMENT_CLASS = "unresolved_element_class"
    DUPLICATE_ELEMENT_ID = "duplicate_element_id"
    DUPLICATE_FUNCTION_DECLARATION = "duplicate_function_declaration"
    DEAD_STYLE_RULE = "dead_style_rule"
    MISSING_LOCAL_ASSET = "missing_local_asset"
    EXTERNAL_RESOURCE_REFERENCE = "external_resource_reference"
    # ADR 0073 split `network_call` into three codes that say *what* was
    # found, so an owner reading a finding can tell a CDN dependency from
    # the product talking to its own backend. `NETWORK_CALL` survives with
    # a narrower meaning: a runtime request API was used at all, reported
    # only under the separate `forbid_runtime_network_apis` policy.
    NETWORK_CALL = "network_call"
    SAME_ORIGIN_REQUEST = "same_origin_request"
    CROSS_ORIGIN_REQUEST = "cross_origin_request"
    UNPROVEN_REQUEST_TARGET = "unproven_request_target"
    UNANALYZED_SELECTOR = "unanalyzed_selector"
    NEGLIGIBLE_EVIDENCE = "negligible_evidence"


class RequestTargetKind(StrEnum):
    """Where a runtime request or document asset actually points.

    The distinction ADR 0073 exists to draw. ``SAME_ORIGIN`` is the product
    talking to whatever server is serving it -- a relative or root-relative
    path, which follows the product wherever it is deployed.
    ``ABSOLUTE_LOOPBACK`` is still a hard-coded origin: it happens to be
    local today, and it breaks the moment the product is served from a
    different port or host, so it is grouped with cross-origin for policy
    purposes and reported with its own wording.
    """

    SAME_ORIGIN = "same_origin"
    ABSOLUTE_LOOPBACK = "absolute_loopback"
    CROSS_ORIGIN = "cross_origin"
    WEBSOCKET = "websocket"
    OTHER_SCHEME = "other_scheme"
    UNPROVEN = "unproven"


# Everything that is not the product's own origin. Grouped here rather than
# tested at each call site so the external-resource policy has exactly one
# definition.
_NON_SAME_ORIGIN_KINDS: frozenset[RequestTargetKind] = frozenset(
    {
        RequestTargetKind.ABSOLUTE_LOOPBACK,
        RequestTargetKind.CROSS_ORIGIN,
        RequestTargetKind.WEBSOCKET,
        RequestTargetKind.OTHER_SCHEME,
    }
)


class WebProductFinding(StrictModel):
    code: WebProductFindingCode
    severity: WebProductFindingSeverity
    path: str
    symbol: str | None = None
    detail: str = Field(min_length=1)
    remediation: str = Field(min_length=1)
    target_kind: RequestTargetKind | None = Field(
        default=None,
        description=(
            "For request/resource findings, where the target resolved to. "
            "Optional so that findings about markup and styles, which have "
            "no target, stay unchanged."
        ),
    )


class WebCheckEvidence(StrictModel):
    """How much this run actually looked at, and what it could not reach.

    Added by ADR 0073. A passing `WebProductReport` with every count at
    zero is a valid static result and a nearly worthless one: the badge
    repair on Crisis Atlas replaced computed classes with data attributes
    and the check then passed having cross-checked no element references at
    all. Counting the evidence makes that visible instead of leaving a
    green result looking like a UI behavior test.
    """

    schema_version: str = "1.0"
    element_references_checked: int = Field(default=0, ge=0)
    css_selectors_checked: int = Field(default=0, ge=0)
    local_assets_resolved: int = Field(default=0, ge=0)
    same_origin_api_references: int = Field(default=0, ge=0)
    cross_origin_api_references: int = Field(default=0, ge=0)
    dynamic_references_unproven: int = Field(default=0, ge=0)
    end_to_end_behavior_measured: bool = False

    @property
    def is_negligible(self) -> bool:
        """True when the run cross-checked nothing at all.

        Deliberately does not count `local_assets_resolved`: a product can
        resolve its own `<script src>` and still have had zero of its
        markup, styles, or requests examined.
        """

        return (
            self.element_references_checked == 0
            and self.css_selectors_checked == 0
            and self.same_origin_api_references == 0
            and self.cross_origin_api_references == 0
        )

    def ceiling_statement(self) -> str:
        """One sentence naming what this result can and cannot support."""

        measured = (
            "end-to-end browser behavior was measured"
            if self.end_to_end_behavior_measured
            else "end-to-end browser behavior was NOT measured"
        )
        if self.is_negligible:
            return (
                "This run cross-checked no element references, no CSS "
                "selectors, and no API references: it establishes only that "
                "the files parse and that referenced local assets exist. "
                f"{measured}. Do not read this pass as evidence of "
                "persistence, browser/API integration, or interaction "
                "behavior."
            )
        return (
            f"Static cross-reference only: {self.element_references_checked} "
            f"element reference(s), {self.css_selectors_checked} CSS "
            f"selector(s), {self.local_assets_resolved} local asset(s), and "
            f"{self.same_origin_api_references + self.cross_origin_api_references} "
            f"API reference(s) were examined, with "
            f"{self.dynamic_references_unproven} reference(s) left unproven "
            f"because their target is computed at runtime. {measured}; "
            "nothing here executes the product."
        )


class WebProductReport(StrictModel):
    schema_version: str = "1.0"
    root: str
    documents: list[str] = Field(default_factory=list)
    scripts: list[str] = Field(default_factory=list)
    stylesheets: list[str] = Field(default_factory=list)
    element_ids: list[str] = Field(default_factory=list)
    element_classes: list[str] = Field(default_factory=list)
    checked_references: int = Field(default=0, ge=0)
    unanalyzed_selectors: int = Field(default=0, ge=0)
    evidence: WebCheckEvidence = Field(default_factory=WebCheckEvidence)
    findings: list[WebProductFinding] = Field(default_factory=list)
    behavioral_probe: str = "not requested"

    @property
    def errors(self) -> list[WebProductFinding]:
        return [
            item
            for item in self.findings
            if item.severity == WebProductFindingSeverity.ERROR
        ]

    @property
    def warnings(self) -> list[WebProductFinding]:
        return [
            item
            for item in self.findings
            if item.severity == WebProductFindingSeverity.WARNING
        ]

    def passed(self, *, treat_warnings_as_errors: bool = False) -> bool:
        if self.errors:
            return False
        return not (treat_warnings_as_errors and self.warnings)


# -- HTML -------------------------------------------------------------------


class _DocumentIndex(HTMLParser):
    """Everything the cross-reference needs from one HTML document.

    Uses the standard library's tolerant parser deliberately: the documents
    under inspection are frequently mid-repair, and a strict parser that
    refused to read a malformed file would hide exactly the products most
    likely to be broken.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.classes: set[str] = set()
        self.tags: set[str] = set()
        self.assets: list[tuple[str, str]] = []
        self.inline_scripts: list[str] = []
        self.inline_styles: list[str] = []
        self._capturing: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag.lower())
        values = {name.lower(): (value or "") for name, value in attrs}
        if "id" in values and values["id"]:
            self.ids.append(values["id"])
        for name in values.get("class", "").split():
            self.classes.add(name)
        for attribute in ("src", "href"):
            target = values.get(attribute, "").strip()
            if target and not target.startswith(("#", "data:", "javascript:", "mailto:")):
                self.assets.append((attribute, target))
        if tag.lower() in {"script", "style"} and "src" not in values:
            self._capturing = tag.lower()

    def handle_endtag(self, tag: str) -> None:
        if self._capturing == tag.lower():
            self._capturing = None

    def handle_data(self, data: str) -> None:
        if self._capturing == "script":
            self.inline_scripts.append(data)
        elif self._capturing == "style":
            self.inline_styles.append(data)


# -- JavaScript -------------------------------------------------------------

# `(?:(?!\1).)` rather than a character class: a selector is routinely
# quoted with one quote character and contains another, as in
# `querySelector('input[type="radio"]')`. A naive class stops at the inner
# quote and the whole call silently fails to match -- which would mean the
# selectors most likely to be complex are also the ones never counted as
# unanalyzed, the worst possible combination.
_GET_ELEMENT_BY_ID = re.compile(
    r"""getElementById\s*\(\s*(['"`])(?P<name>(?:(?!\1).)+)\1"""
)
_GET_BY_CLASS = re.compile(
    r"""getElementsByClassName\s*\(\s*(['"`])(?P<name>(?:(?!\1).)+)\1"""
)
_QUERY_SELECTOR = re.compile(
    r"""querySelector(?:All)?\s*\(\s*(['"`])(?P<selector>(?:(?!\1).)+)\1"""
)
_CLASS_LIST_CALL = re.compile(
    r"""classList\s*\.\s*(?:add|remove|toggle|contains|replace)\s*\((?P<args>[^)]*)\)"""
)
_STRING_LITERAL = re.compile(r"""(['"`])([^'"`]*)\1""")
_MARKUP_ID_ATTRIBUTE = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""")
_MARKUP_CLASS_ATTRIBUTE = re.compile(r"""\bclass\s*=\s*["']([^"']+)["']""")
_CLASSNAME_ASSIGNMENT = re.compile(
    r"""className\s*=\s*(['"`])([^'"`]*)\1"""
)
_SET_ATTRIBUTE_ID = re.compile(
    r"""setAttribute\s*\(\s*['"]id['"]\s*,\s*['"]([^'"]+)['"]"""
)
_TOP_LEVEL_FUNCTION = re.compile(
    r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.MULTILINE
)
# `class="pill ${statusClass(x)}"` carries one real name and one computed
# fragment. Removing the interpolation first keeps the real name, which a
# blanket "discard anything containing `$`" rule would have thrown away
# along with it.
_INTERPOLATION = re.compile(r"\$\{[^{}]*\}")
_INTERPOLATED = re.compile(r"[${}`]")
_WORD = re.compile(r"[A-Za-z_][-\w]*")


def _clean_names(value: str) -> list[str]:
    stripped = _INTERPOLATION.sub(" ", value)
    return [name for name in stripped.split() if name and not _INTERPOLATED.search(name)]


# -- runtime request targets (ADR 0073) -------------------------------------

# Any use of a runtime request API, target irrelevant. This is what the
# separately named `forbid_runtime_network_apis` policy acts on, and it is
# deliberately the same shape as the old blanket rule so that policy's
# behaviour is exactly the pre-0073 one.
_NETWORK_API = re.compile(
    r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon)\b"
)

# Each pattern captures the request target when, and only when, it is a
# static string literal. A missing `url` group means the argument was a
# variable, a concatenation, or a call -- which this module reports as
# unproven rather than guessing at. `(?:(?!\1).)` for the same reason it is
# used for selectors above: a URL literal routinely contains the other
# quote character.
_LITERAL = r"""(?:(['"`])(?P<url>(?:(?!\1).)*)\1)?"""
_REQUEST_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fetch", re.compile(r"\bfetch\s*\(\s*" + _LITERAL)),
    ("WebSocket", re.compile(r"\bnew\s+WebSocket\s*\(\s*" + _LITERAL)),
    ("EventSource", re.compile(r"\bnew\s+EventSource\s*\(\s*" + _LITERAL)),
    ("navigator.sendBeacon", re.compile(r"\bsendBeacon\s*\(\s*" + _LITERAL)),
    # `xhr.open(method, url)`: the method argument is skipped so the URL is
    # the one captured. An `open` call whose method is not a literal is not
    # matched at all, which the bare-`XMLHttpRequest` API detection above
    # still catches for the blanket policy.
    (
        "XMLHttpRequest.open",
        re.compile(r"""\.open\s*\(\s*['"][A-Za-z]+['"]\s*,\s*""" + _LITERAL),
    ),
)

_SCHEME = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*):")
_LOOPBACK_HOSTS: frozenset[str] = frozenset(
    {"localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1"}
)


def classify_request_target(target: str) -> RequestTargetKind:
    """Where a request or asset URL points, from the URL text alone.

    The whole point of ADR 0073 lives in this function, so its rules are
    stated rather than implied:

    * A root-relative path (`/incidents`) or a relative one (`incidents`,
      `./api/x`, `../api/x`) is same-origin. It resolves against whatever
      server delivered the document, which is the product's own backend.
    * A protocol-relative URL (`//cdn.example.com/x`) is cross-origin. It
      names a host.
    * An absolute `http`/`https` URL is cross-origin, and separately
      reported as `ABSOLUTE_LOOPBACK` when its host is a loopback name --
      still a hard-coded origin, still broken by a port change, but worth
      distinct wording because the owner's intent was clearly local.
    * `ws`/`wss` is a WebSocket, which is its own kind because the
      strictest realistic policy about it differs from the one about
      fetching a script.
    * Anything else with a scheme (`file:`, `chrome-extension:`) is
      `OTHER_SCHEME` and is not same-origin.
    * A literal whose origin is decided by interpolation (`${base}/x`) is
      `UNPROVEN`. Interpolation *after* the origin is already fixed
      (`/incidents/${id}`) does not make it unprovable -- the leading `/`
      settled the question.
    """

    text = target.strip()
    if not text:
        return RequestTargetKind.UNPROVEN
    if text.startswith("//"):
        return RequestTargetKind.CROSS_ORIGIN
    if text.startswith("/"):
        # Root-relative, and the leading slash fixes the origin before any
        # later `${...}` can affect it.
        return RequestTargetKind.SAME_ORIGIN
    scheme_match = _SCHEME.match(text)
    if scheme_match is not None:
        scheme = scheme_match.group("scheme").lower()
        if scheme in {"ws", "wss"}:
            return RequestTargetKind.WEBSOCKET
        if scheme in {"http", "https"}:
            remainder = text[scheme_match.end() :].lstrip("/")
            authority = remainder.split("/", 1)[0].split("?", 1)[0]
            host = authority.rsplit("@", 1)[-1]
            if host.startswith("["):
                host = host.split("]", 1)[0] + "]"
            else:
                host = host.split(":", 1)[0]
            if host.lower() in _LOOPBACK_HOSTS:
                return RequestTargetKind.ABSOLUTE_LOOPBACK
            return RequestTargetKind.CROSS_ORIGIN
        return RequestTargetKind.OTHER_SCHEME
    if "${" in text:
        # Interpolation before anything fixed the origin: the target could
        # be same-origin or a third-party host and this module cannot tell.
        return RequestTargetKind.UNPROVEN
    return RequestTargetKind.SAME_ORIGIN


@dataclass(frozen=True)
class RequestReference:
    """One runtime request found in a script.

    ``target`` is ``None`` when the call site gave no static literal at
    all, which is a different and slightly worse situation than a literal
    whose origin depends on interpolation; both classify as ``UNPROVEN``,
    and the finding text distinguishes them.
    """

    api: str
    target: str | None
    kind: RequestTargetKind

# A selector is analyzed only when it is plainly a list of compound
# selectors: names, ids, classes, and descendant/child combinators. Anything
# with attribute selectors, pseudo-classes, or functional notation is
# reported as unanalyzed rather than guessed at.
_ANALYZABLE_SELECTOR = re.compile(r"^[\w\s.#,>-]+$")
_COMPOUND_PART = re.compile(r"[#.]?[A-Za-z_][-\w]*")


@dataclass
class _ScriptReferences:
    """Internal scratch state, deliberately a plain dataclass: it is
    accumulated mutably during analysis and never serialized or trusted
    across a boundary."""

    ids: set[str] = field(default_factory=set)
    classes: set[str] = field(default_factory=set)
    tags: set[str] = field(default_factory=set)
    unanalyzed: int = 0
    dynamic_ids: set[str] = field(default_factory=set)
    dynamic_classes: set[str] = field(default_factory=set)
    duplicate_functions: list[str] = field(default_factory=list)
    # Which request APIs appear at all, target irrelevant. Retains the
    # pre-0073 meaning and is what the blanket
    # `forbid_runtime_network_apis` policy acts on.
    network_apis: list[str] = field(default_factory=list)
    # Where each request actually points. This is what the external-resource
    # policy acts on.
    requests: list[RequestReference] = field(default_factory=list)


def _split_compound(compound: str) -> list[str]:
    return [part for part in _COMPOUND_PART.findall(compound) if part]


def _analyze_selector(selector: str, refs: _ScriptReferences) -> None:
    if not _ANALYZABLE_SELECTOR.match(selector):
        refs.unanalyzed += 1
        return
    for alternative in selector.split(","):
        for compound in alternative.replace(">", " ").split():
            for part in _split_compound(compound):
                if part.startswith("#"):
                    refs.ids.add(part[1:])
                elif part.startswith("."):
                    refs.classes.add(part[1:])
                else:
                    refs.tags.add(part.lower())


def analyze_script(source: str) -> _ScriptReferences:
    """Which elements this script expects, and which it creates itself."""

    refs = _ScriptReferences()
    for match in _GET_ELEMENT_BY_ID.finditer(source):
        refs.ids.add(match.group("name"))
    for match in _GET_BY_CLASS.finditer(source):
        refs.classes.update(match.group("name").split())
    for match in _QUERY_SELECTOR.finditer(source):
        _analyze_selector(match.group("selector").strip(), refs)

    # Names the script itself introduces at runtime are not missing markup.
    # Scanned across the whole source rather than per string literal: a
    # client-rendered product builds its DOM inside template literals that
    # contain both quote characters and `${...}` interpolation, and a
    # literal-by-literal scan misses all of it. Missing it is not a cosmetic
    # inaccuracy -- it would report every id in a single-page application as
    # unresolved, and a check that cries wolf on working products is a check
    # owners turn off.
    for match in _CLASS_LIST_CALL.finditer(source):
        for literal in _STRING_LITERAL.finditer(match.group("args")):
            refs.dynamic_classes.update(_clean_names(literal.group(2)))
    for match in _CLASSNAME_ASSIGNMENT.finditer(source):
        refs.dynamic_classes.update(_clean_names(match.group(2)))
    for found in _MARKUP_ID_ATTRIBUTE.findall(source):
        refs.dynamic_ids.update(_clean_names(found))
    for found in _MARKUP_CLASS_ATTRIBUTE.findall(source):
        refs.dynamic_classes.update(_clean_names(found))
    for found in _SET_ATTRIBUTE_ID.findall(source):
        refs.dynamic_ids.update(_clean_names(found))

    seen: set[str] = set()
    for name in _TOP_LEVEL_FUNCTION.findall(source):
        if name in seen:
            refs.duplicate_functions.append(name)
        seen.add(name)

    refs.network_apis = sorted(set(_NETWORK_API.findall(source)))
    requests: list[RequestReference] = []
    for api, pattern in _REQUEST_PATTERNS:
        for match in pattern.finditer(source):
            target = match.group("url")
            requests.append(
                RequestReference(
                    api=api,
                    target=target,
                    kind=(
                        classify_request_target(target)
                        if target is not None
                        else RequestTargetKind.UNPROVEN
                    ),
                )
            )
    # Stable order so findings are deterministic across runs regardless of
    # which pattern happened to match first.
    refs.requests = sorted(
        requests, key=lambda item: (item.api, item.target or "", item.kind.value)
    )
    return refs


# -- CSS --------------------------------------------------------------------

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CSS_PSEUDO = re.compile(r"::?[A-Za-z-]+(?:\([^)]*\))?")
_CSS_ATTRIBUTE = re.compile(r"\[[^\]]*\]")


def css_rule_selectors(source: str) -> list[str]:
    """Every rule prelude in ``source``, at-rule preludes excluded.

    A hand-rolled brace scan rather than a CSS parser: the only structure
    needed is "text before a `{`", nested at-rules included, and a full
    parser would be a dependency this module refuses to take.
    """

    text = _CSS_COMMENT.sub("", source)
    selectors: list[str] = []
    buffer: list[str] = []
    for character in text:
        if character == "{":
            prelude = "".join(buffer).strip()
            buffer = []
            if prelude and not prelude.startswith("@"):
                selectors.append(prelude)
            continue
        if character == "}":
            buffer = []
            continue
        buffer.append(character)
    return selectors


def selector_targets(selector: str) -> tuple[set[str], set[str]]:
    """The ids and classes a rule prelude requires to match anything."""

    cleaned = _CSS_ATTRIBUTE.sub(" ", selector)
    cleaned = _CSS_PSEUDO.sub(" ", cleaned)
    ids: set[str] = set()
    classes: set[str] = set()
    for alternative in cleaned.split(","):
        for compound in alternative.replace(">", " ").replace("+", " ").replace(
            "~", " "
        ).split():
            for part in _split_compound(compound):
                if part.startswith("#"):
                    ids.add(part[1:])
                elif part.startswith("."):
                    classes.add(part[1:])
    return ids, classes


# -- the check itself -------------------------------------------------------


_CROSS_ORIGIN_DETAIL: dict[RequestTargetKind, str] = {
    RequestTargetKind.CROSS_ORIGIN: (
        "requests {target!r}, which names another origin; the product "
        "depends on a host it does not serve"
    ),
    RequestTargetKind.ABSOLUTE_LOOPBACK: (
        "requests {target!r}, an absolute loopback URL; it is local today "
        "but still a hard-coded origin, and it breaks as soon as the "
        "product is served on a different host or port"
    ),
    RequestTargetKind.WEBSOCKET: (
        "opens the WebSocket {target!r}, which names an absolute origin"
    ),
    RequestTargetKind.OTHER_SCHEME: (
        "requests {target!r}, whose scheme is neither same-origin nor http"
    ),
}

_CROSS_ORIGIN_REMEDIATION: dict[RequestTargetKind, str] = {
    RequestTargetKind.CROSS_ORIGIN: (
        "vendor the resource locally, or drop --forbid-external-resources "
        "if this third-party dependency is intended"
    ),
    RequestTargetKind.ABSOLUTE_LOOPBACK: (
        "use a root-relative path such as '/incidents' so the request "
        "follows whatever server delivered the page"
    ),
    RequestTargetKind.WEBSOCKET: (
        "derive the socket URL from location.host, or drop "
        "--forbid-external-resources if the absolute origin is intended"
    ),
    RequestTargetKind.OTHER_SCHEME: (
        "use a same-origin path, or drop --forbid-external-resources"
    ),
}


def _request_finding(
    path: str, request: RequestReference, *, forbid_external_resources: bool
) -> WebProductFinding:
    """One finding per runtime request, always emitted.

    Every request produces a finding even when nothing is wrong, at INFO
    severity. That is the evidence-reporting half of ADR 0073: an owner
    reading the report should be able to see that the product does call its
    own backend, and at which paths, rather than inferring it from silence.
    INFO findings do not fail a check.
    """

    if request.kind == RequestTargetKind.SAME_ORIGIN:
        assert request.target is not None
        return WebProductFinding(
            code=WebProductFindingCode.SAME_ORIGIN_REQUEST,
            severity=WebProductFindingSeverity.INFO,
            path=path,
            symbol=request.target,
            target_kind=request.kind,
            detail=(
                f"{request.api} requests {request.target!r} on the product's "
                "own origin; this is product-internal communication, not an "
                "external resource dependency"
            ),
            remediation=(
                "no action needed for the external-resource policy; use "
                "--forbid-runtime-network-apis if this product must make no "
                "runtime requests at all"
            ),
        )

    if request.kind == RequestTargetKind.UNPROVEN:
        described = (
            f"{request.api} requests {request.target!r}, whose origin is "
            "decided by a runtime value"
            if request.target is not None
            else (
                f"{request.api} is called with a computed target rather than "
                "a string literal"
            )
        )
        return WebProductFinding(
            code=WebProductFindingCode.UNPROVEN_REQUEST_TARGET,
            severity=(
                # A warning, not an error: the target may well be
                # same-origin. But it is not *proven* same-origin, and a
                # policy that forbids external resources must not report an
                # unexamined request as compliant. Owners who want this to
                # block already have --treat-warnings-as-errors.
                WebProductFindingSeverity.WARNING
                if forbid_external_resources
                else WebProductFindingSeverity.INFO
            ),
            path=path,
            symbol=request.target,
            target_kind=request.kind,
            detail=(
                f"{described}; this check cannot tell whether it stays on "
                "the product's own origin"
            ),
            remediation=(
                "use a literal root-relative path where possible, or cover "
                "this request with a behavioral check; it is counted as "
                "unproven, not as compliant"
            ),
        )

    template = _CROSS_ORIGIN_DETAIL[request.kind]
    return WebProductFinding(
        code=WebProductFindingCode.CROSS_ORIGIN_REQUEST,
        severity=(
            WebProductFindingSeverity.ERROR
            if forbid_external_resources
            else WebProductFindingSeverity.INFO
        ),
        path=path,
        symbol=request.target,
        target_kind=request.kind,
        detail=f"{request.api} " + template.format(target=request.target),
        remediation=_CROSS_ORIGIN_REMEDIATION[request.kind],
    )


def verify_web_product(
    root: str | Path,
    *,
    entry: str = "index.html",
    optional_elements: frozenset[str] | set[str] | None = None,
    forbid_external_resources: bool = False,
    forbid_runtime_network_apis: bool = False,
) -> WebProductReport:
    """Cross-reference a dependency-free browser product's own files.

    Pure and offline: reads files, resolves references between them, and
    reports disagreements. Nothing is executed, nothing is fetched, and
    nothing is written.

    The two request policies are independent, and ADR 0073 separated them
    because collapsing them cost Crisis Atlas its product:

    ``forbid_external_resources``
        No third-party or internet dependency. Cross-origin URLs,
        protocol-relative URLs, WebSockets, absolute loopback origins, and
        external `<script src>`/`<link href>` assets are errors. A
        same-origin request -- `fetch('/incidents')` -- is **not**, because
        talking to the server that served the page is not an external
        dependency. Before ADR 0073 this option failed on any `fetch` at
        all, so a plan that required UI-to-local-API integration had its
        own required mechanism turned into a verification failure; the
        implementation made the check green by deleting the integration.

    ``forbid_runtime_network_apis``
        No runtime request of any kind, same-origin included. This is the
        pre-0073 blanket behaviour, kept for owners who genuinely mean it,
        under a name that says what it does. Off by default.

    Passing both is meaningful and not redundant: the stricter option
    subsumes the looser one for scripts, while only the looser one speaks
    about document assets.
    """

    base = Path(root).resolve()
    optional = set(optional_elements or ())
    findings: list[WebProductFinding] = []

    documents = sorted(
        path for path in base.rglob("*.htm*") if path.is_file() and ".git" not in path.parts
    )
    entry_path = base / entry
    if not entry_path.is_file():
        findings.append(
            WebProductFinding(
                code=WebProductFindingCode.NO_ENTRY_DOCUMENT,
                severity=WebProductFindingSeverity.ERROR,
                path=entry,
                detail=f"{entry} does not exist under {base}",
                remediation=(
                    "create the entry document, or pass --entry with the "
                    "document the product actually opens from"
                ),
            )
        )
        return WebProductReport(root=str(base), findings=findings)

    ids: list[str] = []
    classes: set[str] = set()
    tags: set[str] = set()
    inline_scripts: list[tuple[str, str]] = []
    inline_styles: list[tuple[str, str]] = []
    assets: list[tuple[str, str, Path]] = []
    for document in documents:
        index = _DocumentIndex()
        index.feed(document.read_text(encoding="utf-8", errors="replace"))
        index.close()
        relative = document.relative_to(base).as_posix()
        ids.extend(index.ids)
        classes.update(index.classes)
        tags.update(index.tags)
        inline_scripts.extend((relative, source) for source in index.inline_scripts)
        inline_styles.extend((relative, source) for source in index.inline_styles)
        for _attribute, target in index.assets:
            assets.append((relative, target, document.parent))

    seen_ids: set[str] = set()
    for name in ids:
        if name in seen_ids:
            findings.append(
                WebProductFinding(
                    code=WebProductFindingCode.DUPLICATE_ELEMENT_ID,
                    severity=WebProductFindingSeverity.ERROR,
                    path=entry,
                    symbol=name,
                    detail=(
                        f"id {name!r} is defined more than once; "
                        "getElementById will silently return only the first"
                    ),
                    remediation="make every id unique within the document",
                )
            )
        seen_ids.add(name)

    script_files = sorted(
        path
        for path in base.rglob("*.js")
        if path.is_file() and ".git" not in path.parts
    )
    style_files = sorted(
        path
        for path in base.rglob("*.css")
        if path.is_file() and ".git" not in path.parts
    )

    sources: list[tuple[str, str]] = [
        (path.relative_to(base).as_posix(), path.read_text(encoding="utf-8", errors="replace"))
        for path in script_files
    ]
    sources.extend(
        (f"{document} (inline script)", source) for document, source in inline_scripts
    )

    dynamic_ids: set[str] = set()
    dynamic_classes: set[str] = set()
    # Every word the scripts contain, used only to suppress dead-style-rule
    # warnings. A class a script mentions anywhere is plausibly applied at
    # runtime by a path this module cannot follow, and a warning that fires
    # on working code trains an owner to stop reading warnings. This set is
    # deliberately NOT consulted for the error-level checks, where a loose
    # match would let a genuinely missing element pass.
    script_words: set[str] = set()
    analyses: list[tuple[str, _ScriptReferences]] = []
    for relative, source in sources:
        refs = analyze_script(source)
        analyses.append((relative, refs))
        dynamic_ids.update(refs.dynamic_ids)
        dynamic_classes.update(refs.dynamic_classes)
        script_words.update(_WORD.findall(source))

    available_ids = seen_ids | dynamic_ids | optional
    available_classes = classes | dynamic_classes | optional

    checked = 0
    unanalyzed = 0
    css_selectors_checked = 0
    local_assets_resolved = 0
    same_origin_requests = 0
    cross_origin_requests = 0
    unproven_requests = 0
    for relative, refs in analyses:
        unanalyzed += refs.unanalyzed
        for name in sorted(refs.ids):
            checked += 1
            if name not in available_ids:
                findings.append(
                    WebProductFinding(
                        code=WebProductFindingCode.UNRESOLVED_ELEMENT_ID,
                        severity=WebProductFindingSeverity.ERROR,
                        path=relative,
                        symbol=name,
                        detail=(
                            f"the script looks up element id {name!r}, which "
                            "no document defines and no script creates; the "
                            "lookup returns null at runtime"
                        ),
                        remediation=(
                            f"add an element with id={name!r} to the markup, "
                            "correct the id in the script, or declare it "
                            "optional if it is created at runtime"
                        ),
                    )
                )
        for name in sorted(refs.classes):
            checked += 1
            if name not in available_classes:
                findings.append(
                    WebProductFinding(
                        code=WebProductFindingCode.UNRESOLVED_ELEMENT_CLASS,
                        severity=WebProductFindingSeverity.ERROR,
                        path=relative,
                        symbol=name,
                        detail=(
                            f"the script selects class {name!r}, which no "
                            "document carries and no script adds"
                        ),
                        remediation=(
                            f"add class={name!r} to the intended element or "
                            "correct the selector"
                        ),
                    )
                )
        for name in refs.duplicate_functions:
            findings.append(
                WebProductFinding(
                    code=WebProductFindingCode.DUPLICATE_FUNCTION_DECLARATION,
                    severity=WebProductFindingSeverity.ERROR,
                    path=relative,
                    symbol=name,
                    detail=(
                        f"function {name!r} is declared more than once; "
                        "JavaScript keeps the last declaration silently"
                    ),
                    remediation="rename or remove the duplicate declaration",
                )
            )
        if forbid_runtime_network_apis and refs.network_apis:
            findings.append(
                WebProductFinding(
                    code=WebProductFindingCode.NETWORK_CALL,
                    severity=WebProductFindingSeverity.ERROR,
                    path=relative,
                    symbol=", ".join(refs.network_apis),
                    detail=(
                        "the script uses a runtime request API in a product "
                        "declared to make no runtime requests of any kind "
                        "(--forbid-runtime-network-apis)"
                    ),
                    remediation=(
                        "remove the request, or drop "
                        "--forbid-runtime-network-apis if the product is "
                        "allowed to call its own backend"
                    ),
                )
            )
        for request in refs.requests:
            findings.append(
                _request_finding(
                    relative,
                    request,
                    forbid_external_resources=forbid_external_resources,
                )
            )
            if request.kind == RequestTargetKind.SAME_ORIGIN:
                same_origin_requests += 1
            elif request.kind == RequestTargetKind.UNPROVEN:
                unproven_requests += 1
            else:
                cross_origin_requests += 1

    style_sources: list[tuple[str, str]] = [
        (path.relative_to(base).as_posix(), path.read_text(encoding="utf-8", errors="replace"))
        for path in style_files
    ]
    style_sources.extend(
        (f"{document} (inline style)", source) for document, source in inline_styles
    )
    for relative, source in style_sources:
        for selector in css_rule_selectors(source):
            css_selectors_checked += 1
            rule_ids, rule_classes = selector_targets(selector)
            missing = sorted(
                [
                    name
                    for name in rule_ids
                    if name not in available_ids and name not in script_words
                ]
                + [
                    name
                    for name in rule_classes
                    if name not in available_classes and name not in script_words
                ]
            )
            if missing:
                findings.append(
                    WebProductFinding(
                        code=WebProductFindingCode.DEAD_STYLE_RULE,
                        severity=WebProductFindingSeverity.WARNING,
                        path=relative,
                        symbol=selector.strip()[:120],
                        detail=(
                            f"this rule targets {', '.join(missing)}, which "
                            "nothing in the product carries, so it can never "
                            "apply and the element it was written for will "
                            "render unstyled"
                        ),
                        remediation=(
                            "apply the missing id/class in the markup or "
                            "remove the rule"
                        ),
                    )
                )

    for document, target, parent in assets:
        # Document assets go through the same classifier as script requests,
        # so "what counts as external" has one definition rather than two
        # that can drift (ADR 0073). An `<img src>` or `<link href>` is
        # never same-origin *communication* in the sense a fetch is, but the
        # origin question is identical.
        kind = classify_request_target(target)
        if kind in _NON_SAME_ORIGIN_KINDS:
            findings.append(
                WebProductFinding(
                    code=WebProductFindingCode.EXTERNAL_RESOURCE_REFERENCE,
                    severity=(
                        WebProductFindingSeverity.ERROR
                        if forbid_external_resources
                        else WebProductFindingSeverity.INFO
                    ),
                    path=document,
                    symbol=target,
                    target_kind=kind,
                    detail=f"{document} references the external resource {target}",
                    remediation=(
                        "vendor the resource locally or relax the "
                        "dependency-free constraint"
                    ),
                )
            )
            continue
        if kind == RequestTargetKind.UNPROVEN:
            unproven_requests += 1
            findings.append(
                WebProductFinding(
                    code=WebProductFindingCode.UNPROVEN_REQUEST_TARGET,
                    severity=(
                        WebProductFindingSeverity.WARNING
                        if forbid_external_resources
                        else WebProductFindingSeverity.INFO
                    ),
                    path=document,
                    symbol=target,
                    target_kind=kind,
                    detail=(
                        f"{document} references {target!r}, whose origin is "
                        "decided at runtime; this check cannot resolve it to "
                        "a local file or confirm it stays on this origin"
                    ),
                    remediation=(
                        "reference a literal local path, or cover it with a "
                        "behavioral check; it is counted as unproven"
                    ),
                )
            )
            continue
        cleaned = target.split("?", 1)[0].split("#", 1)[0]
        # A root-absolute reference is resolved against the product root, not
        # the document's directory -- that is what a browser does when the
        # product is served, and treating `/app.js` as a sibling path would
        # report a working product as broken.
        resolved = (
            (base / cleaned.lstrip("/")) if cleaned.startswith("/") else (parent / cleaned)
        ).resolve()
        if not resolved.is_file():
            findings.append(
                WebProductFinding(
                    code=WebProductFindingCode.MISSING_LOCAL_ASSET,
                    severity=WebProductFindingSeverity.ERROR,
                    path=document,
                    symbol=target,
                    target_kind=kind,
                    detail=f"{document} references {target}, which does not exist",
                    remediation="create the file or correct the reference",
                )
            )
        else:
            local_assets_resolved += 1

    if unanalyzed:
        findings.append(
            WebProductFinding(
                code=WebProductFindingCode.UNANALYZED_SELECTOR,
                severity=WebProductFindingSeverity.INFO,
                path=entry,
                detail=(
                    f"{unanalyzed} selector(s) were too complex to analyze "
                    "with confidence and were not checked"
                ),
                remediation=(
                    "treat these as unverified; simplify them or cover them "
                    "with a behavioral check"
                ),
            )
        )

    evidence = WebCheckEvidence(
        element_references_checked=checked,
        css_selectors_checked=css_selectors_checked,
        local_assets_resolved=local_assets_resolved,
        same_origin_api_references=same_origin_requests,
        cross_origin_api_references=cross_origin_requests,
        # Selectors too complex to analyze are the same category of "looked
        # at it, could not prove anything" as a computed request target, so
        # they are counted together rather than reported as two unrelated
        # numbers an owner has to add up.
        dynamic_references_unproven=unproven_requests + unanalyzed,
        # `verify_web_product` never executes anything. This is hard-coded
        # False rather than parameterised so no caller can set it without
        # a real probe existing; `run_behavioral_probe` still raises.
        end_to_end_behavior_measured=False,
    )
    if evidence.is_negligible:
        findings.append(
            WebProductFinding(
                code=WebProductFindingCode.NEGLIGIBLE_EVIDENCE,
                severity=WebProductFindingSeverity.WARNING,
                path=entry,
                detail=(
                    "this check cross-checked nothing: no element "
                    "references, no CSS selectors, and no API references "
                    "were examined, so a pass here is a valid but nearly "
                    "empty static result"
                ),
                remediation=(
                    "if the product's markup is generated at runtime or "
                    "driven by data attributes this check cannot follow, "
                    "configure a project-specific acceptance command that "
                    "exercises the behavior instead of relying on this one"
                ),
            )
        )

    return WebProductReport(
        root=str(base),
        documents=[path.relative_to(base).as_posix() for path in documents],
        scripts=[path.relative_to(base).as_posix() for path in script_files],
        stylesheets=[path.relative_to(base).as_posix() for path in style_files],
        element_ids=sorted(seen_ids),
        element_classes=sorted(classes),
        checked_references=checked,
        unanalyzed_selectors=unanalyzed,
        evidence=evidence,
        findings=findings,
    )


class BrowserProbeUnavailableError(RuntimeError):
    """No browser probe provider is configured.

    Raised rather than returning a passing report, deliberately. The whole
    reason behavioral verification is wanted is that static agreement is not
    proof of behavior; degrading a requested behavioral check into a silent
    pass would reintroduce the exact failure this module was written for.
    """


def run_behavioral_probe(root: str | Path, *, entry: str = "index.html") -> None:
    """The seam for real in-browser verification. No provider is implemented.

    Modelled on the official-document search seam (ADR 0055/0056): the
    boundary is defined and honest about being empty, so that "Apoapsis can
    verify browser behavior" never becomes a claim before it is a fact. A
    provider would need to load ``entry``, assert no uncaught console error
    during initialization, and drive the product's controls; none of that
    is possible from the harness today, and pretending otherwise would be
    worse than the gap.
    """

    raise BrowserProbeUnavailableError(
        "behavioral browser verification was requested but no browser probe "
        "provider is implemented; the static cross-reference check ran, and "
        "no behavioral claim can be made from it"
    )


__all__ = [
    "BrowserProbeUnavailableError",
    "RequestReference",
    "RequestTargetKind",
    "WebCheckEvidence",
    "WebProductFinding",
    "WebProductFindingCode",
    "WebProductFindingSeverity",
    "WebProductReport",
    "analyze_script",
    "classify_request_target",
    "css_rule_selectors",
    "run_behavioral_probe",
    "selector_targets",
    "verify_web_product",
]
