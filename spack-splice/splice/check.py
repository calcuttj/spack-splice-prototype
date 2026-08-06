"""Checking a dev spec against the constraints its recipe declares.

Splice never runs the solver, so nothing verifies that the versions and variants it
inherited from the installed spec still satisfy what the recipe asks for. Usually
they do -- they were solved for once. They stop doing so when you bump a local
version or edit a `depends_on`, and the symptom is a compile or configure error deep
in the build rather than anything obviously to do with the spec.

This is a *shallow* check, deliberately. It answers "does each dependency in the
graph satisfy the constraint written next to it?" and nothing more. What it cannot do
is anything requiring search: choosing between alternative virtual providers,
propagating a variant across the DAG, or resolving a `conflicts` by picking different
versions. Those need the concretizer, which is the thing we are avoiding.
"""

from typing import List, NamedTuple

import spack.repo
import spack.spec


class Problem(NamedTuple):
    package: str
    detail: str

    def __str__(self):
        return f"{self.package}: {self.detail}"


def _satisfies(spec, when) -> bool:
    try:
        return spec.satisfies(when)
    except Exception:  # noqa: BLE001 -- an unevaluable condition is simply not met
        return False


def check_spec(spec) -> List[Problem]:
    """Constraints in ``spec``'s recipe that the graph does not satisfy."""
    problems = []
    try:
        pkg_cls = spack.repo.PATH.get_pkg_class(spec.name)
    except Exception as e:  # noqa: BLE001 -- reportable, not fatal
        return [Problem(spec.name, f"no recipe found ({e})")]

    have = {e.spec.name: e.spec for e in spec.edges_to_dependencies()}

    for when, by_name in pkg_cls.dependencies.items():
        if not _satisfies(spec, when):
            continue
        for name, dep in by_name.items():
            if spack.repo.PATH.is_virtual(name):
                continue
            node = have.get(name)
            if node is None:
                problems.append(
                    Problem(spec.name, f"requires '{dep.spec}' but has no {name} dependency")
                )
                continue
            if not node.satisfies(dep.spec):
                problems.append(
                    Problem(
                        spec.name,
                        f"requires '{dep.spec}' but the graph has "
                        f"{node.format('{name}{@version}{variants}')}",
                    )
                )

    for when, conflict_list in getattr(pkg_cls, "conflicts", {}).items():
        if not _satisfies(spec, when):
            continue
        for conflict, msg in conflict_list:
            if _satisfies(spec, conflict):
                note = f" ({msg})" if msg else ""
                problems.append(Problem(spec.name, f"conflicts with '{conflict}'{note}"))

    return problems


def check_all(buildable) -> List[Problem]:
    """Check every dev spec. Returns the problems, worst first by package name."""
    out = []
    for name in sorted(buildable):
        out.extend(check_spec(buildable[name]))
    return out
