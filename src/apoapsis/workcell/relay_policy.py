"""What the model relay will and will not forward.

Kept pure and separate from the socket code, because this is the part that has
to be right: the relay is the workcell's only egress, and every decision about
what may cross it is made here, with no I/O to obscure it.

The governing rule is that the relay is **not a proxy**. A proxy takes a
destination from its client. This takes a destination from the controller's
configuration and ignores anything the client says about where the request
should go. That is why `CONNECT`, absolute-form request URIs, and `Host`
overrides are rejected outright rather than sanitised — each one is a client
asking to choose an upstream, and there is no version of that request that is
legitimate here.

The route allowlist is a module constant. Configuration may *narrow* it and
cannot widen it, so a permissive config file can never turn the relay into a
general-purpose tunnel to the model host.
"""

from __future__ import annotations

import json
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from apoapsis.specification.schema import StrictModel

#: Every route the workcell is permitted to reach, and the only methods
#: permitted on each. `/health` is included because the forwarder and the
#: preflight check need a cheap liveness probe that spends no tokens.
ALLOWED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/completions"),
        ("GET", "/v1/models"),
        ("GET", "/health"),
    }
)

#: Headers the relay never forwards in either direction. Hop-by-hop headers
#: describe a single connection and are meaningless across the relay;
#: forwarding them is how a keep-alive or upgrade leaks through.
HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

#: Headers that could describe or influence an upstream choice. These are
#: **stripped**, not rejected.
#:
#: `Host` is mandatory in HTTP/1.1, so every real client sends one and refusing
#: it would simply break the relay. The security property does not come from
#: refusing the header — it comes from the relay never consulting it. The
#: upstream is taken from configuration and `http.client` regenerates `Host`
#: from the connection it actually opened, so a client-supplied value cannot
#: reach the model server or change where the request goes. Stripping an
#: ignored header is strictly safer than maintaining a blocklist of the ways
#: someone might phrase the same request.
_UPSTREAM_STEERING_HEADERS: frozenset[str] = frozenset(
    {"host", "x-forwarded-host", "x-forwarded-for", "x-forwarded-proto", "forwarded"}
)


#: Every key an OpenAI-compatible client can use to ask for an output budget.
#: `max_tokens` is the classic one; Qwen Code's own bundle carries
#: `max_completion_tokens` and `max_new_tokens` in its
#: `PROVIDER_OUTPUT_BUDGET_KEYS`, so a cap that only knew about `max_tokens`
#: would be trivially bypassed by a provider preset the operator never read.
OUTPUT_BUDGET_KEYS: tuple[str, ...] = (
    "max_tokens",
    "max_completion_tokens",
    "max_new_tokens",
)

#: The only routes whose bodies are inspected at all. Narrow on purpose: see
#: `classify_request_body` for why body inspection is kept this small.
_OUTPUT_BUDGET_ROUTES: frozenset[str] = frozenset(
    {"/v1/chat/completions", "/v1/completions"}
)


class RelayRejection(StrEnum):
    CONNECT_METHOD = "connect_method"
    ABSOLUTE_FORM_URI = "absolute_form_uri"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    PATH_NOT_ALLOWED = "path_not_allowed"
    MALFORMED_PATH = "malformed_path"
    BODY_TOO_LARGE = "body_too_large"
    RESPONSE_TOO_LARGE = "response_too_large"
    REQUEST_BUDGET_EXHAUSTED = "request_budget_exhausted"
    CONCURRENCY_LIMIT = "concurrency_limit"
    CROSS_ORIGIN_REDIRECT = "cross_origin_redirect"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    #: The upstream closed, or errored, before delivering the body it promised.
    #: Distinct from UPSTREAM_UNAVAILABLE, which means it never answered at all:
    #: here it answered, and then stopped part-way through.
    UPSTREAM_DISCONNECT = "upstream_disconnect"
    #: An SSE response that ended without its terminal event. Kept apart from
    #: UPSTREAM_DISCONNECT because the two are diagnosed differently -- a short
    #: body is a transport fact, a missing `[DONE]` is a protocol one -- and
    #: because Stage 3 injects them as separate faults.
    DROPPED_STREAM = "dropped_stream"
    IDLE_TIMEOUT = "idle_timeout"
    #: The request asked for more output tokens than the run is pinned to.
    OUTPUT_BUDGET_ABOVE_CAP = "output_budget_above_cap"


