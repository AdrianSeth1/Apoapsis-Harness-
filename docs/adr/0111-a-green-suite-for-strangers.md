# ADR 0111: A green suite for strangers, and what a skip must say

## Status

Accepted and implemented on 2026-08-03.

## Context

The suite was carried at "7 failures, 2 errors — documented pre-existing
inventory". Measured on 2026-08-03 against Python 3.14.5 on Windows it was
actually **2,074 tests, 5 failures, 73 errors**. The inventory had not been
re-measured in a while and the number had grown by an order of magnitude.

Internally, carrying known failures is a defensible triage decision. Externally
it is not survivable: a reviewer clones the repo, runs one command, sees 78
problems, and stops. Nothing after that gets read — not the checkpoint
machinery, not the paired evaluation evidence, not the ADR trail. MH-7 exists
because "the suite is red but I know why" and "the suite is red" are the same
artifact to everyone who is not the author.

The 78 problems were not 78 defects. They were five causes.

## Decision

**Every failure is fixed at its cause, or skipped with a reason that names the
cause and what would unblock it. No blanket skips, no deleted assertions, no
`expectedFailure` used to launder a real defect.**

The five causes and their disposition:

**1. A stale plan fixture — 68 errors, fixed.** `tests/architect_helpers.py`'s
`make_slice` produced a slice with test obligations, a verification command
that collects from `tests/`, and no `suggested_path` inside `tests/`. That is
an invalid plan under `UNASSIGNED_TEST_DISCOVERY_ROOT`, a rule added from live
failure PLAN-19E795D6DC4B/SLICE-002 (93 tests written into an uncollected
directory). Every fixture plan was therefore unapprovable, and every test that
needed an approved plan died at `approve_plan` — across `test_architect_slice`,
`test_architect_slice_ui`, `test_plan_auto_run`, `test_architect_cli` and
`test_ui`. The helper carries similar accommodations for ADR 0074, ADR 0076 and
`MISSING_TEST_OBLIGATIONS`; this rule simply never got its turn. Fixed by
giving the default slice a `tests/` path, and the three tests that override
`suggested_paths` an explicit one.

**2. A real defect the fixture was hiding — 1 failure, fixed in the product.**
With the plans valid, `test_derived_specification_preserves_full_approved_slice_contract`
reached its round-trip assertion and failed:
`enrich_specification_with_slice_package` was not a complete mirror of what
`package_slice` builds. It omitted `test_discovery_roots` and
`inherited_slice_ids`, so a task enriched from an approved package silently
lost the two known facts that exist *because* live slices got them wrong —
where tests must live, and that inherited code is out of scope. A continuation
is precisely where a coder most needs both. `test_discovery_roots` is now
carried on `PlanSliceExecutionPackage` (default-empty, so older packages stay
readable) rather than re-derived on read, because re-deriving would let a later
configuration edit change a fact attributed to an already-approved package.

**3. Platform-impossible relay tests — 7 errors, skipped with the product's own
reason.** The relay-fault and real-containment tests require a Unix domain
socket that a Linux container can actually connect to. `assess_socket_support`
already refuses this on a Windows host and explains why in one paragraph. The
tests were gated on `hasattr(socket, "AF_UNIX")`, which is *true* on Windows —
the bind succeeds and the mount silently does not carry socket inodes, which is
the whole point of the assessment. The containment test's guard had the same
shape of bug: `hasattr(os, "geteuid") and os.geteuid() != 0` evaluated False on
Windows, let the test through, and died in `os.chown`. Both now skip on
`assess_socket_support(...).usable` — driven by the product's own rule, so the
skip can never disagree with the error the relay would have raised, and a host
that becomes supported starts running them with no test edit.

**4. Windows-only teardown and fixture bugs — 3 errors/failures, fixed.**
`shutil.rmtree` over a Git repository fails on Windows because loose objects
are read-only and Windows enforces that on the file rather than the directory;
`tests/helpers.py` gained a `remove_tree` that clears the attribute and
retries. Separately, one reference-evidence fixture wrote its file with
`write_text`, which applies platform newline translation, then asserted the
sha256 of `...\n` — the service was correct on both platforms and the fixture
was not. Now written as bytes.

**5. Flakes — named, not skipped.** `test_ui`'s oversized-request
test aborted the connection once in the baseline run and passed in every run
since. Left alone rather than skipped: a skip would hide a real flake, and one
observation is not enough to change behaviour on. Noted in NEXT_STEPS to watch.

A second one surfaced later, during MH-9's full-suite run:
`test_discovery_ui`'s temp directory intermittently fails to delete on Windows
(`OSError [WinError 145] The directory is not empty`) while the test body
itself passes. Fourteen isolated repeats were clean, so this is contention
under full-suite I/O rather than a defect in the test. Its
`TemporaryDirectory` now passes `ignore_cleanup_errors=True`, with the
reasoning recorded at the call site: teardown is not the assertion, and
failing a suite on an undeletable scratch directory reports an environment
condition as a test result — the same category error this ADR exists to
correct.

**A skip must be readable by someone who did not write it.** The house rule
this ADR sets: a skip reason names the missing capability, and where possible
is generated from the product code that owns the requirement rather than
restated in the test. "requires POSIX socket ownership (os.chown); run this
suite under WSL or Linux" is a skip; "not supported here" is not.

## Consequences

- `python -m unittest discover -s tests` exits 0 on a clean checkout:
  **2,075 tests, 48 skipped, ~975 s**, Python 3.14.5, Windows 11, Docker and
  the pilot image absent. MH-8's public repo and demo can now inherit a green
  suite instead of an explanation.
- Skips rose from 40 to 48. All eight additions are the relay/containment tests
  that a Windows host cannot run, and each says so with the relay's own words.
  On WSL2 or Linux they execute.
- One product change ships with this: the package schema gained
  `test_discovery_roots`, which changes `package_sha256` for newly built
  packages. Packages already on disk still load (the field defaults to empty)
  and are still hash-verifiable against their own recorded value, because the
  hash is computed over the package as written.
- The "pre-existing inventory" line is retired. If the suite goes red again,
  that is news rather than inventory.

## Alternatives rejected

**Skip the 68 architect errors as a known fixture gap.** They were one
three-line fixture fix away from passing, and skipping them would have hidden
the real product defect underneath (cause 2), which no one would have found
until a continuation lost a coder's test-location instruction in a live run.

**Fix the Windows platform gaps in the product so the relay tests run
everywhere.** The product already made this decision deliberately and
documented it at length in `platform_support.py`: the limitation is Docker
Desktop's shared filesystem, not Apoapsis. Tests should skip where the product
refuses, not pretend the refusal is not there.
