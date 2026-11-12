"""Local workload generator for a remote LangWars deployment.

The pack models many player sessions. Administrator credentials are used only
by ``provision`` and ``cleanup``; load scenarios never call admin endpoints.
"""

__version__ = "1.0.0"