#: HTTP status the relay returns for each rejection. Deliberately not 200 with
#: an error body: the CLI must see a transport failure, not a model answer.
_REJECTION_STATUS: dict[RelayRejection, int] = {
    RelayRejection.CONNECT_METHOD: 405,
    RelayRejection.ABSOLUTE_FORM_URI: 400,
    RelayRejection.METHOD_NOT_ALLOWED: 405,
    RelayRejection.PATH_NOT_ALLOWED: 403,
    RelayRejection.MALFORMED_PATH: 400,
    RelayRejection.BODY_TOO_LARGE: 413,
    RelayRejection.RESPONSE_TOO_LARGE: 502,
    RelayRejection.REQUEST_BUDGET_EXHAUSTED: 429,
    RelayRejection.CONCURRENCY_LIMIT: 429,
    RelayRejection.CROSS_ORIGIN_REDIRECT: 502,
    RelayRejection.UPSTREAM_UNAVAILABLE: 502,
    # Recorded as 502 for taxonomy purposes. Both are discovered *after* the
    # response line has gone out, so the status the client actually saw is
    # whatever the upstream sent -- usually 200. The record says what happened;
    # it does not claim the status was retroactively changed.
    RelayRejection.UPSTREAM_DISCONNECT: 502,
    RelayRejection.DROPPED_STREAM: 502,
    RelayRejection.IDLE_TIMEOUT: 504,
    # 400, not 413: the body is not too large, it asks for something the run is
    # not pinned to. A distinct status keeps the two apart in the audit trail.
    RelayRejection.OUTPUT_BUDGET_ABOVE_CAP: 400,
}


class RelayDecision(StrictModel):
    allowed: bool
    #: Normalised path to send upstream. Set only when `allowed`.
    upstream_path: str | None = None
    rejection: RelayRejection | None = None
    status: int = 200
    detail: str = ""

    @model_validator(mode="after")
    def validate_decision(self) -> RelayDecision:
        if self.allowed and (self.rejection is not None or not self.upstream_path):
            raise ValueError("an allowed decision needs a path and no rejection")
        if not self.allowed and self.rejection is None:
            raise ValueError("a refused decision must say why")
        return self


def _refuse(rejection: RelayRejection, detail: str) -> RelayDecision:
    return RelayDecision(
        allowed=False,
        rejection=rejection,
        status=_REJECTION_STATUS[rejection],
        detail=detail,
    )


