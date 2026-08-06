"""Constructing concrete Specs by hand, and weaving them into the base spec.

This is where "never concretize" is actually delivered. A dev package's spec is not
solved for; it is *copied* from the installed node and modified in place. Everything
the solver would have decided -- version, variants, compiler, architecture, which
build of each dependency to use -- is already recorded in the installed spec, and we
keep all of it.

Inheriting the compiler nodes verbatim (``gcc``, ``gcc-runtime``, ``glibc``,
``compiler-wrapper``) is what makes the result ABI-compatible with the parts of the
stack we are *not* rebuilding. It costs nothing: they come along with the copy.
"""

import os
from typing import NamedTuple

import spack.error
import spack.hash_types as ht
import spack.repo
import spack.spec
import spack.variant as vt

#: Compiler-ish nodes whose identity must survive into the dev spec.
ABI_NODES = ("gcc", "llvm", "gcc-runtime", "glibc", "musl", "compiler-wrapper")


class SpecBuildError(spack.error.SpackError):
    """Raised when a dev spec cannot be constructed."""


def recanonicalize(spec):
    """Round-trip a spec through JSON to reset its in-memory state.

    Needed between splices, not cosmetic. Splicing an already-spliced DAG gets
    dramatically more expensive each time -- on a 319-node stack the successive
    splices measured 1.4s, then 14.7s, and the next would have been minutes. A
    freshly deserialized spec carries explicit hashes for every node and none of
    the partially-invalidated caches a splice leaves behind, which flattens the
    cost back to ~1.9s per splice.

    (The obvious suspect, ``build_spec`` chains deepening and making the
    frankenhash at ``spec.py:2161`` recurse, was measured and ruled out: there were
    no chains to collapse.)
    """
    return spack.spec.Spec.from_json(spec.to_json(hash=ht.dag_hash))


def finalize(spec) -> None:
    """Recompute hashes after hand-editing a concrete spec.

    Call this again after any further mutation (recipe-drift reconciliation, for
    instance) -- it is safe to repeat. ``_package_hash`` is dropped because the
    recipe on disk may have changed since the node was installed, so the cached
    value is not necessarily still valid.
    """
    spec._package_hash = None
    spec.clear_caches()
    spec._mark_concrete()


def dev_spec(node, source_path: str, version=None):
    """Return a concrete Spec for developing ``node`` out of ``source_path``.

    ``node`` is taken from the DAG being spliced, so its dependencies already point
    at any dev packages spliced in before it.

    Setting the ``dev_path`` variant is not just bookkeeping: it makes
    ``pkg.stage`` a ``DevelopStage`` rooted at the source directory
    (``package_base.py:1208``), so the build runs against the working tree with no
    fetch, no checksum and no staging copy.
    """
    dev = node.copy(deps=True)

    if version is not None:
        dev.versions = spack.spec.VersionList([spack.spec.Version(version)])

    dev.variants["dev_path"] = vt.SingleValuedVariant("dev_path", source_path)

    # A dev package is genuinely compiled from source, so its build_spec is itself.
    # Without this it inherits provenance from whatever splice already rewired on
    # the node we copied, and claims to have been built as the installed spec.
    dev._build_spec = None

    finalize(dev)
    return dev


def borrow_abi_nodes(spec, base_root, donor_name=None) -> None:
    """Give ``spec`` the same compiler and runtime nodes the base stack uses.

    A package that was never in the base spec has no compiler of its own, and
    recipes ask for one only through the ``c``/``cxx`` virtuals -- which cannot be
    resolved without the concretizer. Copying the edges from a node that is already
    in the stack sidesteps that entirely, and is also exactly what ABI compatibility
    requires: the new package must be built by the same compiler as everything it
    will link against.
    """
    donor = None
    if donor_name:
        donor = base_root[donor_name]
    else:
        # Any node that has a compiler will do; they are uniform within a stack.
        for node in base_root.traverse():
            if node.dependencies(virtuals="cxx") or node.dependencies(virtuals="c"):
                donor = node
                break
    if donor is None:
        raise SpecBuildError(
            f"no node in {base_root.name} has a compiler to copy for {spec.name}"
        )

    have = {e.spec.name for e in spec.edges_to_dependencies()}
    for edge in donor.edges_to_dependencies():
        if edge.spec.name in ABI_NODES and edge.spec.name not in have:
            spec.add_dependency_edge(
                edge.spec.copy(deps=True),
                depflag=edge.depflag,
                virtuals=edge.virtuals,
                direct=edge.direct,
            )


