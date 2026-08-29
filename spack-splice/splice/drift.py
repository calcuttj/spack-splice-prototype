"""Reconciling a copied spec against the recipe as it is *now*.

A dev spec is copied from the installed node, and that node records the recipe as it
was **at install time**. The moment you edit `package.py` -- add a `variant`, add or
drop a `depends_on` -- the copy is stale. Building from a stale copy fails in
confusing ways: `cmake_args` doing `spec.variants["newvar"]` raises `KeyError`, and a
newly added dependency is simply absent from `CMAKE_PREFIX_PATH`.

Reconciliation is pure lookup. Nothing here searches or solves: variant defaults come
from the recipe, and new dependencies are resolved **by name** against nodes already
in the base DAG, falling back to an ABI-filtered store query. If neither finds it, we
stop and say so rather than quietly concretizing.
"""

from typing import Dict, List, NamedTuple

import spack.deptypes as dt
import spack.error
import spack.repo
import spack.spec
import spack.store

#: Variants Spack manages itself. They are not recipe declarations, so their absence
#: from the recipe must never be read as "the user deleted this".
RESERVED_VARIANTS = frozenset({"dev_path", "patches", "commit"})

#: Compiler and runtime nodes are injected by concretization rather than named in a
#: recipe, so their absence from `depends_on` never means "remove this". Duplicated
#: from ``specbuild`` rather than imported: ``specbuild`` calls into this module, and
#: a module-level import back would be circular.
ABI_NODES = ("gcc", "llvm", "gcc-runtime", "glibc", "musl", "compiler-wrapper")


class DriftError(spack.error.SpackError):
    """Raised when a spec cannot be reconciled with its recipe."""


class Drift(NamedTuple):
    """What reconciliation changed, for reporting."""

    package: str
    added_variants: Dict[str, str]
    removed_variants: List[str]
    added_deps: Dict[str, str]
    removed_deps: List[str]

    def __bool__(self):
        return bool(
            self.added_variants or self.removed_variants or self.added_deps or self.removed_deps
        )

    def summary(self) -> str:
        bits = []
        if self.added_variants:
            bits.append(
                "+variants " + ", ".join(f"{k}={v}" for k, v in sorted(self.added_variants.items()))
            )
        if self.removed_variants:
            bits.append("-variants " + ", ".join(sorted(self.removed_variants)))
        if self.added_deps:
            bits.append(
                "+deps " + ", ".join(f"{k}({v})" for k, v in sorted(self.added_deps.items()))
            )
        if self.removed_deps:
            bits.append("-deps " + ", ".join(sorted(self.removed_deps)))
        return f"{self.package}: " + "; ".join(bits)


def _satisfies(spec, when) -> bool:
    """Does ``spec`` meet a directive's ``when=``? Unknown conditions count as no.

    A ``when`` may reference a variant the spec does not have yet -- precisely the
    situation we are here to fix -- so this must not raise.
    """
    try:
        return spec.satisfies(when)
    except Exception:  # noqa: BLE001 -- an unevaluable condition is simply not met
        return False


def _wanted_variants(spec, pkg_cls):
    """Variant name -> definition, for every variant the recipe declares for ``spec``."""
    wanted = {}
    for name in pkg_cls.variant_names():
        for when, vdef in pkg_cls.variant_definitions(name):
            if _satisfies(spec, when):
                wanted[name] = vdef
    return wanted


def _reconcile_variants(spec, pkg_cls):
    wanted = _wanted_variants(spec, pkg_cls)
    added, removed = {}, []

    for name, vdef in wanted.items():
        if name not in spec.variants:
            value = vdef.make_default()
            spec.variants[name] = value
            added[name] = str(getattr(value, "value", value))

    for name in [n for n in spec.variants if n not in wanted and n not in RESERVED_VARIANTS]:
        del spec.variants[name]
        removed.append(name)

    return added, removed


def _wanted_dependencies(spec, pkg_cls):
    """Dependency name -> depflag, for every ``depends_on`` that applies to ``spec``."""
    wanted = {}
    for when, by_name in pkg_cls.dependencies.items():
        if not _satisfies(spec, when):
            continue
        for name, dep in by_name.items():
            wanted[name] = wanted.get(name, 0) | dep.depflag
    return wanted


