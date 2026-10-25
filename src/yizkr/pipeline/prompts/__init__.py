"""Packaged Markdown prompts, interpolated with Python's ``str.format``.

Named replacement fields hold runtime inputs; doubled braces represent
literal braces in JSON examples. Callers own separators between fragments.
"""

from importlib.resources import files


def load_prompt(name: str) -> str:
    """Read a UTF-8 prompt beside this module, including its final newline."""
    return files(__package__).joinpath(name).read_text(encoding="utf-8")
