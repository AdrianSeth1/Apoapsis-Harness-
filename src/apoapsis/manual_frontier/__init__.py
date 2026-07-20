"""Manual subscription-based frontier coding handoff (ADR 0031).

Lets a user who has a ChatGPT/Claude *subscription* but no configured API
credential still get frontier-model help on a task that stopped at
``HUMAN_REVIEW_REQUIRED``: Apoapsis exports an immutable, hashed package the
user uploads by hand to their own chat session, and imports one bounded,
schema-validated response back. Apoapsis never authenticates to a hosted
API on this path, never automates either website, and never stores or
reuses subscription credentials -- the model's identity is
operator-declared provenance, and tokens/cost are always ``unmeasured``,
never a fabricated zero. This is entirely separate from, and does not
change, the existing automated API frontier path
(``AUTHORIZE_FRONTIER_STAGE``/``FRONTIER_CONTINUATION``).
"""
