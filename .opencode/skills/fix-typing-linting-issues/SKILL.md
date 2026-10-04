---
name: fix-typing-linting-issues
description: Guide for fixing typing and linting issues. Use this when checkers surface linting or typing issues (eg ruff, mypy, etc)
---

Best practices for fixing typing and linting issues:

* Fix all the issues, including pre-existing ones.
* Do not add ignore flags to type checks unless strictly necessary.
* Do not add ignore flags to linting checks unless strictly necessary.
* Do not try to avoid fixing the issues by bypassing the pre-commit hooks.

If you need to run checks, run the dev script (consult the README for reference).
