"""Guard for the LLM cache's boundary (#225, ADR-0032).

Only the client factory, yizkr.pipeline.llm, imports the cache module:
the pipeline reaches the cache only through a client the factory wraps.
So the pipeline cannot come to depend on it, and removing the cache means
no longer wrapping the client. This test parses every module under
src/yizkr and fails on any other way of reaching the cache module:

* an import of it, absolute or relative, `import` or `from ... import`;
* a call of anything named `import_module` or `__import__`, or an alias
  of one, whose name resolves to it: relative names against the package
  given (`__package__` included), positional or keyword arguments, and
  `__import__`'s fromlist and level;
* any string constant other than a docstring, and any attribute, that
  names it (`sys.modules[...]`, `pkgutil.resolve_name(...)`,
  `yizkr.pipeline.llmcache.wrap`, a name kept in a variable, `exec`).
"""

from __future__ import annotations

import ast
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "yizkr"
CACHE_MODULE = "yizkr.pipeline.llmcache"
CACHE_NAME = CACHE_MODULE.rpartition(".")[2]
NAMES_THE_CACHE = re.compile(rf"\b{CACHE_NAME}\b")
# the modules that may import it: the factory, and the module itself
ALLOWED = {"yizkr/pipeline/llm.py", "yizkr/pipeline/llmcache.py"}
IMPORTERS = ("import_module", "__import__")


def _package(module: str, is_package: bool) -> str:
    return module if is_package else module.rpartition(".")[0]


def _relative(name: str, package: str, level: int) -> str:
    """`name` `level` packages up from `package`, as Python resolves a
    relative import."""
    base = package
    for _ in range(level - 1):
        base = base.rpartition(".")[0]
    return f"{base}.{name}" if name else base


