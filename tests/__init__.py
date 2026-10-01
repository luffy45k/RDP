"""mytool test-suite (pure stdlib unittest — the project has zero pip deps).

Run everything:

    ./run_tests.sh              # or:  python3 -m unittest discover -s tests -v

Every test runs against a throw-away data dir (and, where code is patched, a
throw-away copy of the repo), so the suite can never touch your real
~/.my_ai_tool or rewrite files in this checkout.
"""