def new_spec(name: str, base_root, source_path: str, version=None, prefer=None):
    """Build a concrete Spec for a package that is *not* in the base spec.

    No search happens. The version comes from the recipe's own preference unless
    given, variants from the recipe's defaults, the compiler from the base stack,
    and every dependency is resolved by name against the base DAG (then the store).
    A dependency that exists in neither stops the operation -- see
    :func:`drift.reconcile`.
    """
    import spack.package_base as pb
    import spack.version as vn

    from spack.extensions.splice import drift

    try:
        pkg_cls = spack.repo.PATH.get_pkg_class(name)
    except Exception as e:  # noqa: BLE001 -- an unknown package is a normal user error
        raise SpecBuildError(f"no recipe found for '{name}'", str(e)) from e

    spec = spack.spec.Spec(name)
    spec.namespace = pkg_cls.namespace
    spec.versions = vn.VersionList(
        [vn.Version(version) if version else pb.preferred_version(pkg_cls)]
    )
    spec.architecture = base_root.architecture.copy()

    # A concrete spec carries every compiler-flag key, empty or not. A bare Spec()
    # has an empty FlagMap, and the build environment then dies on KeyError: 'cflags'
    # while assembling flags.
    for flag in spack.spec.FlagMap.valid_compiler_flags():
        spec.compiler_flags[flag] = []

    borrow_abi_nodes(spec, base_root)
    # Fills in variant defaults and resolves dependencies, iterating until stable.
    drift.reconcile(spec, base_root, prefer=prefer)

    spec.variants["dev_path"] = vt.SingleValuedVariant("dev_path", source_path)
    finalize(spec)
    return spec


def without_dev_path(spec):
    """A copy of ``spec`` with ``dev_path`` stripped, for fetching.

    ``dev_path`` turns ``pkg.stage`` into a ``DevelopStage`` rooted at the source
    directory (``package_base.py:1208``), which is exactly what we want at build
    time and exactly wrong at fetch time -- a DevelopStage has no fetcher.
    """
    if "dev_path" not in spec.variants:
        return spec
    clean = spec.copy(deps=True)
    del clean.variants["dev_path"]
    finalize(clean)
    return clean


def remove_dependency(spec, name: str) -> list:
    """Drop ``spec``'s edges on ``name``. Returns the removed edges.

    ``_dependencies`` is a plain ``Dict[str, List[DependencySpec]]``
    (``spec.py:1049``), so the edge has to come out of both the parent's dependency
    map and the child's ``_dependents`` back-map. ``Spec.detach()`` is not the tool
    for this -- it detaches a node from its parents, not one child edge.
    """
    removed = []
    for edge in list(spec.edges_to_dependencies(name=name)):
        spec._dependencies[name].remove(edge)
        if not spec._dependencies[name]:
            del spec._dependencies[name]
        back = edge.spec._dependents.get(spec.name, [])
        if edge in back:
            back.remove(edge)
        removed.append(edge)
    return removed


def replace_dependency(spec, name: str, replacement) -> None:
    """Re-point ``spec``'s edge on ``name`` at ``replacement``, preserving the edge."""
    for edge in remove_dependency(spec, name):
        spec.add_dependency_edge(
            replacement, depflag=edge.depflag, virtuals=edge.virtuals, direct=edge.direct
        )


def weave(root, dev_hashes, source_for, version_for=None):
    """Splice dev builds of ``dev_hashes`` into ``root``. Returns the new root.

    Walks the dev set dependency-first, re-deriving each dev spec from the *current*
    spliced DAG so that a dev package depending on another dev package picks up the
    dev build rather than the installed one.

    ``Spec.splice`` does the hard part: rewiring every dependent, masking and
    reattaching build edges, recording ``build_spec`` provenance, and invalidating
    the affected hashes.

    Two things here are load-bearing, both established by measurement rather than
    from the docs:

    * ``transitive=False``. We are not changing what anything depends on, only which
      build of it is used. ``transitive=True`` reconciles using the dev subtree's
      copies and stampedes: on art-suite it rewired 68 nodes instead of 3 and
      changed ``llvm``'s identity, breaking the ABI guarantee.
    * One splice per dev package, rather than building the dev subgraph by hand and
      splicing only its maximal nodes. The batch version is much faster but wrong --
      with ``transitive=False`` every dev package below the top silently reverts to
      its installed build (4 of 5 on art-suite), and ``transitive=True`` is the
      stampede above. The cost is one full-DAG pass per dev package; see
      ``verify_dev_nodes`` for the guard that catches a regression here.

    Each result is passed through :func:`recanonicalize`, without which the per-
    splice cost compounds badly enough to be unusable -- see that docstring.
    """
    from spack.extensions.splice import graph

    nodes, _, _ = graph.adjacency(root, graph.PROPAGATE)
    version_for = version_for or {}
    current = root

    for h in graph.build_order(root, dev_hashes):
        name = nodes[h].name
        try:
            node = current[name]
        except KeyError as e:
            raise SpecBuildError(f"{name} vanished from the DAG while splicing") from e

        dev = dev_spec(node, source_for[name], version_for.get(name))
        current = recanonicalize(current.splice(dev, transitive=False))

    return current


