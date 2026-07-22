"""Domain module package markers for the modular monolith.

Each module may grow pragmatic clean-architecture layers later:

- domain
- application
- infrastructure
- presentation

Do not import ORM models across modules. Cross-module links use IDs and
application contracts only.
"""
