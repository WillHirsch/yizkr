"""Guard for the LLM cache's boundary (#225, ADR-0032).

Only the client factory, yizkr.pipeline.llm, imports the cache module:
the pipeline reaches the cache only through a client the factory wraps.
So the pipeline cannot come to depend on it, and removing the cache means
no longer wrapping the client. This test parses every module under
src/yizkr and fails on any other import of the cache module, in any of
its forms: absolute or relative, `import` or `from ... import`, and
`importlib.import_module` or `__import__` with a literal name.
"""

from __future__ import annotations

import ast
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "yizkr"
CACHE_MODULE = "yizkr.pipeline.llmcache"
# the modules that may import it: the factory, and the module itself
ALLOWED = {"yizkr/pipeline/llm.py", "yizkr/pipeline/llmcache.py"}


def _package(module: str, is_package: bool) -> str:
    return module if is_package else module.rpartition(".")[0]


def imported_modules(source: str, module: str) -> Iterator[str]:
    """Every module a source file imports, by its full dotted name.
    `module` is the file's own dotted name (`yizkr.pipeline.orchestrate`,
    or `yizkr.pipeline` for a package's __init__), for relative
    imports."""
    is_package = module.endswith(".__init__")
    module = module.removesuffix(".__init__")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _package(module, is_package)
                for _ in range(node.level - 1):
                    base = base.rpartition(".")[0]
                parent = f"{base}.{node.module}" if node.module else base
            else:
                parent = node.module or ""
            yield parent
            for alias in node.names:
                yield f"{parent}.{alias.name}"
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and (
                (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                )
                or (
                    isinstance(node.func, ast.Name)
                    and node.func.id in ("import_module", "__import__")
                )
            )
        ):
            yield node.args[0].value


def imports_cache(source: str, module: str) -> bool:
    return any(
        name == CACHE_MODULE or name.startswith(f"{CACHE_MODULE}.")
        for name in imported_modules(source, module)
    )


def importers(root: Path) -> list[str]:
    """The modules under `root` (a copy of src/yizkr) that import the
    cache module, as paths relative to its parent."""
    found = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root.parent)
        module = ".".join(relative.with_suffix("").parts)
        if imports_cache(path.read_text(encoding="utf-8"), module):
            found.append(relative.as_posix())
    return found


# -- the rule, applied to the package --------------------------------------


def test_only_the_client_factory_imports_the_cache():
    assert set(importers(SRC)) <= ALLOWED, (
        "only the client factory (yizkr.pipeline.llm) may import the LLM "
        "cache: take a client from the factory instead (ADR-0032)"
    )
    # the factory does import it, so the rule is not passed vacuously
    assert "yizkr/pipeline/llm.py" in importers(SRC)


# -- planted imports are caught ---------------------------------------------

PIPELINE_MODULE = "yizkr.pipeline.orchestrate"


@pytest.mark.parametrize(
    "source",
    [
        "import yizkr.pipeline.llmcache\n",
        "import yizkr.pipeline.llmcache as cache\n",
        "from yizkr.pipeline import llmcache\n",
        "from yizkr.pipeline.llmcache import EngineCache\n",
        "from . import llmcache\n",
        "from .llmcache import wrap\n",
        "from ..pipeline.llmcache import wrap\n",
        "def f():\n    from yizkr.pipeline.llmcache import wrap\n",
        "import importlib\n"
        "importlib.import_module('yizkr.pipeline.llmcache')\n",
        "from importlib import import_module\n"
        "import_module('yizkr.pipeline.llmcache')\n",
        "__import__('yizkr.pipeline.llmcache')\n",
    ],
)
def test_a_planted_import_is_caught(source):
    assert imports_cache(source, PIPELINE_MODULE)


@pytest.mark.parametrize(
    "source",
    [
        "from yizkr.pipeline.llm import cached_client, open_cache\n",
        "import yizkr.pipeline.llm\n",
        "from . import llm\n",
        '"""Named in a docstring: yizkr.pipeline.llmcache."""\n',
        "NAME = 'yizkr.pipeline.llmcache'\n",
    ],
)
def test_other_mentions_pass(source):
    assert not imports_cache(source, PIPELINE_MODULE)


def test_a_relative_import_in_a_package_init_is_caught():
    assert imports_cache(
        "from .llmcache import wrap\n", "yizkr.pipeline.__init__"
    )
    assert not imports_cache(
        "from .llm import wrap\n", "yizkr.pipeline.__init__"
    )


def test_a_planted_import_fails_the_tree(tmp_path):
    tree = tmp_path / "yizkr"
    shutil.copytree(SRC, tree, ignore=shutil.ignore_patterns("__pycache__"))
    assert set(importers(tree)) <= ALLOWED
    target = tree / "pipeline" / "orchestrate.py"
    target.write_text(
        target.read_text() + "\n\nfrom yizkr.pipeline.llmcache import wrap\n"
    )
    assert "yizkr/pipeline/orchestrate.py" in importers(tree)
