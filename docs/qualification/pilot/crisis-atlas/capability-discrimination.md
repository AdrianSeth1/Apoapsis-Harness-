# What this case discriminates, and what it cannot

Crisis Atlas separates two failures that a pass/fail outcome merges:

* a **proposal miss** - the agent writes the wrong or incomplete thing;
* a **detection miss** - the harness accepts it.

The historical Slice 2 arm is both at once. Qwen proposed one partial file at
`services/incident_service.py`, created no export service, and wrote no tests.
Apoapsis applied that change, ran four configured commands that never imported
the new file, observed exit 0 on all of them, and terminated the session as
`COMPLETE`. Source: `src/apoapsis/evaluation/crisis_atlas_facts.py`, slice 2,
misses `[proposal, detection]`.

The discriminating property is the seed's inherited suite. `tests/test_smoke.py`
asserts `crisis_atlas.__version__ == "0.1.0"` and nothing else. It imports the
package and touches no service behaviour, so it passes over any candidate that
leaves the package importable - including one that implements nothing. Inherited
green is therefore *guaranteed* here, which makes the case a reliable probe for
whether a harness can distinguish a green suite from a completed task.

## What a result here can and cannot establish

Can: whether the Crisis Atlas regression is fixed - whether a candidate with the
recorded historical failure shape is refused, and whether a candidate that
satisfies every mapped criterion completes.

Cannot: non-inferiority on anything else. Crisis Atlas is a **regression
benchmark**, not held-out evidence. Its failure mode was known before the
acceptance rules were written, so the rules are fitted to it. Three repetitions
of one case cannot support a claim about a corpus, and cannot authorize Apoapsis
as a default. The seven deferred corpus cases exist precisely because this one
cannot stand in for them.