def _callee(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _string(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _argument(call: ast.Call, position: int, keyword: str):
    for item in call.keywords:
        if item.arg == keyword:
            return item.value
    if len(call.args) > position:
        return call.args[position]
    return None


def _importer_aliases(tree: ast.AST) -> set[str]:
    """The names a module binds to an importing function: `from importlib
    import import_module as im`, or `im = importlib.import_module`."""
    aliases = set(IMPORTERS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in IMPORTERS:
                    aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            callee = _callee(node.value)
            if callee is not None and callee.endswith(IMPORTERS):
                aliases.update(
                    target.id
                    for target in node.targets
                    if isinstance(target, ast.Name)
                )
    return aliases


def _call_imports(
    call: ast.Call, package: str, aliases: set[str]
) -> Iterator[str]:
    """What a call of an importing function imports, where its arguments
    say so literally."""
    callee = _callee(call.func)
    if callee is None or not (callee.endswith(IMPORTERS) or callee in aliases):
        return
    name = _string(_argument(call, 0, "name"))
    if name is None:
        return
    if callee.endswith("__import__"):
        level = _argument(call, 4, "level")
        depth = level.value if isinstance(level, ast.Constant) else 0
        resolved = _relative(name, package, depth) if depth else name
        fromlist = _argument(call, 3, "fromlist")
        yield resolved
        if isinstance(fromlist, ast.List | ast.Tuple | ast.Set):
            for element in fromlist.elts:
                item = _string(element)
                if item is not None:
                    yield f"{resolved}.{item}"
        return
    stripped = name.lstrip(".")
    depth = len(name) - len(stripped)
    if not depth:
        yield name
        return
    anchor = _argument(call, 1, "package")
    if isinstance(anchor, ast.Name) and anchor.id == "__package__":
        yield _relative(stripped, package, depth)
    elif _string(anchor) is not None:
        yield _relative(stripped, _string(anchor), depth)


def imported_modules(source: str, module: str) -> Iterator[str]:
    """Every module a source file imports, by its full dotted name, from
    its import statements and its literal calls of importing functions.
    `module` is the file's own dotted name (`yizkr.pipeline.orchestrate`,
    or `yizkr.pipeline.__init__` for a package's __init__), for relative
    imports."""
    is_package = module.endswith(".__init__")
    module = module.removesuffix(".__init__")
    package = _package(module, is_package)
    tree = ast.parse(source)
    aliases = _importer_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parent = _relative(node.module or "", package, node.level)
            else:
                parent = node.module or ""
            yield parent
            for alias in node.names:
                yield f"{parent}.{alias.name}"
        elif isinstance(node, ast.Call):
            yield from _call_imports(node, package, aliases)


def _docstrings(tree: ast.AST) -> set[int]:
    """The constant nodes that are docstrings, by id."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        ):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and _string(first.value):
                found.add(id(first.value))
    return found


def mentions(source: str) -> Iterator[str]:
    """Every string constant (docstrings aside) and attribute that names
    the cache module, as `<line>: <what>`."""
    tree = ast.parse(source)
    docstrings = _docstrings(tree)
    for node in ast.walk(tree):
        text = _string(node) if isinstance(node, ast.Constant) else None
        if (
            text is not None
            and id(node) not in docstrings
            and NAMES_THE_CACHE.search(text)
        ):
            yield f"{node.lineno}: string {text!r}"
        elif isinstance(node, ast.Attribute) and node.attr == CACHE_NAME:
            yield f"{node.lineno}: attribute .{node.attr}"


def violations(source: str, module: str) -> list[str]:
    found = [
        f"imports {name}"
        for name in imported_modules(source, module)
        if name == CACHE_MODULE or name.startswith(f"{CACHE_MODULE}.")
    ]
    return found + list(mentions(source))


def imports_cache(source: str, module: str) -> bool:
    return bool(violations(source, module))


def importers(root: Path) -> dict[str, list[str]]:
    """The modules under `root` (a copy of src/yizkr) that reach the cache
    module, as paths relative to its parent, with how they do."""
    found = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root.parent)
        module = ".".join(relative.with_suffix("").parts)
        reasons = violations(path.read_text(encoding="utf-8"), module)
        if reasons:
            found[relative.as_posix()] = reasons
    return found


# -- the rule, applied to the package --------------------------------------


def test_only_the_client_factory_imports_the_cache():
    outside = {
        path: reasons
        for path, reasons in importers(SRC).items()
        if path not in ALLOWED
    }
    assert outside == {}, (
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
        # relative names, keyword arguments, other importing functions
        "import importlib\n"
        "importlib.import_module('.llmcache', 'yizkr.pipeline')\n",
        "import importlib\n"
        "importlib.import_module('.llmcache', package=__package__)\n",
        "import importlib\n"
        "importlib.import_module(name='yizkr.pipeline.llmcache')\n",
        "import builtins\nbuiltins.__import__('yizkr.pipeline.llmcache')\n",
        "import importlib\nimportlib.__import__('yizkr.pipeline.llmcache')\n",
        "__import__('yizkr.pipeline', fromlist=['llmcache'])\n",
        "from importlib import import_module as im\n"
        "im('yizkr.pipeline.llmcache')\n",
        # names kept in strings and attributes
        "import importlib\n"
        "NAME = 'yizkr.pipeline.llmcache'\n"
        "importlib.import_module(NAME)\n",
        "import importlib\n"
        "importlib.import_module('yizkr.pipeline.' + 'llmcache')\n",
        "import importlib\n"
        "getattr(importlib, 'import_module')('yizkr.pipeline.llmcache')\n",
        "import sys\nsys.modules['yizkr.pipeline.llmcache'].wrap\n",
        "import pkgutil\n"
        "pkgutil.resolve_name('yizkr.pipeline.llmcache:wrap')\n",
        "import yizkr.pipeline.llm, yizkr.pipeline\n"
        "yizkr.pipeline.llmcache.wrap\n",
        "exec('import yizkr.pipeline.llmcache')\n",
    ],
)
def test_a_planted_import_is_caught(source):
    assert imports_cache(source, PIPELINE_MODULE)


@pytest.mark.parametrize(
    "source, module",
    [
        (
            "import importlib\n"
            "importlib.import_module('.llmcache', package=__package__)\n",
            "yizkr.pipeline.orchestrate",
        ),
        (
            "from importlib import import_module as load\n"
            "load('..pipeline.llmcache', 'yizkr.pipeline')\n",
            "yizkr.cli",
        ),
        (
            "import importlib\n"
            "load = importlib.import_module\n"
            "load(name='.llmcache', package=__package__)\n",
            "yizkr.pipeline.__init__",
        ),
        (
            "__import__('pipeline', globals(), None, ['llmcache'], 1)\n",
            "yizkr.cli",
        ),
        (
            "__import__('yizkr.pipeline', fromlist=('llmcache',))\n",
            "yizkr.cli",
        ),
    ],
)
def test_an_importing_call_is_resolved(source, module):
    """The resolution itself, apart from the string rule: each call
    imports the cache module by the name Python gives it."""
    assert CACHE_MODULE in set(imported_modules(source, module))


@pytest.mark.parametrize(
    "source",
    [
        "from yizkr.pipeline.llm import cached_client, open_cache\n",
        "import yizkr.pipeline.llm\n",
        "from . import llm\n",
        '"""Named in a docstring: yizkr.pipeline.llmcache."""\n',
        'def f():\n    """Named in a docstring: llmcache."""\n',
        "# named in a comment: yizkr.pipeline.llmcache\n",
        "NOTES = 'llmcache_notes'\n",
        "import importlib\n"
        "importlib.import_module('.llm', 'yizkr.pipeline')\n",
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
        target.read_text()
        + "\n\nimport importlib\n\n"
        + "importlib.import_module('.llmcache', package=__package__)\n"
    )
    assert "yizkr/pipeline/orchestrate.py" in importers(tree)
