"""Style checks on the lines a candidate diff adds, against packaging/worker-style.md.

Red (exit 1): a test that reads product source, docs, skills or prompt text; a card, sprint, issue
or PR id, a date or an em dash in added prose; an em dash in an added path; a new `X as X` re-export
in `src` that nothing imports. Warnings: the prose share of added code, a new exact copy of a private
helper, a test shaped like an absence check. A line equal to one the diff deletes is moved, not added.

Run from the repository root: `python3 -m scripts.check_style --base-sha <sha> [--candidate-sha <sha>]`.
"""

from __future__ import annotations

import argparse
import ast
import collections
import hashlib
import io
import os
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

from scripts.ci_lint import candidate_base, git, require_candidate

REF = re.compile(
    r"\b(?:secretary|ummanu|codegen[-_]orchestrator)-\d{2,5}\b|\bsprint[: #-]?\d{3,4}\b|"
    r"\bissue:[0-9a-f]{6,}|\bPR ?#\d+|(?<![\w/])#\d{3}\b|\b20\d\d-\d\d-\d\d\b|\b\d\d\.\d\d\.(?:20)?\d\d\b",
    re.IGNORECASE,
)
NAME_REF = re.compile(
    r"(?:secretary|ummanu|codegen_orchestrator|sprint|issue)_?\d{3,5}|20\d\d_\d\d_\d\d", re.IGNORECASE
)
EM_DASH = "\u2014"
ABSENCE = re.compile(
    r"assertFalse\(hasattr\(|find_spec\(|assertRaises\(Module(?:NotFound|Import)Error|"
    r"assertFalse\([^)\n]*(?:ROOT|SRC|src|PACKAGE)[^)\n]*\.exists\(\)|"
    r"assertNotIn\([^\n]*,\s*(?:source|src|text|content|module_text|code)\)|"
    r"assertEqual\(\s*(?:offenders|violations|hits|leaks?|mentions)\s*,\s*(?:\[\]|set\(\)|\{\})|"
    r"assert not hasattr\(|pytest\.raises\(Module(?:NotFound|Import)Error|"
    r"assert not [^\n]*(?:ROOT|SRC|src|PACKAGE)[^\n]*\.exists\(\)|"
    r"assert [^\n]* not in (?:source|src|text|content|module_text|code)\b"
)
GUARD_MARKER = re.compile(r"^\s*#\s*architecture-guard:\s*\S")
REPOSITORY_NAME = re.compile(
    r"^_?(?:[A-Z0-9]+_)*(?:ROOT|REPO|REPOSITORY|SRC|SOURCES?|PACKAGE|PRODUCT|DOCS|SKILLS)(?:_[A-Z0-9]+)*$"
)
SOURCE_NAME = re.compile(r"(?:^|_)(?:SRC|SOURCES?|PACKAGE)(?:_|$)")
SOURCE_INDEX = "tests/source_trees.py"
SRC_PROSE_LIMIT = 0.25
TEST_PROSE_LIMIT = 0.15
MIN_CODE_LINES = 20


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}" if self.path else self.message


@dataclass
class Report:
    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)


@dataclass(frozen=True)
class Source:
    lines: list[str]
    tree: ast.Module | None
    docstrings: frozenset[int]
    comments: dict[int, str]
    inline: frozenset[int]


def is_test(path: str) -> bool:
    return "/tests/" in "/" + path or (
        path.rsplit("/", 1)[-1].startswith("test_") and not path.startswith("src/")
    )


def parse(text: str) -> Source:
    lines = text.splitlines()
    docstrings: set[int] = set()
    comments: dict[int, str] = {}
    inline: set[int] = set()
    try:
        tree: ast.Module | None = ast.parse(text)
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (SyntaxError, tokenize.TokenError):
        return Source(
            lines,
            None,
            frozenset(),
            {n: l for n, l in enumerate(lines, 1) if l.lstrip().startswith("#")},
            frozenset(),
        )
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    for token in tokens:
        if token.type == tokenize.COMMENT:
            row = token.start[0]
            comments[row] = token.string
            if not lines[row - 1].lstrip().startswith("#"):
                inline.add(row)
    return Source(lines, tree, frozenset(docstrings), comments, frozenset(inline))


