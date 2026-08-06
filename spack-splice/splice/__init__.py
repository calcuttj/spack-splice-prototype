"""``spack splice`` — no-concretize local development against an installed spec.

See ``README.md`` for the design. The short version: take a spec that is already
concrete and already installed, pick a subset of its packages to develop locally,
and rebuild only those — without ever invoking the concretizer.
"""
