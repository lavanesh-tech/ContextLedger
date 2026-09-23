"""Deterministic temporal resolution.

* ``model``: value types (VersionSnapshot, ResolvedFact, FactChange, Lineage, ...).
* ``reference``: pure-Python definition of the bitemporal semantics.
The SQL implementation lives in ``app.repositories.temporal`` and is tested
against ``reference`` on randomly generated histories.
"""