def diff(root: Path, base: str, head: str) -> tuple[dict[str, set[int]], collections.Counter[str], set[str]]:
    """Added line numbers per path, the stripped text of every deleted line, and the added paths."""
    added: dict[str, set[int]] = {}
    deleted: collections.Counter[str] = collections.Counter()
    path = None
    header = False
    raw = git(
        root,
        "-c",
        "core.quotepath=off",
        "diff",
        "-U0",
        "--no-renames",
        "--no-color",
        "--no-ext-diff",
        base,
        head,
    ).decode(errors="replace")
    for line in raw.splitlines():
        if line.startswith("diff --git "):
            header, path = True, None
        elif header and line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            header = False
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if path and match:
                start, count = int(match.group(1)), int(match.group(2) or 1)
                added.setdefault(path, set()).update(range(start, start + count))
        elif not header and line.startswith("-"):
            deleted[line[1:].strip()] += 1
    new_paths = git(root, "diff", "--name-only", "-z", "--no-renames", "--diff-filter=A", base, head).decode()
    return added, deleted, {name for name in new_paths.split("\0") if name}


def written(source: Source, added: set[int], deleted: collections.Counter[str]) -> set[int]:
    """Added non-blank lines that are not a moved copy of a deleted line."""
    found = set()
    for number in sorted(added):
        if number > len(source.lines) or not source.lines[number - 1].strip():
            continue
        key = source.lines[number - 1].strip()
        if len(key) >= 6 and deleted[key] > 0:
            deleted[key] -= 1
            continue
        found.add(number)
    return found


def prose_findings(path: str, source: Source, lines: set[int]) -> list[Finding]:
    found = []
    for number in sorted(lines):
        text = source.lines[number - 1] if number in source.docstrings else source.comments.get(number, "")
        refs = REF.findall(text)
        if refs:
            found.append(
                Finding(path, number, f"S3 card, sprint, issue or PR id or date in prose: {', '.join(refs)}")
            )
        if EM_DASH in text:
            found.append(Finding(path, number, "em dash in prose"))
    if source.tree is not None:
        for node in ast.walk(source.tree):
            if (
                isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                and node.lineno in lines
                and NAME_REF.search(node.name)
            ):
                found.append(
                    Finding(path, node.lineno, f"S3 card, sprint, issue id or date in the name {node.name}")
                )
    return found


def _names_repository(node: ast.AST) -> bool:
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and (inner.id == "__file__" or REPOSITORY_NAME.match(inner.id)):
            return True
        if isinstance(inner, ast.Attribute) and inner.attr == "__file__":
            return True
        if isinstance(inner, ast.Call) and (_call_name(inner) == "files" or _relative_repository_path(inner)):
            return True
    return _relative_repository_path(node)


def _relative_repository_path(node: ast.AST) -> bool:
    """A path relative to the working directory that names a repository tree, like `Path("docs/x.md")`."""
    if isinstance(node, ast.Call) and _call_name(node) in {"Path", "PurePath"} and node.args:
        node = node.args[0]
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.split("/", 1)[0] in {"docs", "src", "skills", "packaging"}
    )


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return node.func.id if isinstance(node.func, ast.Name) else ""


def _strings(node: ast.AST) -> list[str]:
    return [
        inner.value
        for inner in ast.walk(node)
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str)
    ]


def _is_document(value: str) -> bool:
    lowered = value.lower()
    return (
        lowered.endswith(".md")
        or value in {"docs", "skills"}
        or value.startswith(("docs/", "skills/"))
        or "/docs/" in value
        or "prompt" in lowered
    )


def _read_kind(target: ast.AST, pattern: str = "") -> str:
    """`docs` or `source` when reading `target` reads a repository document or product source."""
    if not _names_repository(target):
        return ""
    strings = [*_strings(target), pattern]
    if any(_is_document(value) for value in strings if value):
        return "docs"
    names = [inner.id for inner in ast.walk(target) if isinstance(inner, ast.Name)]
    if (
        any(value.endswith(".py") or value == "src" or value.startswith("src/") for value in strings if value)
        or any(SOURCE_NAME.search(name) for name in names)
        or (not any(strings) and "__file__" in names)
    ):
        return "source"
    return ""


def _reads(node: ast.AST) -> str:
    """What a node reads as text: `source`, `docs` or nothing."""
    if isinstance(node, ast.Attribute) and node.attr == "getsource":
        return "source"
    if isinstance(node, ast.Name) and node.id == "getsource":
        return "source"
    if isinstance(node, ast.ImportFrom) and node.module == "inspect":
        return "source" if any(alias.name == "getsource" for alias in node.names) else ""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        right = node.right
        if isinstance(right, ast.Constant) and isinstance(right.value, str) and _is_document(right.value):
            return "docs" if _names_repository(node.left) else ""
        return ""
    if not isinstance(node, ast.Call):
        return ""
    name = _call_name(node)
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "source_trees"
    ):
        return "source" if name in {"parse", "modules"} else ""
    if name == "SourceIndex":
        return "source"
    if isinstance(func, ast.Attribute) and name in {"read_text", "read_bytes", "open"}:
        return _read_kind(func.value)
    if name == "open" and node.args:
        return _read_kind(node.args[0])
    if isinstance(func, ast.Attribute) and name in {"rglob", "glob", "iterdir"}:
        pattern = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else ""
        return _read_kind(func.value, str(pattern))
    if (
        name == "walk"
        and isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    ):
        return _read_kind(node.args[0]) if node.args else ""
    return ""


