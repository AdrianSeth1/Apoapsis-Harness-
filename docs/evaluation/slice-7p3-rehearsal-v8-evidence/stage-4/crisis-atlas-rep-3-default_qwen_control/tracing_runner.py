"""Run a project's own unittest suite and record which lines executed.

Emits coverage.py's JSON report shape from the standard library `trace`
module, so the witness pipeline is unchanged and the measurement carries no
optional dependency.
"""
import json
import os
import sys
import trace
import unittest

artifact = sys.argv[1]
root = os.getcwd()

# `python -m unittest` puts the working directory on sys.path; a script run by
# path does not -- sys.path[0] is the script's own directory. Without this the
# project under test is simply not importable, every test errors, and the
# result reads as "the seed's suite is red" rather than "the runner was wrong".
if root not in sys.path:
    sys.path.insert(0, root)

tracer = trace.Trace(
    count=1, trace=0, ignoredirs=[sys.prefix, sys.exec_prefix]
)
outcome = {}


def _run():
    suite = unittest.TestLoader().discover("tests", top_level_dir=root)
    runner = unittest.TextTestRunner(verbosity=0, stream=sys.stderr)
    outcome["result"] = runner.run(suite)


tracer.runfunc(_run)

files = {}
for (filename, lineno), hits in tracer.results().counts.items():
    if not hits:
        continue
    # `<frozen importlib._bootstrap>` and friends are not files. Reporting
    # them as covered paths would put pseudo-entries in the evidence that no
    # path check can ever resolve.
    if filename.startswith("<") or not os.path.isfile(filename):
        continue
    try:
        relative = os.path.relpath(os.path.realpath(filename), root)
    except ValueError:
        continue
    if relative.startswith(os.pardir) or os.path.isabs(relative):
        continue
    files.setdefault(relative.replace(os.sep, "/"), set()).add(lineno)

with open(artifact, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "files": {
                path: {"executed_lines": sorted(lines)}
                for path, lines in sorted(files.items())
            }
        },
        handle,
        sort_keys=True,
    )

sys.exit(0 if outcome["result"].wasSuccessful() else 1)
