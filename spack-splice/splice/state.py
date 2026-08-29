"""Persistent state for a splice dev area.

Lives at ``<dev-area>/splice.yaml``. Deliberately small: it records what the user
chose, not anything derivable from the spec DAG. Everything else is recomputed,
because recomputing it costs well under a second.
"""

import os
from typing import Dict, Optional

import spack.error
import spack.util.spack_yaml as syaml

STATE_FILE = "splice.yaml"

#: Bumped when the on-disk shape changes incompatibly.
FORMAT_VERSION = 1


class NoDevAreaError(spack.error.SpackError):
    """Raised when a command needs a dev area and there isn't one."""


class State:
    """The contents of a dev area's ``splice.yaml``."""

    def __init__(self, path: str, base_hash: str, base_source: str, picks=None):
        #: dev area root
        self.path = os.path.abspath(path)
        #: dag hash of the concrete installed spec we are developing against
        self.base_hash = base_hash
        #: human-readable note of where the base spec came from (store / env path)
        self.base_source = base_source
        #: package name -> {"path": <source dir>, "new": bool}
        self.picks: Dict[str, dict] = picks or {}

    # -- locations ---------------------------------------------------------

    @property
    def state_file(self) -> str:
        return os.path.join(self.path, STATE_FILE)

    @property
    def view_root(self) -> str:
        return os.path.join(self.path, "view")

    @property
    def install_root(self) -> str:
        return os.path.join(self.path, "install")

    @property
    def source_root(self) -> str:
        return os.path.join(self.path, "src")

    @property
    def spliced_file(self) -> str:
        """Where the woven DAG is cached, so status/env need not recompute it."""
        return os.path.join(self.path, "spliced.json")

    def prefix_for(self, name: str, dag_hash: str) -> str:
        return os.path.join(self.install_root, f"{name}-{dag_hash[:7]}")

    def source_path(self, name: str) -> str:
        """Source directory for a developed package."""
        return self.picks[name]["path"]

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "splice": {
                "format_version": FORMAT_VERSION,
                "base": {"hash": self.base_hash, "source": self.base_source},
                "picks": self.picks,
            }
        }

    def write(self) -> None:
        os.makedirs(self.path, exist_ok=True)
        with open(self.state_file, "w") as f:
            syaml.dump(self.to_dict(), stream=f, default_flow_style=False)

    @classmethod
    def load(cls, path: str) -> "State":
        state_file = os.path.join(os.path.abspath(path), STATE_FILE)
        if not os.path.exists(state_file):
            raise NoDevAreaError(
                f"no splice dev area at {path} (no {STATE_FILE}). Run 'spack splice init' first."
            )
        with open(state_file) as f:
            data = syaml.load(f)

        d = data["splice"]
        version = d.get("format_version")
        if version != FORMAT_VERSION:
            raise NoDevAreaError(
                f"{state_file} has format_version {version}, this splice expects {FORMAT_VERSION}"
            )
        return cls(
            path=path,
            base_hash=d["base"]["hash"],
            base_source=d["base"].get("source", "unknown"),
            picks=d.get("picks") or {},
        )


def find(path: Optional[str] = None) -> State:
    """Load the dev area at ``path``, or the one the cwd sits inside.

    Walks upward looking for a ``splice.yaml`` so you can run ``spack splice status``
    from a source subdirectory, the way git behaves.
    """
    if path:
        return State.load(path)

    env_dir = os.environ.get("SPACK_SPLICE_DIR")
    if env_dir:
        return State.load(env_dir)

    current = os.getcwd()
    while True:
        if os.path.exists(os.path.join(current, STATE_FILE)):
            return State.load(current)
        parent = os.path.dirname(current)
        if parent == current:
            raise NoDevAreaError(
                "not inside a splice dev area. Use -d/--dir, set SPACK_SPLICE_DIR, "
                "or run 'spack splice init'."
            )
        current = parent