def _guarded_lines(source: Source) -> set[int]:
    """Lines of a statement, class or function that carries the `# architecture-guard:` marker."""
    guarded: set[int] = set()
    if source.tree is None:
        return guarded
    for node in ast.walk(source.tree):
        if not isinstance(node, ast.stmt):
            continue
        decorators = getattr(node, "decorator_list", [])
        start = min([node.lineno, *(decorator.lineno for decorator in decorators)])
        if start >= 2 and GUARD_MARKER.match(source.lines[start - 2]):
            guarded.update(range(start, (node.end_lineno or node.lineno) + 1))
    return guarded


def _with_function(node: ast.AST, function: ast.AST | None) -> list[tuple[ast.AST, ast.AST | None]]:
    """Every node with its innermost enclosing function, so an edit inside an old test is not a new test."""
    found = [(node, function)]
    inner = node if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) else function
    for child in ast.iter_child_nodes(node):
        found += _with_function(child, inner)
    return found


def test_read_findings(path: str, source: Source, lines: set[int]) -> list[Finding]:
    if path == SOURCE_INDEX or source.tree is None:
        return []
    guarded = _guarded_lines(source)
    found = {}
    for node, function in _with_function(source.tree, None):
        number = getattr(node, "lineno", 0)
        if (
            number not in lines
            or number in guarded
            or (function is not None and function.lineno not in lines)
        ):
            continue
        kind = _reads(node)
        if kind == "source":
            found[number] = "S1 test reads product source as text"
        elif kind == "docs" and number not in found:
            found[number] = "S1 test reads docs, skills, prompt or Markdown text"
    return [Finding(path, number, message) for number, message in sorted(found.items())]


def absence_warnings(path: str, source: Source, lines: set[int]) -> list[Finding]:
    found = []
    for node in ast.walk(source.tree) if source.tree is not None else ():
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name.startswith("test")
            and node.lineno in lines
        ):
            body = "\n".join(source.lines[node.lineno - 1 : node.end_lineno])
            if ABSENCE.search(body):
                found.append(Finding(path, node.lineno, f"S1 {node.name} looks like a test of absence"))
    return found


def module_name(path: str) -> str:
    parts = path.removeprefix("src/").removesuffix(".py").split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def reexports(source: Source, lines: set[int]) -> list[tuple[str, int]]:
    return [
        (alias.name, alias.lineno)
        for node in (source.tree.body if source.tree is not None else ())
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
        if alias.asname == alias.name and "." not in alias.name and alias.lineno in lines
    ]


