"""Opt-in live integration harnesses.

Tests in this package are part of normal pytest collection but do nothing unless
their explicit infrastructure gates are enabled. A skipped gate is never a pass:
it means the harness did not exercise the capability it claims to verify.

See ``docs/release-gates.md`` for the exact commands, required environment
variables and the claims each gate licenses.
"""
