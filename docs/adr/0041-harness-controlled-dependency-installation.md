# ADR 0041: Harness-controlled dependency installation

- Status: Accepted
- Date: 2026-07-21

## Context

Allowing a coding model to edit dependency manifests did not make declared
libraries available to verification. The owner explicitly authorized Apoapsis to
install model-chosen packages and to execute package build/install scripts, with
consistent end-to-end execution taking priority.

This changes the previous authority boundary. The model still receives no shell or
direct process authority: it proposes a manifest patch, while the harness selects
the installer invocation from recognized repository facts.

## Decision

Verification configuration now defaults `auto_install_dependencies = true` with a
600-second install timeout. Before configured checks, `VerificationRunner` detects
the first deterministic Python manifest in this order: sorted
`requirements*.txt`, then `pyproject.toml`.

For requirements, the harness runs Python's pip with `--upgrade --target <task
dependency directory> -r <manifest>`. For `pyproject.toml`, it installs the current
project with the same bounded target strategy. On the host, the target is outside
the task worktree under the operating-system temporary directory; Docker uses an
ephemeral path in its copied execution context. `PYTHONPATH` is supplied to all
subsequent configured checks.

Package build and install scripts are allowed. The install is represented as a
required `dependency-install` verification result containing exact argv, bounded
stdout/stderr, exit status, backend metadata, and an explicit
`install_scripts_allowed: true` marker. A failed install skips later checks and
makes aggregate verification fail. It never marks a task complete.

The model cannot select a raw command, installer executable, target directory,
timeout, environment, or completion result. It can influence executed package code
through an allowed manifest, which is the explicit risk accepted by the owner.

## Consequences

- Declared Python packages are available automatically during verification.
- Malicious or compromised packages can execute code with the selected verification
  backend's authority. Host mode remains unsandboxed; Docker remains the recommended
  backend for stronger isolation.
- Network/package-index failures become ordinary auditable verification failures.
- Node and other ecosystem bootstraps are not inferred by this decision; they need
  their own deterministic installer selection before being claimed supported.

## Verification

Deterministic fake-backend coverage verifies installer ordering, manifest argv,
task-scoped `PYTHONPATH`, and audit metadata without contacting a package index.
Per the owner's request, no tests, installation, network call, compile check, or
diff check were run for this change.