def _imported_module(importer: str, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package = module_name(importer).split(".")
    if not importer.endswith("__init__.py"):
        package = package[:-1]
    package = package[: len(package) - node.level + 1]
    return ".".join([*package, *([node.module] if node.module else [])])


def imports_name(importer: str, text: str, module: str, name: str) -> bool:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    tail = module.rsplit(".", 1)[-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _imported_module(importer, node) == module:
            if any(alias.name == name for alias in node.names):
                return True
        elif isinstance(node, ast.Attribute) and node.attr == name:
            if ast.unparse(node.value).rsplit(".", 1)[-1] == tail:
                return True
        elif isinstance(node, ast.Constant) and node.value == f"{module}.{name}":
            return True
    return False


def tracked_python(root: Path) -> list[str]:
    listed = git(root, "ls-files", "-z", "--", "*.py").decode()
    return sorted(name for name in listed.split("\0") if name and (root / name).is_file())


def _body_digest(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    text = "\n".join(ast.unparse(statement) for statement in body)
    if text.count("\n") < 1:
        return ""
    return hashlib.sha1(re.sub(r"\s+", " ", ast.unparse(node.args) + "\n" + text).encode()).hexdigest()


def private_helpers(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("_")
        and not node.name.startswith("__")
    ]


def merge_base(root: Path, base: str, head: str) -> str:
    """Where the candidate left a commit base, so lines the base gained or lost later are not the diff's."""
    if git(root, "cat-file", "-t", base).decode().strip() != "commit":
        return base
    return git(root, "merge-base", base, head).decode().strip()


def prose_counts(source: Source, lines: set[int]) -> collections.Counter[str]:
    """Code lines, and prose lines: docstring and comment lines plus code lines with an inline comment."""
    counts: collections.Counter[str] = collections.Counter()
    for number in lines:
        if number in source.docstrings or (number in source.comments and number not in source.inline):
            counts["prose"] += 1
        else:
            counts["code"] += 1
            counts["prose"] += number in source.inline
    return counts


def new_helpers(path: str, source: Source, lines: set[int]) -> list[tuple[str, int, str, str]]:
    return [
        (path, node.lineno, node.name, digest)
        for node in (private_helpers(source.tree) if source.tree is not None else ())
        if node.lineno in lines and (digest := _body_digest(node))
    ]


def unimported_reexports(tracked: dict[str, str], candidates: list[tuple[str, int, str]]) -> list[Finding]:
    found = []
    for path, number, name in candidates:
        module = module_name(path)
        if not any(
            other != path and name in text and imports_name(other, text, module, name)
            for other, text in tracked.items()
        ):
            found.append(Finding(path, number, f"S2 re-export {name} as {name} that nothing imports"))
    return found


def check(root: Path, base: str, head: str) -> Report:
    require_candidate(root, base, head)
    added, deleted, new_paths = diff(root, merge_base(root, base, head), head)
    report = Report(
        [Finding(path, 1, "em dash in a file name") for path in sorted(new_paths) if EM_DASH in path]
    )
    counts = {"src": collections.Counter(), "test": collections.Counter()}
    candidates: list[tuple[str, int, str]] = []
    helpers: list[tuple[str, int, str, str]] = []
    for path in sorted(name for name in added if name.endswith(".py")):
        source = parse(git(root, "show", f"{head}:{path}").decode(errors="replace"))
        lines = written(source, added[path], deleted)
        kind = "test" if is_test(path) else "src"
        counts[kind].update(prose_counts(source, lines))
        report.errors += prose_findings(path, source, lines)
        if kind == "test":
            report.errors += test_read_findings(path, source, lines)
            report.warnings += absence_warnings(path, source, lines)
            continue
        if path.startswith("src/"):
            candidates += [(path, number, name) for name, number in reexports(source, lines)]
        helpers += new_helpers(path, source, lines)
    if candidates or helpers:
        tracked = {
            path: (root / path).read_text(encoding="utf-8", errors="replace") for path in tracked_python(root)
        }
        report.errors += unimported_reexports(tracked, candidates)
        report.warnings += duplicate_helpers(tracked, helpers)
    for kind, limit in (("src", SRC_PROSE_LIMIT), ("test", TEST_PROSE_LIMIT)):
        code, prose = counts[kind]["code"], counts[kind]["prose"]
        if code >= MIN_CODE_LINES and prose / code > limit:
            share = f"{prose / code:.2f} > {limit} ({prose} prose, {code} code)"
            report.warnings.append(Finding("", 0, f"S5 prose share of added {kind} lines {share}"))
    return report


def duplicate_helpers(tracked: dict[str, str], helpers: list[tuple[str, int, str, str]]) -> list[Finding]:
    wanted = {digest for *_, digest in helpers}
    originals: dict[str, list[str]] = collections.defaultdict(list)
    for path, text in tracked.items():
        if is_test(path):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in private_helpers(tree):
            digest = _body_digest(node)
            if digest in wanted:
                originals[digest].append(f"{path}:{node.lineno}")
    found = []
    for path, number, name, digest in helpers:
        others = [where for where in originals[digest] if where != f"{path}:{number}"]
        if others:
            found.append(Finding(path, number, f"S4 private helper {name} duplicates {', '.join(others)}"))
    return found


def publish(report: Report) -> None:
    actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for level, findings in (("error", report.errors), ("warning", report.warnings)):
        for finding in findings:
            if actions:
                where = f" file={finding.path},line={finding.line}," if finding.path else " "
                print(f"::{level}{where}title=style::{finding.message}")
            else:
                print(f"{level}: {finding}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and (report.errors or report.warnings):
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("### Style of added lines\n\n")
            stream.writelines(
                f"- {level}: `{finding}`\n"
                for level, findings in (("error", report.errors), ("warning", report.warnings))
                for finding in findings
            )
    print(f"Style: {len(report.errors)} errors, {len(report.warnings)} warnings.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--candidate-sha", default="")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        head = args.candidate_sha or git(root, "rev-parse", "HEAD").decode().strip()
        report = check(root, candidate_base(root, args.base_sha, head), head)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Cannot run the style check: {exc}", file=sys.stderr)
        return 1
    publish(report)
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
