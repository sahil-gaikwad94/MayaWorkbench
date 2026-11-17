"""Claim-to-code mapping.

* :mod:`mayaworkbench.mapping.signals` — strict behavioural-signal matching;
* :mod:`mayaworkbench.mapping.evidence` — candidate discovery and span analysis;
* :mod:`mayaworkbench.mapping.rules` — the deterministic rule pack;
* :mod:`mayaworkbench.mapping.service` — persistence, adjudication and the matrix.

Submodules are imported lazily to keep ``research`` and ``mapping`` free of
import cycles.
"""

__all__ = ["evidence", "models", "prompts", "rules", "service", "signals"]