def build_specs(root, dev_hashes, source_for, version_for=None):
    """Hand-built dev specs, bottom-up, keyed by dag hash.

    These -- not the nodes in the spliced DAG -- are what actually get compiled.
    ``Spec.splice`` strips build dependencies from every node it splices, because
    in Spack's model a spliced node is *rewired* from an existing binary and never
    built; its build deps live on ``build_spec``. Measured on art-suite,
    ``cetlib-except`` goes into the splice with 8 dependencies and comes out with
    2, having lost ``gcc``, ``ninja``, ``cmake``, ``cetmodules`` and ``catch2``.
    Building from that produces ``CMAKE_CXX_COMPILER not set``.

    Restoring the edges afterwards is not an option worth taking: ``dag_hash``
    covers build deps (``hash_types.py:53``), so it would churn every hash in the
    DAG after splicing had already settled. These specs never go through splice,
    so they keep what they were copied with.

    Dev-on-dev dependencies are wired by hand, so a dev package depending on
    another dev package gets the dev build.
    """
    from spack.extensions.splice import drift, graph

    nodes, children, _ = graph.adjacency(root, graph.PROPAGATE)
    version_for = version_for or {}
    dev = set(dev_hashes)
    built, by_name, drifts = {}, {}, []

    for h in graph.build_order(root, dev):
        name = nodes[h].name
        spec = dev_spec(nodes[h], source_for[name], version_for.get(name))
        for kid in children.get(h, ()) & dev:
            replace_dependency(spec, nodes[kid].name, built[kid])
        # Bring the copy into line with the recipe as it is on disk now, before the
        # hashes are settled -- the installed node reflects the recipe at install
        # time, which may no longer be what the user has. ``by_name`` is passed so a
        # newly added dependency on another dev package picks up the dev build.
        d = drift.reconcile(spec, root, prefer=by_name)
        if d:
            drifts.append(d)
        finalize(spec)
        built[h] = spec
        by_name[name] = spec

    return built, by_name, drifts


def frontier_of(buildable):
    """Dag hashes of everything the dev specs depend on that is not itself a dev spec.

    Read off the reconciled dev specs rather than the base DAG, because recipe drift
    can add or drop dependencies -- a newly added one exists only here.
    """
    dev_names = set(buildable)
    out = set()
    for spec in buildable.values():
        for edge in spec.edges_to_dependencies():
            if edge.spec.name not in dev_names:
                out.add(edge.spec.dag_hash())
    return out


def abi_nodes(spec):
    """Map of compiler/runtime node name -> dag hash, for ABI comparison."""
    return {
        s.name: s.dag_hash() for s in spec.traverse() if s.name in ABI_NODES
    }


def check_abi_preserved(original, spliced):
    """Return names of compiler/runtime nodes whose identity changed.

    Should always be empty: dev specs are copies, so the compiler nodes come along
    unchanged. This is cheap insurance against a future change to ``dev_spec``
    quietly breaking the ABI guarantee.
    """
    before, after = abi_nodes(original), abi_nodes(spliced)
    return sorted(
        name for name in before.keys() & after.keys() if before[name] != after[name]
    )


def dev_nodes(spliced, names):
    """The dev packages as they appear in the spliced DAG, by name."""
    return {s.name: s for s in spliced.traverse() if s.name in set(names)}


