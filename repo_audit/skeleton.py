"""Stage 0: the deterministic skeleton. No model calls.

Walks the repository and records what can be known for certain: which files
exist, which import which, where the entry points are, which tables, files
and environment variables each file touches, which code is unreachable, and
which code is duplicated. The agent waves refine this map; they never have
to invent it.

To support another language, write a function with the signature of
`analyze_python` (fill `rec["imports"]`, `rec["io"]`, `rec["defs"]`,
`rec["entry"]`) and register it in ANALYZERS.
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import sys
import warnings
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from . import evidence
from .names import norm_entity

LANG_BY_EXT = {
    ".py": "python", ".ipynb": "notebook", ".sql": "sql", ".hql": "sql",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".sh": "shell", ".bash": "shell",
    ".r": "r", ".java": "java", ".scala": "scala", ".go": "go", ".rb": "ruby",
    ".php": "php", ".kt": "kotlin", ".cs": "csharp", ".cpp": "cpp", ".cc": "cpp",
    ".c": "c", ".h": "c", ".rs": "rust", ".groovy": "groovy",
    ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".json": "json",
    ".cfg": "ini", ".ini": "ini", ".tf": "terraform",
    ".md": "markdown", ".rst": "markdown", ".txt": "text", ".tex": "tex",
    # Not analyzed, but scanned for references to other files (script tags, includes).
    ".html": "markup", ".htm": "markup", ".jinja": "markup", ".j2": "markup",
    ".jinja2": "markup", ".xml": "markup", ".tpl": "markup",
}
LANG_BY_NAME = {"dockerfile": "docker", "makefile": "make", "jenkinsfile": "groovy"}
CODE_LANGS = {"python", "notebook", "sql", "javascript", "typescript", "shell", "r", "java",
              "scala", "go", "ruby", "php", "kotlin", "csharp", "cpp", "c", "rust", "groovy"}
GLUE_LANGS = {"yaml", "toml", "json", "ini", "docker", "make", "terraform"}
# Languages where "nothing imports or references this file" can be computed.
GRAPH_LANGS = {"python", "javascript", "typescript", "sql"}
# Directories whose files are discovered by a framework, not imported by name.
CONVENTION_DIRS = {"migrations", "versions"}
SPECIAL_NAMES = {"__init__.py", "__main__.py", "setup.py", "conftest.py", "manage.py",
                 "wsgi.py", "asgi.py", "settings.py"}
PRUNE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache",
              ".pytest_cache", ".repo-audit"}
TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "testing", "cypress", "e2e",
             "fixtures", "__fixtures__", "__mocks__", "stories"}
# Tool configuration loaded by name, not by import.
_CONVENTION_FILE = re.compile(
    r"(?:^|/)(?:[\w.-]+\.config|Gruntfile|gulpfile|sidebars|\.eslintrc|\.prettierrc)\.(?:js|cjs|mjs|ts)$")
JS_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
STDLIB = set(getattr(sys, "stdlib_module_names", ()))


# --------------------------------------------------------------------------
# file listing
# --------------------------------------------------------------------------
def _compile_patterns(patterns: list[str]) -> list[str]:
    out = []
    for p in patterns:
        p = p.replace("**", "*")
        out.append(p if p.startswith("*") else "/" + p.lstrip("/"))
    return out


def _excluded(rel: str, patterns: list[str]) -> bool:
    target = "/" + rel
    return any(fnmatchcase(target, p) for p in patterns)


def list_files(repo: Path, cfg: dict) -> list[str]:
    rels: list[str] | None = None
    if cfg["repo"]["use_git_ls_files"] and (repo / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "ls-files", "-co", "--exclude-standard", "-z"],
                capture_output=True, timeout=180)
            if out.returncode == 0:
                rels = [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]
        except (OSError, subprocess.SubprocessError):
            rels = None
    if rels is None:
        rels = []
        for root, dirs, names in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in PRUNE_DIRS]
            for name in names:
                rels.append(Path(root, name).relative_to(repo).as_posix())
    patterns = _compile_patterns(cfg["repo"]["exclude"])
    out_dir = Path(cfg["output"]["dir"])
    for inside in (out_dir, Path(cfg["baseline"]["docs_dir"] or "/nonexistent")):
        try:  # the tool's own output and the baseline docs are not part of the audit target
            patterns.append("/" + inside.relative_to(repo).as_posix() + "/*")
        except ValueError:
            pass
    keep = []
    for rel in rels:
        p = repo / rel
        if _excluded(rel, patterns) or p.is_symlink() or not p.is_file():
            continue
        keep.append(rel)
    return sorted(set(keep))


def _lang(rel: str) -> str:
    p = PurePosixPath(rel)
    return LANG_BY_NAME.get(p.name.lower()) or LANG_BY_EXT.get(p.suffix.lower(), "other")


def _role(rel: str, lang: str) -> str:
    p = PurePosixPath(rel)
    name = p.name.lower()
    dirs = {part.lower() for part in p.parts[:-1]}
    if lang in CODE_LANGS:
        if (dirs & TEST_DIRS or name.startswith("test_") or name == "conftest.py"
                or re.search(r"(_test|\.test|\.spec|\.stories|\.d)\.[a-z]+$", name)):
            return "test"
        return "code"
    if lang in GLUE_LANGS:
        return "config"
    if lang in ("markdown", "text", "tex"):
        return "doc"
    return "other"


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------
_PART = r"(?:[A-Za-z_][\w$]*|\{\})"
_ID = r"[`\"\[]?(" + _PART + r"(?:\." + _PART + r"){0,3})[`\"\]]?"
_SQL_HINT = re.compile(
    # "select *" or "select col," or "select col from": prose rarely looks like this
    r"\bselect\s+(?:distinct\s+)?(?:\*|[\w.`\"\[\]{}()]+(?:\s+as\s+\w+)?\s*(?:,|\bfrom\b))"
    r"|\binsert\s+(?:into|overwrite)\b|\bmerge\s+into\b|\bdelete\s+from\b"
    r"|\bcreate\s+(?:or\s+replace\s+)?(?:temp(?:orary)?\s+)?(?:external\s+)?(?:table|view)\b"
    r"|\bupdate\s+\S+\s+set\b", re.I | re.S)
_SQL_READ = re.compile(r"\b(?:from|join)\s+" + _ID, re.I)
_SQL_WRITE = re.compile(
    r"\b(?:insert\s+(?:into|overwrite)(?:\s+table)?|merge\s+into|delete_from|truncate\s+table"
    r"|create\s+(?:or\s+replace\s+)?(?:temp(?:orary)?\s+)?(?:external\s+)?(?:table|view)"
    r"(?:\s+if\s+not\s+exists)?|update(?=\s+\S+\s+set\b))\s+" + _ID, re.I)
_CTE = re.compile(r"(?:\bwith|,)\s+([A-Za-z_]\w*)\s+as\s*\(", re.I)
_DELETE_FROM = re.compile(r"\bdelete\s+from\b", re.I)
_SQL_STOP = {"select", "unnest", "lateral", "dual", "values", "table", "the", "a", "an",
             "set", "where", "generate_series", "{}", "all", "any", "each", "some", "this",
             "that", "these", "those", "which", "it", "them", "here", "there", "one", "your",
             "my", "our", "their", "its", "both", "either", "list", "among", "now", "to"}


def sql_tables(text: str, require_hint: bool = True) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Return ([(table, offset)] read, [(table, offset)] written) for SQL-looking text."""
    if require_hint and not _SQL_HINT.search(text):
        return [], []
    text = _DELETE_FROM.sub(lambda m: "delete_from".ljust(len(m.group(0))), text)
    ctes = {m.group(1).lower() for m in _CTE.finditer(text)}

    def ok(name: str) -> bool:
        low = name.lower()
        return low not in _SQL_STOP and low not in ctes and not low.isdigit()

    reads = [(m.group(1), m.start()) for m in _SQL_READ.finditer(text) if ok(m.group(1))]
    writes = [(m.group(1), m.start()) for m in _SQL_WRITE.finditer(text) if ok(m.group(1))]
    return reads, writes


