"""Built-in, non-executing skill seeds (playbooks and checker templates)."""

from __future__ import annotations

DEFAULT_SKILLS = [
    {
        "key": "plan-first",
        "name": "Plan before editing",
        "category": "procedure",
        "title": "Plan before editing",
        "description": "Read the contract and allowed paths before proposing any edit.",
        "body": (
            "1. Restate the goal and acceptance criteria.\n"
            "2. Read the allowed paths and pinned retrieval spans.\n"
            "3. List the minimum change and the tests that prove it.\n"
            "4. Wait for approval before writes."
        ),
    },
    {
        "key": "smoke-python-check",
        "name": "Python smoke check",
        "category": "checker",
        "title": "Python smoke check",
        "description": "Declare a small deterministic Python smoke check.",
        "body": (
            "check: import the changed module; call the declared callable with a tiny input; "
            "assert shape/type; record stdout and exit code."
        ),
    },
    {
        "key": "readonly-sqlite-query",
        "name": "Read-only SQLite query",
        "category": "procedure",
        "title": "Read-only SQLite query",
        "description": "Inspect data without mutating it.",
        "body": "Use the database connector; single SELECT; row cap; never ATTACH or write.",
    },
    {
        "key": "graph-structure-summary",
        "name": "Repository structure summary",
        "category": "procedure",
        "title": "Repository structure summary",
        "description": "Summarise a pinned repository map with coverage labels.",
        "body": "Read `maya inspect`; report languages, entry points, tests and parser coverage.",
    },
    {
        "key": "test-fast-path",
        "name": "Fast test path",
        "category": "procedure",
        "title": "Fast test path",
        "description": "Run the smallest useful declared check first.",
        "body": "Order checks by cost; run the fastest relevant test before broader suites.",
    },
    {
        "key": "static-python-review",
        "name": "Static Python review",
        "category": "checker",
        "title": "Static Python review",
        "description": "Review a patch without executing it.",
        "body": "Check scope, secret-shaped content, test changes and dependency changes in the diff.",
    },
]

__all__ = ["DEFAULT_SKILLS"]
