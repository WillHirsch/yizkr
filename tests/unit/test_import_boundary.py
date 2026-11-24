"""Guard for the package layering (docs/ARCHITECTURE.md#package-layering).

The lower layer is every module under yizkr.core and yizkr.model, and
every yizkr module they reach through imports outside yizkr.pipeline and
yizkr.cli; importing a module also reaches the packages that hold it.
A lower-layer module imports neither yizkr.pipeline nor yizkr.cli, and
a module under yizkr.pipeline does not import yizkr.cli. The CLI and the
top-level modules the lower layer does not reach are not constrained.

This test parses every module under src/yizkr. Every import counts, as
`imported_modules` reads them from the source text: at module scope,
inside a function or under TYPE_CHECKING, absolute or relative, and
literal `importlib.import_module` and `__import__` calls. ALLOWED holds
the current exceptions. The test also fails on an entry that no longer
occurs, so a change that removes an allowed import deletes its entry,
and the list only shrinks.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.unit.test_cache_boundary import imported_modules

SRC = Path(__file__).resolve().parents[2] / "src" / "yizkr"
LOWER = ("yizkr.core", "yizkr.model")
PIPELINE = "yizkr.pipeline"
CLI = "yizkr.cli"

# (importing module, imported module), each a current import of the
# pipeline from the lower layer.
ALLOWED = {
    ("yizkr.core.instance", "yizkr.pipeline.stages"),
    ("yizkr.core.instance", "yizkr.pipeline.rundir"),
    ("yizkr.core.write_recovery", "yizkr.pipeline.cancellation"),
    ("yizkr.core.write_recovery", "yizkr.pipeline.assigned_support"),
    ("yizkr.core.claim_applications", "yizkr.pipeline.assigned_support"),
}


def under(name: str, package: str) -> bool:
    return name == package or name.startswith(f"{package}.")


def above(name: str) -> bool:
    return under(name, PIPELINE) or under(name, CLI)


def modules(root: Path) -> dict[str, Path]:
    """The modules of the package tree at `root`, by dotted name, a
    package by its own name."""
    found = {}
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root.parent).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        found[".".join(parts)] = path
    return found


def import_graph(root: Path) -> dict[str, set[str]]:
    """Each module of the tree at `root`, with the other modules of the
    tree it imports."""
    files = modules(root)
    graph = {}
    for name, path in files.items():
        own = f"{name}.__init__" if path.name == "__init__.py" else name
        source = path.read_text(encoding="utf-8")
        targets = set()
        for imported in imported_modules(source, own):
            # `from m import f` names m.f: the module is the longest
            # prefix that is one
            while imported and imported not in files:
                imported = imported.rpartition(".")[0]
            if imported and imported != name:
                targets.add(imported)
        graph[name] = targets
    return graph


def holders(name: str) -> Iterator[str]:
    """The packages that hold module `name`, which importing it also
    imports."""
    while "." in name:
        name = name.rpartition(".")[0]
        yield name


def lower_layer(graph: dict[str, set[str]]) -> set[str]:
    """The modules under yizkr.core and yizkr.model and every module they
    reach through imports outside the pipeline and the CLI."""
    reached = set()
    todo = [name for name in graph if any(under(name, p) for p in LOWER)]
    while todo:
        name = todo.pop()
        if name in reached or name not in graph or above(name):
            continue
        reached.add(name)
        todo.extend(graph[name])
        todo.extend(holders(name))
    return reached


def crossings(root: Path) -> set[tuple[str, str]]:
    """The imports of the tree at `root` against the layering, as
    (importing module, imported module)."""
    graph = import_graph(root)
    found = {
        (name, target)
        for name in lower_layer(graph)
        for target in graph[name]
        if above(target)
    }
    found.update(
        (name, target)
        for name, targets in graph.items()
        if under(name, PIPELINE)
        for target in targets
        if under(target, CLI)
    )
    return found


# -- the rule, applied to the package --------------------------------------


def test_imports_keep_to_the_layering():
    found = crossings(SRC)
    assert found - ALLOWED == set(), (
        "an import against the package layering: neither yizkr.core, "
        "yizkr.model nor a module they import may import yizkr.pipeline "
        "or yizkr.cli, and yizkr.pipeline may not import yizkr.cli "
        "(docs/ARCHITECTURE.md#package-layering)"
    )
    assert ALLOWED - found == set(), (
        "an allowed import no longer occurs: delete its entry from ALLOWED"
    )


# -- planted imports in a package tree --------------------------------------

# Imports that keep to the layering: the CLI and a top-level module the
# lower layer does not reach import anything, the pipeline imports core
# and core imports model.
TREE = {
    "__init__.py": "",
    "cli.py": "from yizkr.pipeline.scratch import run\n",
    "claims.py": "",
    "reset.py": (
        "from yizkr.cli import main\nfrom yizkr.pipeline.scratch import run\n"
    ),
    "core/__init__.py": "",
    "core/config.py": "from yizkr.model.schema import Vocabulary\n",
    "model/__init__.py": "",
    "model/schema.py": "",
    "pipeline/__init__.py": "",
    "pipeline/scratch.py": "from yizkr.core.config import Settings\n",
    "queries/__init__.py": "",
    "queries/publish.py": "",
}


def plant(tmp_path: Path, files: dict[str, str]) -> Path:
    """TREE as a package tree under `tmp_path`, with `files` replacing
    or adding modules."""
    root = tmp_path / "yizkr"
    for relative, source in (TREE | files).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root


def test_imports_down_the_layers_pass(tmp_path):
    assert crossings(plant(tmp_path, {})) == set()


@pytest.mark.parametrize(
    "files, crossing",
    [
        pytest.param(
            {
                "core/config.py": "def load():\n"
                "    from yizkr.pipeline.scratch import run\n"
            },
            ("yizkr.core.config", "yizkr.pipeline.scratch"),
            id="core-imports-pipeline-in-a-function",
        ),
        pytest.param(
            {
                "core/config.py": "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    from yizkr.pipeline.scratch import Run\n"
            },
            ("yizkr.core.config", "yizkr.pipeline.scratch"),
            id="core-imports-pipeline-under-type-checking",
        ),
        pytest.param(
            {
                "core/config.py": "from yizkr import claims\n",
                "claims.py": "def read():\n"
                "    from yizkr.pipeline.scratch import run\n",
            },
            ("yizkr.claims", "yizkr.pipeline.scratch"),
            id="core-reaches-pipeline-through-a-top-level-module",
        ),
        pytest.param(
            {
                "core/config.py": "from yizkr.queries.publish import put\n",
                "queries/__init__.py": "from ..pipeline.scratch import run\n",
            },
            ("yizkr.queries", "yizkr.pipeline.scratch"),
            id="core-reaches-pipeline-through-a-holding-package",
        ),
        pytest.param(
            # a model module that core does not import
            {"model/bootstrap.py": "import yizkr.pipeline.scratch\n"},
            ("yizkr.model.bootstrap", "yizkr.pipeline.scratch"),
            id="model-imports-pipeline",
        ),
        pytest.param(
            {"core/__init__.py": "from ..pipeline.scratch import run\n"},
            ("yizkr.core", "yizkr.pipeline.scratch"),
            id="core-package-imports-pipeline-relatively",
        ),
        pytest.param(
            {
                "core/config.py": "import importlib\n"
                "importlib.import_module('yizkr.pipeline.scratch')\n"
            },
            ("yizkr.core.config", "yizkr.pipeline.scratch"),
            id="core-imports-pipeline-by-import-module",
        ),
        pytest.param(
            {"core/config.py": "import yizkr.cli\n"},
            ("yizkr.core.config", "yizkr.cli"),
            id="core-imports-cli",
        ),
        pytest.param(
            {"pipeline/scratch.py": "from yizkr.cli import main\n"},
            ("yizkr.pipeline.scratch", "yizkr.cli"),
            id="pipeline-imports-cli",
        ),
        pytest.param(
            {"pipeline/__init__.py": "from .. import cli\n"},
            ("yizkr.pipeline", "yizkr.cli"),
            id="pipeline-package-imports-cli-relatively",
        ),
    ],
)
def test_a_planted_import_against_the_layering_fails(
    tmp_path, files, crossing
):
    assert crossings(plant(tmp_path, files)) == {crossing}