def _resolve(name, spec, base_root, prefer=None):
    """Find a concrete node for ``name``: dev build, then base DAG, then store.

    ``prefer`` holds dev specs already built in this run. It is checked first so a
    package that gains a dependency on something you are *also* developing picks up
    the dev build rather than the installed one -- otherwise you would silently link
    against the old copy of your own work.

    The base DAG comes next, because reusing the build already in this stack is what
    keeps everything consistent. The store fallback is filtered to candidates sharing
    the dev spec's architecture and runtime nodes; anything else would not be
    ABI-compatible with the rest of the graph.
    """
    if prefer and name in prefer:
        return prefer[name], "dev build"
    try:
        return base_root[name], "base spec"
    except KeyError:
        pass

    want_abi = {s.name: s.dag_hash() for s in spec.traverse() if s.name in ABI_NODES}
    candidates = [
        c
        for c in spack.store.STORE.db.query(name)
        if str(c.architecture) == str(spec.architecture)
        and all(
            d.dag_hash() == want_abi[d.name]
            for d in c.traverse()
            if d.name in want_abi and d.name in ("gcc-runtime", "glibc")
        )
    ]
    if len(candidates) == 1:
        return candidates[0], "store"
    if not candidates:
        raise DriftError(
            f"{spec.name} now depends on '{name}', which is in neither the base spec "
            "nor the store (for this architecture and runtime)",
            "Splice will not concretize to invent it. Install it, or add it to the "
            "base environment and re-run 'spack splice init'.",
        )
    listing = "\n  ".join(c.format("{name}{@version}{/hash:7}") for c in candidates)
    raise DriftError(
        f"{spec.name} now depends on '{name}', and the store has {len(candidates)} "
        f"ABI-compatible builds:\n  {listing}",
        "Add the one you want to the base environment so the choice is recorded.",
    )


def _reconcile_dependencies(spec, pkg_cls, base_root, prefer=None):
    from spack.extensions.splice import specbuild

    wanted = _wanted_dependencies(spec, pkg_cls)
    have = {e.spec.name: e for e in spec.edges_to_dependencies()}
    added, removed = {}, []

    for name, depflag in wanted.items():
        if name in have or spack.repo.PATH.is_virtual(name):
            # Virtuals are satisfied by whichever node provides them; the existing
            # edge already carries that, and re-resolving would mean solving.
            continue
        node, origin = _resolve(name, spec, base_root, prefer)
        spec.add_dependency_edge(
            node.copy(deps=True), depflag=depflag, virtuals=(), direct=False
        )
        added[name] = origin

    for name, edge in have.items():
        if name in wanted or name in ABI_NODES or edge.virtuals:
            continue
        specbuild.remove_dependency(spec, name)
        removed.append(name)

    return added, removed


#: Variants and dependencies condition each other, so reconciliation iterates. Four
#: passes is far more than any real recipe needs; the loop normally settles in two.
MAX_PASSES = 4


def reconcile(spec, base_root, prefer=None) -> Drift:
    """Bring ``spec`` into line with its recipe. Mutates ``spec`` in place.

    Runs to a fixpoint rather than once, because variant and dependency conditions
    depend on each other. ``nlohmann-json``'s ``ipo`` is declared
    ``when="build_system=cmake ^cmake@3.9:"`` -- it cannot be evaluated until
    ``build_system`` has been defaulted *and* the ``cmake`` edge resolved. A single
    pass silently drops such variants, and the recipe then fails on a missing key.

    The caller must re-finalize afterwards if anything changed -- ``Drift`` is truthy
    exactly when it did.
    """
    try:
        pkg_cls = spack.repo.PATH.get_pkg_class(spec.name)
    except Exception as e:  # noqa: BLE001 -- a missing recipe is a real, reportable state
        raise DriftError(f"no recipe found for {spec.name}: {e}") from e

    added_v, removed_v, added_d, removed_d = {}, [], {}, []
    for _ in range(MAX_PASSES):
        av, rv = _reconcile_variants(spec, pkg_cls)
        ad, rd = _reconcile_dependencies(spec, pkg_cls, base_root, prefer)
        added_v.update(av)
        removed_v.extend(rv)
        added_d.update(ad)
        removed_d.extend(rd)
        if not (av or rv or ad or rd):
            break

    return Drift(spec.name, added_v, removed_v, added_d, removed_d)