def _strip_sql_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    return re.sub(r"--[^\n]*", lambda m: " " * len(m.group(0)), text)


def analyze_sql(rel: str, text: str, rec: dict, env: dict) -> None:
    clean = _strip_sql_comments(text)
    reads, writes = sql_tables(clean, require_hint=False)
    seen = set()
    for direction, items in (("write", writes), ("read", reads)):
        for name, offset in items:
            if (direction, name.lower()) in seen:
                continue
            seen.add((direction, name.lower()))
            _add_io(rec, direction, "table", name, clean.count("\n", 0, offset) + 1)


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------
READ_FUNCS = {"read_csv", "read_parquet", "read_json", "read_excel", "read_sql", "read_sql_query",
              "read_sql_table", "read_table", "read_feather", "read_pickle", "read_orc",
              "read_hdf", "read_stata", "loadtxt", "genfromtxt", "loadmat"}
WRITE_FUNCS = {"to_csv", "to_parquet", "to_json", "to_excel", "to_sql", "to_pickle", "to_feather",
               "to_orc", "to_hdf", "to_stata", "to_latex", "savetxt", "savefig", "savez",
               "savez_compressed", "savemat"}
# np.save / np.load / joblib.dump: only when called on a module known to do file I/O
IO_MODULES = {"np", "numpy", "joblib", "torch"}
# pathlib methods: the path is the receiver, not the argument
PATH_METHODS = {"read_text": "read", "read_bytes": "read", "write_text": "write", "write_bytes": "write"}
SPARK_FORMATS = {"parquet", "csv", "json", "orc", "text", "load", "save", "jdbc", "delta"}
HTTP_VERBS = {"get", "post", "put", "delete", "patch", "request"}


