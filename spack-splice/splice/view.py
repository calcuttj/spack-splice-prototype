"""The dev_view: a symlink farm over everything below the dev set.

Everything the dev packages depend on but that we are *not* rebuilding gets merged
into one directory, so a dev build sees a single ``-I``/``-L`` root instead of ~200
separate store prefixes.

Built with Spack's own ``SimpleFilesystemView``, using the same atomic build-then-swap
dance as ``ViewDescriptor.regenerate``: the real tree is ``<dev>/._view_<hash>`` and
``<dev>/view`` is a symlink to it. A regeneration that fails leaves the previous view
untouched.
"""

import os
import shutil
import tempfile

import spack.error
import spack.filesystem_view as fsv
import spack.util.filesystem as fs
import spack.util.tty as tty
import spack.repo
import spack.store
import spack.util.hash
from spack.util.filesystem import symlink
from spack.util.link_tree import MergeConflictError


#: Records which spec set a built view corresponds to. Kept inside the view tree
#: rather than encoded in its directory name, so the staging directory can always
#: have a fresh name without breaking the up-to-date check.
MARKER = ".splice-view-hash"


class ViewError(spack.error.SpackError):
    """Raised when the dev_view cannot be built."""


def _content_hash(specs) -> str:
    """Stable short hash of the view's contents, so an unchanged view is a no-op."""
    return spack.util.hash.b32_hash(",".join(sorted(s.dag_hash() for s in specs)))[:8]


def _installed_hash(view_root: str):
    """Content hash of the view currently linked at ``view_root``, if any."""
    try:
        with open(os.path.join(view_root, MARKER)) as f:
            return f.read().strip()
    except OSError:
        return None


def _exclude_duplicate_runtimes(specs):
    """Keep only the newest of each runtime package.

    Lifted from ``ViewDescriptor._exclude_duplicate_runtimes`` (environment.py:978).
    Without it, two ``gcc-runtime`` versions in the closure collide in the view.
    """
    runtimes = spack.repo.PATH.packages_with_tags("runtime")
    newest = {}
    for s in specs:
        if s.name in runtimes:
            newest[s.name] = max(newest.get(s.name, s), s, key=lambda x: x.version)
    return [s for s in specs if s.name not in runtimes or newest[s.name] == s]


def linkable(specs):
    """Filter a closure down to specs that can actually be linked into a view.

    Externals have no prefix of ours to link, and anything not installed has no
    prefix at all -- that happens when the base spec came from an environment whose
    specs were never built.
    """
    keep, missing = [], []
    for s in specs:
        if s.external:
            continue
        if not s.installed:
            missing.append(s)
            continue
        keep.append(s)
    return _exclude_duplicate_runtimes(keep), missing


def regenerate(state, specs, strict: bool = False) -> str:
    """(Re)build the dev_view at ``state.view_root``. Returns the real tree's path.

    ``specs`` must be root-to-leaf topologically ordered, as
    ``SimpleFilesystemView.add_specs`` requires.

    Conflicts are reported and then tolerated (first package wins), which is what
    Spack's own environment views do -- in a ~200 package closure they are almost
    always ``LICENSE``, ``README`` and ``share/info/dir``, none of which matter for
    a view whose job is to provide ``-I`` and ``-L``. Pass ``strict=True`` to make
    them fatal instead.
    """
    specs, missing = linkable(specs)
    if missing:
        tty.warn(
            f"{len(missing)} packages in the closure are not installed and were skipped: "
            + ", ".join(sorted(s.name for s in missing)[:5])
            + (" ..." if len(missing) > 5 else "")
        )
    if not specs:
        raise ViewError("nothing to link into the dev_view")

    view_root = state.view_root
    want = _content_hash(specs)

    # --strict is used to validate, so it must not short-circuit on an existing view.
    if _installed_hash(view_root) == want and not strict:
        tty.msg(f"dev_view is up to date ({len(specs)} packages)")
        return os.path.realpath(view_root)

    # Always stage into a fresh directory. Building in place would mean the failure
    # path deletes whatever the live symlink points at.
    fs.mkdirp(state.path)
    staging = tempfile.mkdtemp(prefix="._view_", dir=state.path)

    def build(ignore_conflicts):
        fsv.SimpleFilesystemView(
            staging,
            spack.store.STORE.layout,
            ignore_conflicts=ignore_conflicts,
            link_type="symlink",
        ).add_specs(*specs)

    tmp_link = os.path.join(state.path, "._view_link")
    try:
        # Strict first, so we can tell the user what collides; then, unless they
        # asked for strict, do it again letting the first package win. The strict
        # pass bails before linking anything, so this costs a directory walk.
        try:
            build(ignore_conflicts=False)
        except MergeConflictError as e:
            if strict:
                raise ViewError("the dev_view has file conflicts", str(e)) from e
            tty.warn(f"dev_view file conflicts, keeping the first of each:\n{e}")
            shutil.rmtree(staging, ignore_errors=True)
            os.mkdir(staging)
            build(ignore_conflicts=True)

        with open(os.path.join(staging, MARKER), "w") as f:
            f.write(want)

        # Swap the symlink atomically so a reader never sees a half-built view.
        previous = os.path.realpath(view_root) if os.path.islink(view_root) else None
        if os.path.lexists(tmp_link):
            os.unlink(tmp_link)
        symlink(staging, tmp_link)
        if os.path.isdir(view_root) and not os.path.islink(view_root):
            os.rmdir(view_root)  # fails loudly if a real dir is in the way
        fs.rename(tmp_link, view_root)
    except Exception:
        # Only ever clean up what this call created.
        shutil.rmtree(staging, ignore_errors=True)
        if os.path.lexists(tmp_link):
            os.unlink(tmp_link)
        raise

    # Now that the swap has landed, drop the tree we replaced.
    if previous and previous != staging and os.path.isdir(previous):
        shutil.rmtree(previous, ignore_errors=True)

    tty.msg(f"dev_view: {len(specs)} packages linked into {view_root}")
    return staging
