"""Smoke test: the collection root and the suite itself are wired up.

Task group 1 requires `scripts/test` to be green before any real code lands.
This placeholder exists so pytest has something to collect.
"""


def test_the_test_suite_itself_runs() -> None:
    assert True