def _add_io(rec: dict, direction: str, kind: str, name: str | None, line: int) -> None:
    entity = None
    if name and kind in ("table", "file", "env", "api"):
        if kind == "file":
            name = re.sub(r"/+", "/", name.replace("\\", "/"))
            name = re.sub(r"^(?:\{\}/|\.\./|\./)+", "", name)
        looks_like_path = (" " not in name.strip() and len(name) <= 200
                           and re.search(r"[./]", name) and re.search(r"[A-Za-z0-9]", name))
        if kind != "file" or looks_like_path:
            entity = norm_entity(f"{kind}:{name}")
    rec["io"].append({"dir": direction, "kind": kind, "name": name or "<dynamic>",
                      "entity": entity, "line": line})


def _chain(node) -> list[str]:
    out: list[str] = []
    while True:
        if isinstance(node, ast.Attribute):
            out.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Name):
            out.append(node.id)
            break
        else:
            break
    return out[::-1]


def _str(node, consts: dict) -> str | None:
    """Best-effort value of a string expression; unknown parts become "{}"."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
                       for v in node.values)
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.Attribute):          # config.DATA_DIR, self.out_dir
        return consts.get(node.attr)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
        left, right = _str(node.left, consts), _str(node.right, consts)
        if left is None and right is None:
            return None
        sep = "/" if isinstance(node.op, ast.Div) else ""
        return (left if left is not None else "{}") + sep + (right if right is not None else "{}")
    if isinstance(node, ast.Call):
        chain = _chain(node.func)
        if chain[-2:] == ["path", "join"] or chain[-1:] == ["joinpath"]:
            parts = [_str(a, consts) for a in node.args]
            if chain[-1] == "joinpath" and isinstance(node.func, ast.Attribute):
                parts.insert(0, _str(node.func.value, consts))
            if all(p is None for p in parts):
                return None
            return "/".join((p if p is not None else "{}").strip("/") or "{}" for p in parts)
        if chain[-1:] in (["Path"], ["str"], ["abspath"], ["expanduser"], ["resolve"], ["normpath"]):
            if node.args:
                return _str(node.args[0], consts)
            if isinstance(node.func, ast.Attribute):
                return _str(node.func.value, consts)
        if chain[-1:] == ["format"] and isinstance(node.func, ast.Attribute):
            base = _str(node.func.value, consts)
            return re.sub(r"\{[^{}]*\}", "{}", base) if base else None
    return None


def _collect_consts(tree, shared: dict, ambiguous: set | None = None) -> dict:
    """Names assigned exactly one resolvable string value anywhere in the module.

    Flow-insensitive on purpose: `path = os.path.join(OUT, "x.csv")` followed by
    `df.to_csv(path)` is the common case, and a name with two different values
    is simply left unresolved.
    """
    consts = dict(shared)
    for _ in range(3):  # let chains settle: A = "x"; B = join(A, "y"); C = B + ".csv"
        seen: dict[str, set] = defaultdict(set)
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            for target in targets:
                name = target.id if isinstance(target, ast.Name) else (
                    target.attr if isinstance(target, ast.Attribute) else None)
                if name:
                    seen[name].add(_str(value, consts))
        changed = False
        if ambiguous is not None:
            ambiguous.clear()
            ambiguous.update(n for n, v in seen.items() if len(v) > 1 or None in v)
        for name, values in seen.items():
            if len(values) == 1 and None not in values:
                value = next(iter(values))
                if consts.get(name) != value and len(value) <= 300:
                    consts[name] = value
                    changed = True
        if not changed:
            break
    return consts


def _py_strings(tree):
    """Yield (text, line) for string literals, leaving out docstrings (prose, not SQL)."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    stack = [tree]
    while stack:
        node = stack.pop()
        if id(node) in docstrings:
            continue
        if isinstance(node, ast.JoinedStr):
            yield _str(node, {}), node.lineno
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value, node.lineno
            continue
        stack.extend(ast.iter_child_nodes(node))


