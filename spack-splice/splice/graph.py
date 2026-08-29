"""Graph operations over an already-concrete spec DAG.

Nothing in this module concretizes. Every function takes a concrete root spec and
works purely with the edges that are already there.

A note on why we build our own adjacency maps instead of using
``traverse.traverse_nodes(direction="parents")``: Spec objects are shared across
every DAG loaded from the store database, so parent pointers lead *out* of the DAG
you asked about. Walking parents from ``cetlib`` in the ``art-suite`` DAG reaches 25
nodes, but only 8 of them are actually part of ``art-suite``. Every dependent
computation here is therefore confined to nodes reachable from the given root.
"""

import collections
from typing import Dict, Set, Tuple

import spack.deptypes as dt
import spack.error
import spack.traverse as traverse


class NoSuchPackageError(spack.error.SpackError):
    """Raised when a requested package is not in the base spec."""

#: Deptypes a rebuild propagates along. If you rebuild X, anything that links or
#: runs against X needs rebuilding too; a build-only dependent does not.
PROPAGATE = dt.LINK | dt.RUN

#: Deptypes that matter when assembling a build environment. Wider than PROPAGATE
#: because the dev_view must also carry cmake, ninja, cetmodules and friends.
BUILDABLE = dt.BUILD | dt.LINK | dt.RUN

Adjacency = Tuple[Dict[str, "spack.spec.Spec"], Dict[str, Set[str]], Dict[str, Set[str]]]


def adjacency(root, deptype=PROPAGATE) -> Adjacency:
    """Return ``(nodes, children, parents)`` for the DAG rooted at ``root``.

    All three are keyed by dag hash and contain *only* nodes reachable from
    ``root``, which is what makes the parent map safe to walk.
    """
    nodes = {}
    children: Dict[str, Set[str]] = {}
    parents: Dict[str, Set[str]] = collections.defaultdict(set)

    for spec in root.traverse(deptype=deptype, key=traverse.by_dag_hash):
        h = spec.dag_hash()
        nodes[h] = spec
        kids = {e.spec.dag_hash() for e in spec.edges_to_dependencies(depflag=deptype)}
        children[h] = kids

    # Restrict edges to nodes we actually reached, then invert.
    for h, kids in children.items():
        kids &= nodes.keys()
        for kid in kids:
            parents[kid].add(h)

    return nodes, children, dict(parents)


def _closure(seeds: Set[str], adj: Dict[str, Set[str]]) -> Set[str]:
    """Transitive closure of ``seeds`` over adjacency map ``adj``, inclusive."""
    seen = set(seeds)
    stack = list(seeds)
    while stack:
        for nxt in adj.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def dev_set(root, picks, deptype=PROPAGATE) -> Set[str]:
    """The interval closure of ``picks`` within ``root``'s DAG, as dag hashes.

    A node is in the dev set if it lies on some dependency path *between* two
    picked packages -- that is, it is both an ancestor of a pick and a descendant
    of a pick. Picks themselves are always included.

    For ``A -> B -> C -> D`` with picks ``{A, C}``::

        descendants = {A, B, C, D}      # what the picks depend on
        ancestors   = {A, B, C}         # what depends on the picks
        dev set     = {A, B, C}         # B is pulled in, D is not

    ``D`` stays installed and is symlinked into the dev_view; ``B`` must be rebuilt
    because it sits between two things that are changing.
    """
    _, children, parents = adjacency(root, deptype)
    seeds = set(picks)
    return (_closure(seeds, parents) & _closure(seeds, children)) | seeds


def build_order(root, dev_hashes, deptype=PROPAGATE):
    """Dev-set hashes in dependency-first order.

    Both the weave and the build have to go bottom-up: a dev package must be
    spliced in before anything that depends on it, so the dependent picks up the
    dev build rather than the installed one.
    """
    _, children, _ = adjacency(root, deptype)
    dev = set(dev_hashes)
    ordered, seen = [], set()

    def visit(h):
        if h in seen:
            return
        seen.add(h)
        for kid in sorted(children.get(h, ())):
            if kid in dev:
                visit(kid)
        ordered.append(h)

    for h in sorted(dev):
        visit(h)
    return ordered


def frontier(root, dev_hashes, deptype=BUILDABLE) -> Set[str]:
    """Direct dependencies of the dev set that are not themselves being developed.

    These are the packages that get symlinked into the dev_view. Computed over
    ``BUILDABLE`` rather than ``PROPAGATE`` so that build-only dependencies
    (cmake, ninja, cetmodules) land in the view too.
    """
    nodes, children, _ = adjacency(root, deptype)
    out: Set[str] = set()
    for h in dev_hashes:
        out |= children.get(h, set())
    return out - set(dev_hashes)


def view_closure(root, frontier_hashes, deptype=PROPAGATE):
    """Specs to link into the dev_view: the runtime closure of the frontier.

    Returned root-to-leaf in topological order, which is the order
    ``SimpleFilesystemView.add_specs`` documents that it wants.
    """
    _, children, _ = adjacency(root, deptype)
    wanted = _closure(set(frontier_hashes), children)
    ordered = root.traverse(deptype=BUILDABLE, order="topo", key=traverse.by_dag_hash)
    return [s for s in ordered if s.dag_hash() in wanted]


def unshadowable(root, dev_hashes) -> Dict[str, Set[str]]:
    """Installed dependents that will *not* see the dev builds.

    A package above the dev set that links a dev package directly keeps the
    DT_RPATH baked in at install time, and DT_RPATH beats ``LD_LIBRARY_PATH``. Such
    a dependent silently keeps using the installed library. This is a deliberate
    trade -- rebuilding them all is what we are avoiding -- but the user needs to
    be told which ones they are.

    Returns a map of dev package name -> names of dependents that cannot see it.
    """
    nodes, _, parents = adjacency(root, dt.LINK)
    result = {}
    for h in dev_hashes:
        stuck = {nodes[p].name for p in parents.get(h, set()) if p not in dev_hashes}
        if stuck:
            result[nodes[h].name] = stuck
    return result


def names_in(root, deptype=PROPAGATE):
    """Every package name reachable from ``root``."""
    return {spec.name for spec in adjacency(root, deptype)[0].values()}


def resolve_picks(root, names) -> Dict[str, str]:
    """Map user-supplied package names to dag hashes within ``root``'s DAG.

    Raises ``NoSuchPackageError`` for names that are not in the spec, since that is
    the single most likely user error.
    """
    nodes, _, _ = adjacency(root, PROPAGATE)
    by_name: Dict[str, list] = collections.defaultdict(list)
    for h, spec in nodes.items():
        by_name[spec.name].append(h)

    picks, missing, ambiguous = {}, [], {}
    for name in names:
        found = by_name.get(name, [])
        if not found:
            missing.append(name)
        elif len(found) > 1:
            ambiguous[name] = found
        else:
            picks[name] = found[0]

    if missing:
        raise NoSuchPackageError(
            f"not in this spec: {', '.join(sorted(missing))}",
            "Use 'spack splice add --new' to add a package that isn't there yet.",
        )
    if ambiguous:
        detail = "; ".join(f"{n} ({len(h)} builds)" for n, h in ambiguous.items())
        raise NoSuchPackageError(f"ambiguous, specify by hash: {detail}")
    return picks
