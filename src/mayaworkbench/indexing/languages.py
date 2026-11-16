"""Language and file-role classification.

Phase 1 parses Python with the standard library AST. Everything else is indexed
lexically and labelled as such: the repository map reports parser coverage
explicitly so no one mistakes lexical search for semantic understanding (§8.3).
"""

from __future__ import annotations

from pathlib import PurePosixPath

EXTENSION_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".pyx": "cython",
    ".ipynb": "notebook",
    ".md": "markdown",
    ".markdown": "markdown",
    ".rst": "rst",
    ".txt": "text",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".jsonl": "jsonl",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "config",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".ps1": "powershell",
    ".bat": "batch",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".vue": "vue",
    ".svelte": "svelte",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".scala": "scala",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".m": "objective-c",
    ".mm": "objective-c",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".r": "r",
    ".jl": "julia",
    ".lua": "lua",
    ".pl": "perl",
    ".sql": "sql",
    ".proto": "protobuf",
    ".cu": "cuda",
    ".cuh": "cuda",
    ".cmake": "cmake",
    ".dockerfile": "dockerfile",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".xml": "xml",
    ".svg": "svg",
}

FILENAME_LANGUAGES: dict[str, str] = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
    "cmakelists.txt": "cmake",
    "requirements.txt": "requirements",
    "pyproject.toml": "toml",
    "setup.py": "python",
    "setup.cfg": "ini",
    "pipfile": "toml",
    "poetry.lock": "lockfile",
    "uv.lock": "lockfile",
    "environment.yml": "yaml",
    "environment.yaml": "yaml",
    ".gitignore": "ignore",
    ".dockerignore": "ignore",
    ".gitattributes": "config",
    "justfile": "makefile",
}

LANGUAGES_WITH_AST: frozenset[str] = frozenset({"python"})

CONFIG_EXTENSIONS = {
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".jsonl",
    ".ini",
    ".cfg",
    ".conf",
    ".lock",
}
CONFIG_FILENAMES = {
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "pipfile",
    "requirements.txt",
    "environment.yml",
    "environment.yaml",
    "dockerfile",
    "makefile",
    "cmakelists.txt",
    "tox.ini",
    "mypy.ini",
    "pytest.ini",
    ".flake8",
    "noxfile.py",
}
DOC_EXTENSIONS = {".md", ".markdown", ".rst", ".txt"}
DOC_DIR_NAMES = {"docs", "doc", "documentation"}
TEST_DIR_NAMES = {"tests", "test", "testing", "__tests__", "spec"}
TEST_FILENAME_PREFIXES = ("test_",)
TEST_FILENAME_SUFFIXES = ("_test.py", "_test.go", ".test.js", ".test.ts", ".spec.js", ".spec.ts")


def detect_language(path: str) -> str:
    pure = PurePosixPath(path)
    name = pure.name.lower()
    if name in FILENAME_LANGUAGES:
        return FILENAME_LANGUAGES[name]
    suffix = pure.suffix.lower()
    return EXTENSION_LANGUAGES.get(suffix, "unknown")


def is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    parts = [part.lower() for part in pure.parts[:-1]]
    name = pure.name.lower()
    if any(part in TEST_DIR_NAMES for part in parts):
        return True
    if name == "conftest.py":
        return True
    if name.startswith(TEST_FILENAME_PREFIXES):
        return True
    return any(name.endswith(suffix) for suffix in TEST_FILENAME_SUFFIXES)


def is_doc_path(path: str) -> bool:
    pure = PurePosixPath(path)
    parts = [part.lower() for part in pure.parts[:-1]]
    if any(part in DOC_DIR_NAMES for part in parts):
        return True
    return pure.suffix.lower() in DOC_EXTENSIONS


def is_config_path(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name.lower()
    if name in CONFIG_FILENAMES:
        return True
    return pure.suffix.lower() in CONFIG_EXTENSIONS


def module_name_for_path(path: str) -> str:
    """Best-effort dotted module name from a repository-relative path.

    The repository root is not necessarily the Python import root (``src/``
    layouts are common), so resolution additionally tries suffix forms.
    """
    pure = PurePosixPath(path)
    without_suffix = pure.with_suffix("")
    parts = list(without_suffix.parts)
    if not parts:
        return ""
    if parts[-1] == "__init__":
        parts = parts[:-1]
    # ``src/pkg/module.py`` imports as ``pkg.module``; leaving the source root
    # in the name breaks absolute imports and pollutes symbol names.
    if len(parts) > 1 and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


def coverage_for(language: str, parse_failed: bool = False) -> str:
    if language in LANGUAGES_WITH_AST and not parse_failed:
        return "FULL"
    if parse_failed:
        return "PARTIAL"
    return "LEXICAL_ONLY"


__all__ = [
    "detect_language",
    "is_test_path",
    "is_doc_path",
    "is_config_path",
    "module_name_for_path",
    "LANGUAGES_WITH_AST",
    "coverage_for",
]
