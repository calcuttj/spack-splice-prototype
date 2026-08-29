"""Whether a dev build can actually displace the installed one at run time.

Splice deliberately does not rebuild everything above a dev package. Instead it puts
the dev prefixes on ``LD_LIBRARY_PATH`` and lets the dynamic loader prefer them. That
works only for binaries linked with ``DT_RUNPATH``: ``DT_RPATH`` is consulted *before*
``LD_LIBRARY_PATH`` and wins outright. A dependent installed with RPATH therefore goes
on loading the library it was built against, the dev build has no effect on it, and
nothing about the failure points at linking.

So for each package above a dev package, one of two things has to be true:

* it is being developed as well, and so gets rebuilt against the dev package, or
* its binaries carry RUNPATH, and ``LD_LIBRARY_PATH`` can redirect them.

This module answers that from the ELF files themselves. ``graph.unshadowable``
answers a related question from the shape of the DAG alone, which was right when the
base stack was assumed to be RPATH-linked and is too pessimistic now that it need
not be.
"""

import os
from typing import List, Optional, Tuple

import spack.deptypes as dt

from spack.extensions.splice import build, graph

#: Where an install keeps things that might carry an rpath.
BINARY_DIRS = ("lib", "lib64", "bin")

#: Cap on how many files are inspected per package. Each one costs a readelf, and
#: a wide package can hold hundreds. A package linked one way is almost always
#: linked that way throughout, so a sample settles it; the scan also stops at the
#: first offender, so this only bounds the cost of confirming a *clean* package.
SCAN_LIMIT = 25


def binaries_of(prefix, limit: int = SCAN_LIMIT) -> List[str]:
    """Real files under ``prefix`` worth running readelf on.

    Symlinks are skipped: shared libraries are usually a chain of them pointing at
    one real file, and following each would spend the budget re-reading it.
    """
    found: List[str] = []
    for sub in BINARY_DIRS:
        directory = os.path.join(str(prefix), sub)
        if not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            path = os.path.join(directory, entry)
            if os.path.isfile(path) and not os.path.islink(path):
                found.append(path)
                if len(found) >= limit:
                    return found
    return found


def shadowable(spec) -> Optional[bool]:
    """Whether ``LD_LIBRARY_PATH`` can redirect this installed spec.

    ``True`` when everything inspected carries RUNPATH or no rpath at all, ``False``
    as soon as one file carries RPATH without RUNPATH, and ``None`` when there is
    nothing to go on -- no binaries in the prefix, or no readelf on this machine.
    """
    verdict = None
    for path in binaries_of(spec.prefix):
        tags = build.linking_type_of(path)
        if tags is None:  # readelf unavailable, or the file is unreadable
            continue
        if "RPATH" in tags and "RUNPATH" not in tags:
            return False
        verdict = True
    return verdict


def blocked_dependents(root, package: str, developed=()) -> List[Tuple[object, Optional[bool]]]:
    """Packages above ``package`` that will not pick up its dev build.

    Only *direct* link dependents: those are the ones whose rpath names the dev
    package's install prefix. Anything further up reaches it through them, so it is
    fine as long as they are.

    Returns ``(spec, verdict)`` for each one that is not known to be safe, where the
    verdict is ``False`` for RPATH-linked and ``None`` for undeterminable.
    """
    nodes, _, parents = graph.adjacency(root, dt.LINK)
    developed = set(developed)

    blocked = []
    seen = set()
    for h, spec in nodes.items():
        if spec.name != package:
            continue
        for parent in parents.get(h, set()):
            dependent = nodes[parent]
            if dependent.name in developed or parent in seen:
                continue
            seen.add(parent)
            verdict = shadowable(dependent)
            if verdict is not True:
                blocked.append((dependent, verdict))
    return blocked