def _is_main_guard(node) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    parts = [node.test.left] + list(node.test.comparators)
    names = [p.id for p in parts if isinstance(p, ast.Name)]
    consts = [p.value for p in parts if isinstance(p, ast.Constant)]
    return "__name__" in names and "__main__" in consts


def _resolve_py(env: dict, importer: str, dotted: str) -> str | None:
    cands = env["mod_index"].get(dotted)
    if not cands:
        return None
    as_path = dotted.replace(".", "/")
    exact = {as_path + ".py", as_path + "/__init__.py",
             "src/" + as_path + ".py", "src/" + as_path + "/__init__.py"}
    for c in cands:
        if c in exact:
            return c
    if "." not in dotted and dotted in STDLIB:
        return None  # `import json` should not resolve to some/dir/json.py
    if len(cands) == 1:
        return cands[0]
    importer_parts = PurePosixPath(importer).parts

    def shared(c: str) -> int:
        n = 0
        for a, b in zip(PurePosixPath(c).parts, importer_parts):
            if a != b:
                break
            n += 1
        return n

    return max(sorted(cands), key=lambda c: (shared(c), -len(c)))


_PY_IMPORT_FALLBACK = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M)


def analyze_python(rel: str, text: str, rec: dict, env: dict) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        # Python 2 or broken file: keep what a regex can see so the import
        # graph does not lose its edges.
        rec["parse_error"] = True
        for m in _PY_IMPORT_FALLBACK.finditer(text):
            target = _resolve_py(env, rel, m.group(1) or m.group(2))
            if target and target != rel:
                rec["imports"].add(target)
        rec["entry"] = "__main__" in text and "__name__" in text
        return

    consts = _collect_consts(tree, env.get("shared_consts", {}))
    for node in tree.body:
        if _is_main_guard(node):
            rec["entry"] = True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            rec["defs"].append({"name": node.name,
                                "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                                "line": node.lineno, "end": node.end_lineno})

    pkg = list(PurePosixPath(rel).parts[:-1])

    def link(dotted: str, climb: bool = True) -> bool:
        parts = dotted.split(".")
        while parts:
            target = _resolve_py(env, rel, ".".join(parts))
            if target:
                if target != rel:
                    rec["imports"].add(target)
                return True
            if not climb:
                return False
            parts = parts[:-1]
        return False

    min_lines = env["cfg"]["limits"]["dup_function_min_lines"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not link(alias.name):
                    rec["external"].add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                up = node.level - 1
                base = pkg[:len(pkg) - up] if up <= len(pkg) else []
                prefix = ".".join(base + (node.module.split(".") if node.module else []))
            else:
                prefix = node.module or ""
            found = link(prefix) if prefix else False
            for alias in node.names:
                if alias.name != "*":
                    found = link((prefix + "." if prefix else "") + alias.name, climb=False) or found
            if not found and not node.level and prefix:
                rec["external"].add(prefix.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            if rec["role"] == "code" and body and (node.end_lineno - body[0].lineno + 1) >= min_lines:
                digest = hashlib.sha1("".join(ast.dump(b) for b in body).encode()).hexdigest()
                env["func_hashes"][digest].append(
                    {"path": rel, "name": node.name, "line": node.lineno, "end": node.end_lineno})
        elif isinstance(node, ast.Subscript):
            if _chain(node.value) == ["os", "environ"]:
                name = _str(node.slice, consts)
                if name:
                    _add_io(rec, "read", "env", name, node.lineno)
        elif isinstance(node, ast.Call):
            _py_call(node, rec, consts, link)

    for text_value, line in _py_strings(tree):
        if not text_value or len(text_value) < 12:
            continue
        reads, writes = sql_tables(text_value)
        for direction, items in (("write", writes), ("read", reads)):
            for name in dict.fromkeys(n for n, _ in items):
                _add_io(rec, direction, "table", name, line)


def _py_call(node: ast.Call, rec: dict, consts: dict, link) -> None:
    chain = _chain(node.func)
    if not chain:
        return
    tail, root, line = chain[-1], chain[0].lower(), node.lineno
    arg0 = _str(node.args[0], consts) if node.args else None

    if tail == "import_module" or chain == ["__import__"]:
        if arg0:
            link(arg0)
        else:
            rec["dynamic_imports"] = True
    elif chain == ["open"] or chain[-2:] == ["io", "open"]:
        mode = _str(node.args[1], consts) if len(node.args) > 1 else None
        for kw in node.keywords:
            if kw.arg == "mode":
                mode = _str(kw.value, consts)
        writing = bool(mode) and any(c in mode for c in "wax+")
        _add_io(rec, "write" if writing else "read", "file", arg0, line)
    elif tail == "table" and ("spark" in root or "read" in chain or "hive" in root or "session" in root):
        _add_io(rec, "read", "table", arg0, line)
    elif tail in ("saveAsTable", "insertInto"):
        _add_io(rec, "write", "table", arg0, line)
    elif tail in ("read_sql", "read_sql_query"):
        if arg0 and " " not in arg0.strip():
            _add_io(rec, "read", "table", arg0, line)
    elif tail == "read_sql_table":
        _add_io(rec, "read", "table", arg0, line)
    elif tail == "to_sql":
        _add_io(rec, "write", "table", arg0, line)
    elif tail in PATH_METHODS and isinstance(node.func, ast.Attribute):
        receiver = node.func.value
        target = None
        if isinstance(receiver, ast.Call) and _chain(receiver.func)[-1:] == ["Path"] and receiver.args:
            target = _str(receiver.args[0], consts)
        _add_io(rec, PATH_METHODS[tail], "file", target, line)
    elif root in IO_MODULES and tail in ("load", "save", "dump"):
        target = arg0 if tail != "dump" else (_str(node.args[1], consts) if len(node.args) > 1 else None)
        _add_io(rec, "read" if tail == "load" else "write", "file", target, line)
    elif tail in READ_FUNCS:
        _add_io(rec, "read", "file", arg0, line)
    elif tail in WRITE_FUNCS:
        if arg0 is None and tail != "savefig":
            for kw in node.keywords:  # df.to_csv(path_or_buf=...)
                if kw.arg in ("path", "path_or_buf", "fname", "file"):
                    arg0 = _str(kw.value, consts)
        _add_io(rec, "write", "file", arg0, line)
    elif tail in SPARK_FORMATS and "read" in chain[:-1]:
        _add_io(rec, "read", "file", arg0, line)
    elif tail in SPARK_FORMATS and "write" in chain[:-1]:
        _add_io(rec, "write", "file", arg0, line)
    elif (tail == "getenv" and "os" in chain) or chain[-3:] == ["os", "environ", "get"]:
        if arg0:
            _add_io(rec, "read", "env", arg0, line)
    elif tail == "add_argument" or (tail in ("option", "argument") and root == "click"):
        flags = [s for s in (_str(a, consts) for a in node.args) if s]
        if flags:
            _add_io(rec, "read", "cli_arg", max(flags, key=len), line)
    elif root in ("requests", "httpx") and tail in HTTP_VERBS:
        _add_io(rec, "unknown", "api", arg0, line)


def analyze_notebook(rel: str, text: str, rec: dict, env: dict) -> None:
    analyze_python(rel, text, rec, env)
    rec["entry"] = True  # notebooks are run by people, not imported


# --------------------------------------------------------------------------
# JavaScript / TypeScript
# --------------------------------------------------------------------------
_JS_IMPORT = re.compile(
    r"""(?:\bimport\s+(?:[\w*{}\s,$]+\s+from\s+)?|\bexport\s+[\w*{}\s,$]+\s+from\s+"""
    r"""|\brequire\s*\(\s*|\bimport\s*\(\s*)['"]([^'"\n]+)['"]""")


def analyze_js(rel: str, text: str, rec: dict, env: dict) -> None:
    files = env["files"]
    base = PurePosixPath(rel).parent
    for m in _JS_IMPORT.finditer(text):
        spec = m.group(1)
        if not spec.startswith("."):
            # path aliases (@/x/y, ~/x/y, @site/src/x) and root-relative imports
            bare = re.sub(r"\.(?:jsx?|tsx?|mjs|cjs)$", "", spec).rstrip("/")
            for key in (bare, re.sub(r"^(?:@[\w-]*/|~/)", "", bare)):
                cands = set(env["js_index"].get(key, ())) if "/" in key else set()
                if len(cands) == 1:
                    target = cands.pop()
                    if target != rel:
                        rec["imports"].add(target)
                    break
            else:
                rec["external"].add(spec.split("/")[0])
            continue
        target = os.path.normpath(str(base / spec)).replace("\\", "/")
        options = [target] + [target + e for e in JS_EXTS] + [target + "/index" + e for e in JS_EXTS]
        for option in options:
            if option in files and option != rel:
                rec["imports"].add(option)
                break


ANALYZERS = {"python": analyze_python, "notebook": analyze_notebook, "sql": analyze_sql,
             "javascript": analyze_js, "typescript": analyze_js}


# --------------------------------------------------------------------------
# references by path or module name (scripts, CI files, Dockerfiles, docs)
# --------------------------------------------------------------------------
_PATH_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./\\-]*\.[A-Za-z0-9]{1,6}\b")
_DOTTED = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b")
_DASH_M = re.compile(r"-m\s+([A-Za-z_][\w.]*)")


def _scan_references(rel: str, text: str, rec: dict, env: dict) -> None:
    base_index = env["base_index"]
    for m in _PATH_TOKEN.finditer(text):
        token = m.group(0).replace("\\", "/")
        while token.startswith("./"):
            token = token[2:]
        cands = base_index.get(token.rsplit("/", 1)[-1])
        if not cands:
            continue
        if "/" in token:
            for c in cands:
                if c != rel and (c == token or c.endswith("/" + token) or token.endswith("/" + c)):
                    rec["references"].add(c)
        elif len(cands) == 1 and cands[0] != rel:
            rec["references"].add(cands[0])
    if rec["lang"] in GLUE_LANGS or rec["lang"] in ("shell", "groovy"):
        # pkg.mod or pkg.mod.func, as in entry-point tables and scheduler configs
        for m in _DOTTED.finditer(text):
            parts = m.group(0).split(".")
            for size in range(len(parts), 1, -1):
                cands = env["mod_index"].get(".".join(parts[:size]), [])
                if len(cands) == 1 and cands[0] != rel:
                    rec["references"].add(cands[0])
                    break
        # python -m module
        for m in _DASH_M.finditer(text):
            cands = env["mod_index"].get(m.group(1), [])
            if len(cands) == 1 and cands[0] != rel:
                rec["references"].add(cands[0])


# --------------------------------------------------------------------------
# git history
# --------------------------------------------------------------------------
def git_stats(repo: Path, inactive_months: int) -> tuple[str, dict]:
    """Per-file churn. Author names are never stored, only counts and flags."""
    if not (repo / ".git").exists():
        return "", {}
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30).stdout.strip()
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--no-merges", "--name-only",
             "--format=%x01%ae%x02%aI"],
            capture_output=True, timeout=300).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return "", {}
    per_file: dict = defaultdict(lambda: {"commits": 0, "authors": set(), "last": ""})
    author_last: dict = {}
    author = date = ""
    for line in log.splitlines():
        if line.startswith("\x01"):
            author, _, date = line[1:].partition("\x02")
            if date > author_last.get(author, ""):
                author_last[author] = date
        elif line.strip():
            entry = per_file[line.strip()]
            entry["commits"] += 1
            entry["authors"].add(author)
            if date > entry["last"]:
                entry["last"] = date
    if not author_last:
        return head, {}
    try:
        newest = datetime.fromisoformat(max(author_last.values()))
        cutoff = (newest - timedelta(days=30 * inactive_months)).isoformat()
    except ValueError:
        cutoff = ""
    active = {a for a, d in author_last.items() if d >= cutoff}
    out = {}
    for path, entry in per_file.items():
        out[path] = {"commits": entry["commits"], "authors": len(entry["authors"]),
                     "last": entry["last"][:10],
                     "all_authors_inactive": not (entry["authors"] & active)}
    return head, out


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
def build(cfg: dict, log=print) -> dict:
    repo = Path(cfg["repo"]["path"])
    if not repo.is_dir():
        raise FileNotFoundError(f"repository not found: {repo}")
    rels = list_files(repo, cfg)
    files: dict[str, dict] = {}
    for rel in rels:
        lang = _lang(rel)
        size = (repo / rel).stat().st_size
        skipped = None
        if size > cfg["repo"]["max_file_bytes"]:
            skipped = "too large"
        elif lang == "other":
            skipped = "unrecognized type"
        files[rel] = {"lang": lang, "role": _role(rel, lang), "bytes": size, "lines": 0,
                      "sha": "", "skipped": skipped, "entry": False, "parse_error": False,
                      "dynamic_imports": False, "imports": set(), "references": set(),
                      "external": set(), "defs": [], "io": []}

    mod_index: dict[str, list[str]] = defaultdict(list)
    base_index: dict[str, list[str]] = defaultdict(list)
    for rel, rec in files.items():
        base_index[PurePosixPath(rel).name].append(rel)
        if rec["lang"] == "python":
            parts = list(PurePosixPath(rel).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            for i in range(len(parts)):
                mod_index[".".join(parts[i:])].append(rel)

    js_index: dict[str, list[str]] = defaultdict(list)
    for rel, rec in files.items():
        if rec["lang"] in ("javascript", "typescript"):
            parts = list(PurePosixPath(rel).with_suffix("").parts)
            if parts[-1] == "index" and len(parts) > 1:
                parts = parts[:-1]
            for i in range(len(parts) - 1):
                js_index["/".join(parts[i:])].append(rel)

    # UPPER_CASE string constants defined once across the repository (config.DATA_DIR and
    # the like) are visible everywhere, so paths built from them resolve in other files.
    # A name is shared only if every file that assigns it gives it the same, fully known
    # value. BASE = Path(__file__).parent in ten files and BASE = "out" in one is not a
    # constant, and treating it as one puts the wrong directory on every path.
    shared_seen: dict[str, set] = defaultdict(set)
    not_shared: set[str] = set()
    for rel, rec in files.items():
        if rec["lang"] != "python" or rec["skipped"] or rec["role"] == "test":
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(evidence.load_text(repo, rel, do_redact=False))
        except Exception:
            continue
        unsure: set[str] = set()
        top = ast.Module(body=tree.body, type_ignores=[])
        for name, value in _collect_consts(top, {}, unsure).items():
            if name.isupper():
                shared_seen[name].add(value)
        not_shared |= {n for n in unsure if n.isupper()}
    shared_consts = {n: next(iter(v)) for n, v in shared_seen.items()
                     if len(v) == 1 and n not in not_shared}

    env = {"cfg": cfg, "files": files, "mod_index": mod_index, "base_index": base_index,
           "js_index": js_index, "func_hashes": defaultdict(list), "shared_consts": shared_consts}
    line_sets: dict[str, set] = {}
    for rel, rec in files.items():
        if rec["skipped"]:
            continue
        try:
            text = evidence.load_text(repo, rel, do_redact=False)
        except OSError:
            rec["skipped"] = "unreadable"
            continue
        if "\x00" in text[:4000]:
            rec["skipped"] = "binary"
            continue
        rec["lines"] = text.count("\n") + 1
        rec["sha"] = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
        analyzer = ANALYZERS.get(rec["lang"])
        if analyzer:
            try:
                analyzer(rel, text, rec, env)
            except Exception as exc:  # one odd file must not stop the audit
                rec["parse_error"] = True
                log(f"  analyzer failed on {rel}: {type(exc).__name__}: {exc}")
        _scan_references(rel, text, rec, env)
        if rec["role"] in ("code", "test") and rec["lang"] != "sql":
            hashes = set()
            for line in text.splitlines():
                s = line.strip()
                if len(s) >= 12 and not s.startswith(("#", "//", "*", "/*", "--", "import ", "from ")):
                    hashes.add(hash(s))
            if len(hashes) >= 15:
                line_sets[rel] = hashes

    # reachability: everything that is not a plain module is a root
    eligible = {rel for rel, r in files.items()
                if not r["skipped"] and r["role"] == "code" and r["lang"] in GRAPH_LANGS
                and not r["entry"] and PurePosixPath(rel).name not in SPECIAL_NAMES
                and not _CONVENTION_FILE.search(rel)
                and not (set(PurePosixPath(rel).parts[:-1]) & CONVENTION_DIRS)}
    # Docs are not roots: a README mentioning a script does not prove it still runs.
    roots = [rel for rel, r in files.items()
             if not r["skipped"] and rel not in eligible and r["role"] != "doc"]
    seen = set(roots)
    queue = deque(roots)
    while queue:
        current = queue.popleft()
        for nxt in files[current]["imports"] | files[current]["references"]:
            if nxt in files and nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    unused = sorted(eligible - seen)
    # Many unreached files sharing one name (formats.py, plugin.py, handler.py) are
    # almost always loaded by convention. That is a question for a person, not a finding.
    convention: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = defaultdict(list)
    for rel in unused:
        by_name[PurePosixPath(rel).name].append(rel)
    for name, paths in by_name.items():
        if len(paths) >= 5:
            convention[f"files named {name}"] = paths
    grouped = {p for paths in convention.values() for p in paths}
    by_dir: dict[str, list[str]] = defaultdict(list)
    for rel in unused:
        if rel not in grouped:
            by_dir[str(PurePosixPath(rel).parent)].append(rel)
    per_dir = Counter(str(PurePosixPath(rel).parent) for rel in eligible)
    for folder, paths in by_dir.items():
        if len(paths) >= 5 and len(paths) >= 0.5 * per_dir[folder]:
            convention[f"files in {folder}/"] = paths
    grouped = {p for paths in convention.values() for p in paths}
    unused = [rel for rel in unused if rel not in grouped]

    importers: dict[str, list[str]] = defaultdict(list)
    for rel, rec in files.items():
        for target in rec["imports"]:
            importers[target].append(rel)

    entities: dict[str, dict] = {}
    cli_args = []
    for rel, rec in files.items():
        if rec["role"] == "test":
            continue  # tests read and write made-up data
        for touch in rec["io"]:
            if touch["kind"] == "cli_arg":
                cli_args.append({"name": touch["name"], "path": rel, "line": touch["line"]})
            key = touch["entity"]
            if not key:
                continue
            slot = {"read": "readers", "write": "writers"}.get(touch["dir"], "other")
            entities.setdefault(key, {"readers": [], "writers": [], "other": []})[slot].append(
                [rel, touch["line"]])
    data_keys = [k for k in entities if k.startswith(("table:", "file:", "api:"))
                 and k.split(":", 1)[1] not in files]  # reading a repo file is not external input
    external_inputs = sorted(k for k in data_keys if entities[k]["readers"] and not entities[k]["writers"])
    final_outputs = sorted(k for k in data_keys if entities[k]["writers"] and not entities[k]["readers"])

    dup_functions = []
    for group in env["func_hashes"].values():
        if len({(g["path"], g["line"]) for g in group}) > 1:
            dup_functions.append(sorted(group, key=lambda g: (g["path"], g["line"])))
    dup_functions.sort(key=lambda g: -(g[0]["end"] - g[0]["line"]))

    near_dups, notes = _near_duplicates(line_sets, cfg["limits"]["near_dup_jaccard"])

    head, git = git_stats(repo, cfg["repo"]["inactive_months"])
    for rel, stats in git.items():
        if rel in files:
            files[rel]["git"] = stats

    if len(unused) >= 20 and len(unused) > 0.3 * len(eligible):
        notes.append(f"{len(unused)} of {len(eligible)} modules look unreachable. That is a lot: "
                     "entry points are probably being missed (plugins, schedulers, dynamic "
                     "imports), so treat unused-file findings as low confidence.")
    dynamic = [rel for rel, r in files.items() if r["dynamic_imports"]]
    if dynamic:
        notes.append(f"{len(dynamic)} file(s) import modules by a computed name; files loaded "
                     "that way can look unused when they are not.")
    parse_errors = [rel for rel, r in files.items() if r["parse_error"]]
    if parse_errors:
        notes.append(f"{len(parse_errors)} file(s) could not be parsed (e.g. Python 2 syntax); "
                     "their imports and I/O were read with a rougher method.")

    by_lang = Counter(r["lang"] for r in files.values() if not r["skipped"])
    for rec in files.values():
        for key in ("imports", "references", "external"):
            rec[key] = sorted(rec[key])
    return {
        "repo": str(repo), "head": head, "files": files,
        "importers": {k: sorted(v) for k, v in importers.items()},
        "entry_points": sorted(rel for rel, r in files.items() if r["entry"]),
        "unused": unused, "eligible_for_unused": len(eligible), "convention_loaded": convention,
        "entities": entities, "external_inputs": external_inputs,
        "final_outputs": final_outputs, "cli_args": cli_args,
        "dup_functions": dup_functions, "near_dup_files": near_dups, "notes": notes,
        "stats": {"files": len(files),
                  "analyzed": sum(1 for r in files.values() if not r["skipped"]),
                  "skipped": dict(Counter(r["skipped"] for r in files.values() if r["skipped"])),
                  "lines": sum(r["lines"] for r in files.values()),
                  "by_lang": dict(by_lang.most_common())},
    }


def _near_duplicates(line_sets: dict[str, set], threshold: float) -> tuple[list[dict], list[str]]:
    index: dict[int, list[str]] = defaultdict(list)
    for path, hashes in line_sets.items():
        for h in hashes:
            index[h].append(path)
    pairs: Counter = Counter()
    notes: list[str] = []
    for paths in index.values():
        if 2 <= len(paths) <= 25:
            paths.sort()
            for i in range(len(paths)):
                for j in range(i + 1, len(paths)):
                    pairs[(paths[i], paths[j])] += 1
            if len(pairs) > 2_000_000:
                notes.append("Near-duplicate file detection stopped early: too many candidate pairs.")
                break
    out = []
    for (a, b), shared in pairs.items():
        if shared < 12:
            continue
        jaccard = shared / (len(line_sets[a]) + len(line_sets[b]) - shared)
        if jaccard >= threshold:
            out.append({"a": a, "b": b, "jaccard": round(jaccard, 2), "shared_lines": shared})
    out.sort(key=lambda d: -d["shared_lines"])
    return out, notes
