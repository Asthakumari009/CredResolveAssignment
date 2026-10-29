"""Pacing engines. They only PROPOSE a number of calls; they cannot dial.

Architecture rule (enforced by tests/test_architecture.py): this package imports only
smartdialer.domain and the standard library. It has no reference to the allocator, the
providers, the database or the Safety Controller.
"""
