"""Local-first Architect Mode discovery, followed by an optional frontier
planning stage (ADR 0032).

A bounded, deterministic workflow -- never a general chat: a configured
local model may propose up to a small, harness-enforced number of
clarification questions and, once the user answers verbatim, one
``IdeaBrief`` the user must explicitly approve. Only after that approval
does an immutable ``FrontierPlanningRequestPackage`` get built, sent to a
frontier model over either an explicitly configured, authorized, and
spend-ceilinged API transport, or a manual subscription transport (upload
one self-contained Markdown file, paste back one hash-bound response) --
never automating a subscription website. The frontier stage may ask for a
small, capped number of further clarification rounds or return a complete
plan; a returned plan continues through the existing, unmodified Architect
Mode import/validation/approval machinery (ADR 0019) unchanged. Neither
model can approve a plan, invent a verification-command name, bypass a
ceiling, execute a slice, or choose a workflow transition.
"""