def assign_prefixes(specs_by_name, state):
    """Point each dev spec at a prefix inside the dev area.

    Dev builds never enter the store, so their prefixes are ours to choose.
    ``Spec.set_prefix`` is public and takes precedence over the store layout, which
    is what lets us keep the store untouched. Prefixes are named after the
    buildable spec's hash, since that is the thing actually compiled.
    """
    prefixes = {}
    for name, spec in specs_by_name.items():
        prefix = state.prefix_for(name, spec.dag_hash())
        spec.set_prefix(prefix)
        prefixes[name] = prefix
    return prefixes


def source_paths(state, names):
    """Source directory for each dev package.

    Implied packages -- ones pulled into the dev set rather than picked -- have no
    directory of their own, so they default to ``<dev-area>/src/<name>``.
    """
    return {
        name: state.picks[name]["path"]
        if name in state.picks
        else os.path.join(state.source_root, name)
        for name in names
    }


class Computed(NamedTuple):
    """Everything derived from a dev area, in one place."""

    #: The woven DAG -- for reporting, not for building. ``None`` if skipped.
    spliced: object
    #: Package name -> the spec to compile, build deps intact.
    buildable: dict
    #: Package name -> its prefix in the dev area.
    prefixes: dict
    #: Recipe drift found while reconciling, one entry per changed package.
    drifts: list
    #: Package names in dependency-first build order, new packages last.
    order: list


def compute(state, root, weave_dag: bool = True) -> "Computed":
    """Derive the buildable specs, prefixes and spliced DAG for the dev set.

    ``buildable`` maps package name -> the spec to compile (build deps intact);
    ``spliced`` is the woven DAG, useful for reporting but *not* for building --
    see :func:`build_specs`. Pass ``weave_dag=False`` to skip the splice when only
    the buildable specs are needed.

    No concretization happens here, which is why it costs seconds rather than the
    22.7s a real solve of art-suite takes.
    """
    from spack.extensions.splice import graph

    # Packages added with --new are not in the base DAG, so they take a different
    # route: constructed from the recipe rather than copied from an installed node.
    existing = [n for n, p in state.picks.items() if not p.get("new")]
    added = [n for n, p in state.picks.items() if p.get("new")]

    picks = graph.resolve_picks(root, existing)
    dev = graph.dev_set(root, picks.values())
    nodes, _, _ = graph.adjacency(root, graph.PROPAGATE)

    names = [nodes[h].name for h in dev] + added
    source_for = source_paths(state, names)

    _by_hash, buildable, drifts = build_specs(root, dev, source_for)
    order = [nodes[h].name for h in graph.build_order(root, dev)]

    # New packages come last: nothing already in the stack can depend on one without
    # a recipe edit, and that edit is drift, handled above.
    for name in sorted(added):
        buildable[name] = new_spec(name, root, source_for[name], prefer=buildable)
        order.append(name)

    prefixes = assign_prefixes(buildable, state)

    spliced = None
    if weave_dag:
        spliced = weave(root, dev, source_for)
        # Only the *substituted* packages are checked. A --new package is an
        # addition to the stack, not a replacement of a node in it, and
        # ``Spec.splice`` can only swap something that is already there -- so new
        # packages are deliberately absent from the woven DAG.
        verify_dev_nodes(spliced, [nodes[h].name for h in dev])

    return Computed(spliced, buildable, prefixes, drifts, order)


def verify_dev_nodes(spliced, names) -> None:
    """Assert every dev package really came through the splice as a dev build.

    Splice reconciliation can silently drop a hand-built node and leave the
    installed one in place -- that is exactly what ``transitive=False`` did here.
    The failure is quiet and produces a dev area that rebuilds nothing, so it is
    worth an explicit check rather than trusting the splice.
    """
    found = dev_nodes(spliced, names)
    missing = [n for n in names if n not in found]
    if missing:
        raise SpecBuildError(f"dev packages absent from the spliced DAG: {', '.join(missing)}")

    stale = [n for n, s in found.items() if not s.variants.get("dev_path")]
    if stale:
        raise SpecBuildError(
            f"dev packages reverted to their installed build: {', '.join(sorted(stale))}",
            "The splice discarded the hand-built specs. This is a splice bug, not a "
            "usage error.",
        )


def save(state, spliced) -> None:
    with open(state.spliced_file, "w") as f:
        f.write(spliced.to_json(hash=ht.dag_hash))


def load(state):
    """Read back the cached spliced DAG, or None if there isn't one."""
    try:
        with open(state.spliced_file) as f:
            return spack.spec.Spec.from_json(f)
    except OSError:
        return None