class ModelRelayConfig(StrictModel):
    """The controller's fixed forwarding rule.

    `upstream_base_url` is the *only* place an upstream is named. It is
    origin-only by validation, so a configuration cannot smuggle a path prefix
    that would change which API the workcell reaches.
    """

    upstream_base_url: str = Field(min_length=1)
    #: Absolute host path of the Unix socket the relay listens on. Lives in a
    #: dedicated directory containing nothing else (see `socket_directory`).
    socket_path: str = Field(min_length=1)
    #: Narrowing only. Empty means "the full `ALLOWED_ROUTES` set".
    allowed_routes: list[str] = Field(default_factory=list)
    max_request_bytes: int = Field(default=4_194_304, ge=1_024, le=67_108_864)
    max_response_bytes: int = Field(default=33_554_432, ge=1_024, le=536_870_912)
    max_concurrent_requests: int = Field(default=4, ge=1, le=64)
    #: Closes a connection the workcell opened and then abandoned.
    idle_timeout_seconds: float = Field(default=120.0, gt=0, le=3_600)
    #: Deadline on a single write to the workcell while streaming.
    #:
    #: A client that vanishes mid-stream does not always produce an immediate
    #: EPIPE: once the socket buffer fills, `sendall` simply blocks. Without a
    #: separate, shorter deadline it would block for the full idle timeout,
    #: pinning a relay worker and leaving the model server generating tokens
    #: for a reader that no longer exists. Hitting this is recorded as a
    #: cancellation, which is what it is.
    stream_write_timeout_seconds: float = Field(default=15.0, gt=0, le=600)
    #: Ceiling on a single upstream exchange, streaming included.
    upstream_timeout_seconds: float = Field(default=600.0, gt=0, le=3_600)
    #: Hard session budget, enforced at the socket.
    max_total_requests: int = Field(default=400, ge=1, le=10_000)
    #: The run's pinned output ceiling, enforced on request bodies.
    #:
    #: `None` means no body is inspected at all, which is the pre-Slice-2C
    #: behaviour and stays the default so that existing configurations do not
    #: silently acquire a new refusal. A live run sets this to the same value
    #: as `ModelPin.max_output_tokens`.
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_048_576)

    @model_validator(mode="after")
    def validate_upstream_is_origin_only(self) -> ModelRelayConfig:
        parts = urlsplit(self.upstream_base_url)
        if parts.scheme not in {"http", "https"}:
            raise ValueError("upstream_base_url must be http or https")
        if not parts.hostname:
            raise ValueError("upstream_base_url must name a host")
        if parts.path not in {"", "/"} or parts.query or parts.fragment:
            raise ValueError(
                "upstream_base_url must be an origin with no path, query, or "
                "fragment; the relay appends only allowlisted routes"
            )
        return self

    @model_validator(mode="after")
    def validate_routes_narrow_only(self) -> ModelRelayConfig:
        permitted = {path for _method, path in ALLOWED_ROUTES}
        unknown = sorted(set(self.allowed_routes) - permitted)
        if unknown:
            raise ValueError(
                "allowed_routes may only narrow the built-in allowlist; these "
                f"are not permitted routes: {', '.join(unknown)}"
            )
        return self

    @property
    def upstream_origin(self) -> tuple[str, str, int]:
        parts = urlsplit(self.upstream_base_url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return (parts.scheme, parts.hostname or "", port)

    @property
    def effective_routes(self) -> frozenset[tuple[str, str]]:
        if not self.allowed_routes:
            return ALLOWED_ROUTES
        selected = set(self.allowed_routes)
        return frozenset(
            (method, path) for method, path in ALLOWED_ROUTES if path in selected
        )

    @property
    def socket_directory(self) -> str:
        normalised = self.socket_path.replace("\\", "/")
        return normalised.rsplit("/", 1)[0] or "/"


def classify_request(
    *,
    method: str,
    raw_path: str,
    headers: dict[str, str],
    content_length: int | None,
    config: ModelRelayConfig,
    requests_served: int,
    active_requests: int,
) -> RelayDecision:
    """Decide whether one request may cross the relay.

    Order matters. The steering checks run before the allowlist so that an
    attempt to choose an upstream is reported as what it is, rather than being
    reduced to "unknown path" once the URI has been parsed away.
    """

    upper = method.upper()
    if upper == "CONNECT":
        return _refuse(
            RelayRejection.CONNECT_METHOD,
            "CONNECT would open an arbitrary tunnel; the relay forwards only "
            "allowlisted requests to one configured upstream",
        )

    if "://" in raw_path or raw_path.startswith("//"):
        return _refuse(
            RelayRejection.ABSOLUTE_FORM_URI,
            f"absolute-form request URI {raw_path!r}: the client does not "
            "choose the upstream, the controller's configuration does",
        )

    # Steering headers are not checked here: `sanitise_headers` removes them
    # before anything is forwarded, and the upstream comes from configuration
    # regardless of what the client says.
    if not raw_path.startswith("/"):
        return _refuse(
            RelayRejection.MALFORMED_PATH, f"request URI {raw_path!r} is not a path"
        )

    split = urlsplit(raw_path)
    path = split.path
    if split.fragment:
        return _refuse(
            RelayRejection.MALFORMED_PATH, "a request URI may not carry a fragment"
        )
    if ".." in path.split("/") or "//" in path:
        return _refuse(
            RelayRejection.MALFORMED_PATH,
            f"path {path!r} is not normalised; traversal and empty segments are "
            "refused rather than collapsed",
        )
    # Trailing slashes are the one normalisation worth doing: the CLI and the
    # server disagree about them harmlessly and often.
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    routes = config.effective_routes
    if path not in {allowed_path for _method, allowed_path in routes}:
        return _refuse(
            RelayRejection.PATH_NOT_ALLOWED,
            f"path {path!r} is not a permitted model API or health route",
        )
    if (upper, path) not in routes:
        return _refuse(
            RelayRejection.METHOD_NOT_ALLOWED,
            f"{upper} is not permitted on {path!r}",
        )

    if content_length is not None and content_length > config.max_request_bytes:
        return _refuse(
            RelayRejection.BODY_TOO_LARGE,
            f"request body of {content_length:,} bytes exceeds the "
            f"{config.max_request_bytes:,}-byte ceiling",
        )

    if requests_served >= config.max_total_requests:
        return _refuse(
            RelayRejection.REQUEST_BUDGET_EXHAUSTED,
            f"the session's {config.max_total_requests:,}-request budget is spent",
        )
    if active_requests >= config.max_concurrent_requests:
        return _refuse(
            RelayRejection.CONCURRENCY_LIMIT,
            f"{active_requests} requests are already in flight, at the "
            f"{config.max_concurrent_requests} limit",
        )

    upstream_path = path if not split.query else f"{path}?{split.query}"
    return RelayDecision(allowed=True, upstream_path=upstream_path)


def classify_request_body(
    *, upstream_path: str, body: bytes, config: ModelRelayConfig
) -> RelayDecision:
    """Refuse a request that asks for more output than the run is pinned to.

    **Why the relay inspects a body at all.** Until Slice 2C the relay looked
    only at methods, paths, and headers, and that was a deliberate simplicity:
    a forwarder that does not parse payloads cannot be confused by one. The
    Slice 2B failure is what changes the calculus. The CLI believed it had a
    64,000-token output ceiling against a server serving 16,384, and the only
    thing standing between that belief and a run full of silent truncations was
    a JSON file nobody hashed. Configuration is a statement of intent; the
    relay is the only component on the path that is actually a boundary. So the
    boundary gets to enforce the number.

    **Why it refuses rather than clamps.** Clamping is tempting because it
    always "works", and that is the problem. A clamped request succeeds while
    the client still believes it asked for 64,000 tokens, which reproduces the
    exact defect one layer lower: two components disagreeing about the output
    budget, with nothing failing. The disagreement would then surface as a
    response that stopped early for no visible reason -- indistinguishable, in
    the transcript, from a model that chose to stop. Refusing turns a silent
    measurement error into a loud transport error that `conformance.py` can see
    and an operator can fix at its source. This module already refuses rather
    than sanitises everywhere the sanitised version would change meaning
    (`CONNECT`, absolute-form URIs, traversal), and this is the same case.

    **What it deliberately does not do.** It is not a schema validator and must
    not become one. It reads three top-level integer keys on two routes and
    ignores everything else. A body that is not JSON, is not an object, or
    names no budget key is forwarded untouched: the upstream is entitled to
    reject its own malformed input, and a relay that started adjudicating
    payload shape would become a second, undocumented API surface. The
    guarantee is therefore precise and bounded:

    > No request carrying an explicit output budget above the cap crosses the
    > relay.

    A request that names no budget is governed by the server's own `-n` flag,
    which is pinned separately. That is the honest limit of this check, and it
    is why the check is defence in depth rather than the primary control.
    """

    cap = config.max_output_tokens
    if cap is None:
        return RelayDecision(allowed=True, upstream_path=upstream_path)

    observed = observed_output_budget(upstream_path=upstream_path, body=body)
    if observed is not None and observed.tokens > cap:
        return _refuse(
            RelayRejection.OUTPUT_BUDGET_ABOVE_CAP,
            f"the request asked for {observed.tokens:,} output tokens via "
            f"{observed.key!r}, above this run's pinned {cap:,}-token ceiling; "
            "the relay refuses rather than clamping so the disagreement is "
            "visible instead of becoming an unexplained early stop",
        )
    return RelayDecision(allowed=True, upstream_path=upstream_path)


class ObservedOutputBudget(StrictModel):
    """The largest explicit output budget a request carried, and which key
    carried it.

    Separated from the refusal decision because the two answer different
    questions. `classify_request_body` answers "may this cross?", which is a
    boundary control. This answers "what did the client actually ask for?",
    which is *evidence* -- and Slice 2C needs it in the affirmative form. A run
    that merely records "no request was refused" cannot distinguish a fleet of
    well-behaved requests from a relay whose cap was never configured, and the
    latter is exactly the silent-no-op failure the pins exist to catch.
    """

    #: The budget value observed on the wire.
    tokens: int = Field(ge=0)
    #: Which of `OUTPUT_BUDGET_KEYS` carried it, so a disagreement names the
    #: field an operator has to go and change.
    key: str = Field(min_length=1)


def observed_output_budget(
    *, upstream_path: str, body: bytes
) -> ObservedOutputBudget | None:
    """Read the explicit output budget off a request body, or `None`.

    `None` means "this request named no budget", which is not the same as zero
    and must not be recorded as zero: such a request is governed by the
    server's own `-n` flag rather than by anything the client said. Callers
    that summarise a run have to keep that distinction or they will report a
    reassuring peak of 0 for traffic they never actually inspected.

    The maximum across the recognised keys is returned rather than the first
    match, because a body naming two budgets is bounded by the larger one.
    """

    if urlsplit(upstream_path).path not in _OUTPUT_BUDGET_ROUTES:
        return None
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        # Not our business: see `classify_request_body`. The upstream will
        # reject it, and guessing at a budget here would invent evidence.
        return None
    if not isinstance(payload, dict):
        return None

    best: ObservedOutputBudget | None = None
    for key in OUTPUT_BUDGET_KEYS:
        value = payload.get(key)
        # `bool` is an `int` in Python and `{"max_tokens": true}` is nonsense
        # rather than a budget, so it is excluded rather than compared.
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        if value < 0:
            continue
        if best is None or value > best.tokens:
            best = ObservedOutputBudget(tokens=value, key=key)
    return best


def classify_redirect(
    *, location: str, config: ModelRelayConfig
) -> RelayDecision:
    """A redirect may not move the workcell to a different origin.

    The relay never follows redirects itself. This decides whether one is even
    safe to hand back: a `Location` pointing anywhere but the configured
    upstream is an upstream trying to relocate the client, and the client here
    has no business being relocated.
    """

    parts = urlsplit(location)
    if not parts.scheme and not parts.netloc:
        # Relative redirect: same origin by construction.
        return RelayDecision(allowed=True, upstream_path=parts.path or "/")
    scheme, host, port = config.upstream_origin
    target_port = parts.port or (443 if parts.scheme == "https" else 80)
    if (parts.scheme, parts.hostname, target_port) == (scheme, host, port):
        return RelayDecision(allowed=True, upstream_path=parts.path or "/")
    return _refuse(
        RelayRejection.CROSS_ORIGIN_REDIRECT,
        f"upstream redirected to {location!r}, which is not the configured "
        "upstream origin",
    )


def sanitise_headers(headers: dict[str, str]) -> dict[str, str]:
    """Drop hop-by-hop and upstream-steering headers in either direction.

    Nothing a client says about routing survives this, which is what makes the
    relay a fixed forwarder rather than a proxy.
    """

    removed = HOP_BY_HOP_HEADERS | _UPSTREAM_STEERING_HEADERS
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in removed
    }
