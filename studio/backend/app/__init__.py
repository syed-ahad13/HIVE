"""HIVE Studio BFF — the PI-facing console's backend.

Studio is a CLIENT of the HIVE platform, not a second platform (CLAUDE.md §6). It
creates exactly the objects a tenant would have created by hand, it obeys every
invariant in §2 rather than working around any of them, and it holds no authority
the platform does not already enforce.
"""

__all__ = ["catalog", "db", "main", "schema", "validation"]
