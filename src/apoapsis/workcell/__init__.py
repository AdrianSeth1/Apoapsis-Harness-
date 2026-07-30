"""The Capability Sandbox workcell (ADR 0077, handoff slice 2).

A disposable container in which the *real* Qwen coding CLI runs its own native
tool loop. Apoapsis adapts its event stream into the audit log and keeps every
durable authority — admission, verification, checkpointing, promotion, and
delivery — outside the container.

Nothing in this package decides task state, and nothing here performs
acceptance repair. Slice 2 measures whether the hardened workcell preserves the
capability the unrestricted control demonstrated; that is all.
"""
