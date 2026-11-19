"""Potential clean up: files, functions, classes, constants and outputs nothing uses.

Separate from the audit's findings: dead code is not a bug, and a large repository
has enough of it to bury the bugs. Every candidate ends in one of three tiers:

    safe       nothing refers to it anywhere: code, tests, configs, notebooks, strings
    probable   referred to only by tests or documentation, or public in an installable
               package (other repositories may import it)
    check      may be reached in a way static analysis cannot see: a framework
               decorator, a string (getattr, a registry, a config file), a notebook,
               a method of a class built on an outside base, an output for people

Candidates that are not plainly safe go up an escalation ladder, each step only for
what the step before could not settle:

    1. static analysis: every reference to the name, classified by where it occurs
    2. a cheap model reads those references, packed many candidates to a call
    3. the verifier reads the definition, each reference in context, and every place
       the repository resolves names dynamically
    4. an optional outside checker (any command, see [cleanup] external_check)
    5. maintainers: one question per file, whose answer goes back to step 3
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import textwrap
import warnings
from collections import defaultdict
from pathlib import PurePosixPath

from . import evidence
from .context import code_context
from .core import Ctx, dicts, pick, sha, text_of

TIERS = ("safe", "probable", "check")
TIER_LABEL = {"safe": "Safe to remove", "probable": "Probably unused", "check": "Check first"}
# Decorators that do not register the function anywhere: it still has to be called.
NEUTRAL_DECORATORS = {"staticmethod", "classmethod", "property", "cached_property", "wraps", "lru_cache",
                      "cache", "abstractmethod", "dataclass", "overload", "total_ordering", "setter",
                      "getter", "deleter", "jit", "njit", "vectorize", "contextmanager", "singledispatch"}
PACKAGING = {"pyproject.toml", "setup.py", "setup.cfg"}
DOC_EXT = {".md", ".rst", ".txt", ".tex", ".html"}
CONFIG_EXT = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf", ".env", ".sh", ".sql", ".r",
              ".js", ".ts", ".mk", ""}
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# -- references -----------------------------------------------------------------------

def _python_refs(tree, rel: str, role: str, names: set[str], out: dict) -> None:
    """Uses of `names` in parsed Python: loads, attributes, imports, and strings that name
    them (getattr, registries, __all__). Docstrings and comments do not count."""
    code_kind = "test" if role == "test" else "code"

    def strings(node, kind):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and len(sub.value) <= 200:
                for w in set(_WORD.findall(sub.value)) & names:
                    out[w].append((rel, sub.lineno, kind))

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant) \
                    and isinstance(child.value.value, str):
                continue  # a docstring or a bare string is documentation
            if isinstance(child, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__"
                                                     for t in child.targets):
                strings(child.value, "all")
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id in names:
                out[child.id].append((rel, child.lineno, code_kind))
            elif isinstance(child, ast.Attribute) and child.attr in names:
                out[child.attr].append((rel, child.lineno, code_kind))
            elif isinstance(child, (ast.ImportFrom, ast.Import)):
                for alias in child.names:
                    last = alias.name.rsplit(".", 1)[-1]
                    if last in names:
                        out[last].append((rel, child.lineno, code_kind))
            elif isinstance(child, ast.Constant) and isinstance(child.value, str) and len(child.value) <= 200:
                for w in set(_WORD.findall(child.value)) & names:
                    out[w].append((rel, child.lineno, "string" if role != "test" else "test"))
            visit(child)

    visit(tree)


def _kind_of(rel: str, role: str) -> str:
    name = PurePosixPath(rel).name
    if name in PACKAGING:
        return "entry"  # pyproject scripts, setup entry points
    suffix = PurePosixPath(rel).suffix.lower()
    if suffix == ".ipynb":
        return "notebook"
    if role == "test":
        return "test"
    if role == "doc" or suffix in DOC_EXT:
        return "doc"
    return "config" if suffix in CONFIG_EXT or role == "config" else "other"


class References:
    """Every occurrence of a set of names across the repository, classified by where it is."""

    def __init__(self, ctx: Ctx, names: set[str]):
        self.ctx = ctx
        self.where: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
        self.trees: dict[str, ast.AST] = {}
        pattern = re.compile(r"(?<![\w])(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
                             + r")(?![\w])") if names else None
        for rel, rec in ctx.skel["files"].items():
            if rec["skipped"]:
                continue
            if rel.endswith(".py") and rec["lang"] == "python":
                tree = self.tree(rel)
                if tree is not None:
                    _python_refs(tree, rel, rec["role"], names, self.where)
                    continue
            if pattern is None:
                continue
            kind = _kind_of(rel, rec["role"])
            for i, line in enumerate(ctx.lines(rel), 1):
                for m in pattern.finditer(line):
                    self.where[m.group(1)].append((rel, i, kind))

    def tree(self, rel: str):
        if rel not in self.trees:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")  # odd escapes in old strings are not our business
                    self.trees[rel] = ast.parse("\n".join(self.ctx.lines(rel)))
            except (SyntaxError, ValueError):
                self.trees[rel] = None
        return self.trees[rel]

    def of(self, name: str, own: tuple[str, int, int] | None = None) -> list[tuple[str, int, str]]:
        """References to `name`, leaving out those inside its own definition (recursion)."""
        out = []
        for rel, line, kind in self.where.get(name, []):
            if own and rel == own[0] and own[1] <= line <= own[2]:
                continue
            out.append((rel, line, kind))
        return list(dict.fromkeys(out))


# -- candidates -----------------------------------------------------------------------

def _packaged(ctx: Ctx) -> bool:
    """Is this a library other repositories import? Then its public names may be used
    elsewhere. Being installable is not enough: research and application repositories
    often have a pyproject.toml only for their dependencies."""
    setting = ctx.cfg["cleanup"]["public_api"]
    if setting in ("yes", "no"):
        return setting == "yes"
    files = ctx.skel["files"]
    text = "\n".join("\n".join(ctx.lines(p)) for p in ("setup.py", "setup.cfg", "pyproject.toml") if p in files)
    return bool(re.search(r"packages\s*=|find_packages|\[tool\.setuptools\.packages|"
                          r"\[tool\.poetry\][^\[]*packages|\[tool\.hatch\.build", text))


def _decorators(node) -> list[str]:
    out = []
    for d in getattr(node, "decorator_list", []):
        target = d.func if isinstance(d, ast.Call) else d
        parts = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        out.append(".".join(reversed(parts)))
    return out


def _definitions(ctx: Ctx, refs: References, rel: str) -> list[dict]:
    """Top-level functions, classes, methods of top-level classes, and module constants."""
    tree = refs.tree(rel)
    if tree is None:
        return []
    repo_classes = {f["name"] for r in ctx.skel["files"].values() for f in r.get("functions", [])
                    if f["kind"] == "class"}
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            out.append({"kind": kind, "name": node.name, "line": node.lineno, "end": node.end_lineno,
                        "decorators": _decorators(node), "bases": [], "owner": None})
            if kind == "class":
                bases = [ast.unparse(b).rsplit(".", 1)[-1] for b in node.bases]
                outside = [b for b in bases if b not in repo_classes and b not in ("object", "ABC", "Generic")]
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out.append({"kind": "method", "name": sub.name, "line": sub.lineno, "end": sub.end_lineno,
                                    "decorators": _decorators(sub), "bases": outside, "owner": node.name})
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", t.id):
                    out.append({"kind": "constant", "name": t.id, "line": node.lineno, "end": node.end_lineno,
                                "decorators": [], "bases": [], "owner": None})
    return out


def _tier(cand: dict, found: list[tuple[str, int, str]], packaged: bool, in_package: bool) -> tuple[str, str] | None:
    """(tier, reason), or None when the name is plainly used."""
    kinds = {k for _, _, k in found}
    if kinds & {"code", "entry"}:
        return None
    if "all" in kinds:
        return "check", "listed in __all__, so it is part of a public interface"
    framework = [d for d in cand["decorators"] if d.rsplit(".", 1)[-1] not in NEUTRAL_DECORATORS]
    if framework:
        return "check", f"decorated with @{framework[0]}, which may register it with a framework"
    if cand["kind"] == "method" and cand["bases"]:
        return "check", (f"a method of a class built on {cand['bases'][0]}, which may call it "
                         "without this repository naming it")
    if "string" in kinds:
        return "check", "named in a string in the code (getattr, a registry, a lookup table)"
    if "config" in kinds or "other" in kinds:
        return "check", "named in a configuration or script file that may refer to it"
    if "notebook" in kinds:
        return "check", "used only from notebooks"
    if "test" in kinds:
        return "probable", "used only by tests"
    if packaged and in_package and not cand["name"].startswith("_"):
        return "probable", "public in an installable package: other repositories may import it"
    if "doc" in kinds:
        return "probable", "mentioned only in documentation"
    return "safe", "nothing in the repository refers to it"


_CODEISH = re.compile(r"[=(\[]|^\s*(return|import|from|for|if|def|class|print)\b")


def _commented_code(lines: list[str], min_lines: int = 4) -> list[tuple[int, int]]:
    """Runs of comment lines that parse as Python once the # is removed."""
    runs, start = [], None
    for i, line in enumerate(lines + [""], 1):
        if line.lstrip().startswith("#") and not line.lstrip().startswith("#!"):
            start = start or i
            continue
        if start and i - start >= min_lines:
            body = [re.sub(r"^\s*# ?", "", l) for l in lines[start - 1:i - 1]]
            text = "\n".join(body)
            if sum(bool(_CODEISH.search(b)) for b in body) >= len(body) / 2:
                try:
                    ast.parse(textwrap.dedent(text))
                    runs.append((start, i - 1))
                except SyntaxError:
                    pass
        start = None
    return runs


def candidates(ctx: Ctx) -> list[dict]:
    """Everything that looks unused, each with its references and static tier."""
    from .blocks import NOT_INPUTS, data_flow
    files = ctx.skel["files"]
    code = [rel for rel, r in files.items()
            if not r["skipped"] and r["role"] == "code" and r["lang"] == "python" and rel.endswith(".py")]
    packaged = _packaged(ctx)
    packages = {str(PurePosixPath(rel).parent) for rel in files if rel.endswith("__init__.py")}

    # names first, then one pass over the repository for all of them
    pre = References(ctx, set())
    defs = {rel: _definitions(ctx, pre, rel) for rel in code}
    names = {d["name"] for ds in defs.values() for d in ds}
    stems = {PurePosixPath(rel).stem for rel in ctx.skel["unused"]}
    refs = References(ctx, names | stems)
    refs.trees.update(pre.trees)

    out = []

    def add(kind, name, rel, line, end, tier, reason, found, **extra):
        out.append({"key": sha("cleanup", kind, rel, name)[:16], "kind": kind, "name": name, "path": rel,
                    "line": line, "end": end, "lines": end - line + 1, "tier": tier, "reason": reason,
                    "refs": [{"path": p, "line": i, "kind": k} for p, i, k in found[:12]],
                    "decided_by": "static", **extra})

    for rel in ctx.skel["unused"]:
        stem = PurePosixPath(rel).stem
        found = [r for r in refs.of(stem) if r[0] != rel]
        kinds = {k for _, _, k in found}
        if kinds & {"code", "entry"}:
            tier, reason = "check", "no import reaches it, but its name appears in code (a dynamic import?)"
        elif kinds & {"config", "other", "string", "notebook"}:
            tier, reason = "check", "no import reaches it, but a configuration, script or notebook names it"
        elif kinds & {"doc", "test"}:
            tier, reason = "probable", "no import reaches it; only documentation or tests mention it"
        else:
            tier, reason = "safe", "no file imports it, runs it or mentions it"
        git = files[rel].get("git") or {}
        if git.get("last"):
            reason += f"; last changed {git['last']}"
        add("file", rel, rel, 1, files[rel]["lines"], tier, reason, found)

    unused_files = set(ctx.skel["unused"])
    for rel, ds in defs.items():
        if rel in unused_files:
            continue  # the whole file is already a candidate
        dead_classes = set()
        for d in ds:
            if d["name"].startswith("__") and d["name"].endswith("__"):
                continue
            if d["kind"] == "method" and d["owner"] in dead_classes:
                continue  # covered by its class
            found = refs.of(d["name"], (rel, d["line"], d["end"]))
            verdict = _tier(d, found, packaged, str(PurePosixPath(rel).parent) in packages)
            if verdict is None:
                continue
            if d["kind"] == "class":
                dead_classes.add(d["name"])
            label = f"{d['owner']}.{d['name']}" if d["owner"] else d["name"]
            add(d["kind"], label, rel, d["line"], d["end"], *verdict, found,
                decorators=d["decorators"], bases=d["bases"])

    readers, writers, _ = data_flow(ctx)
    for key, ws in sorted(writers.items()):
        name = key.split(":", 1)[1]
        if readers.get(key) or NOT_INPUTS.search(name) or not key.startswith(("file:", "table:")):
            continue
        if key.startswith("file:") and name in files:
            continue  # a file in the repository, not something code produces
        live = sorted(w for w in ws if w not in unused_files)
        for w in live[:1]:  # an output of an unused file goes with the file
            line = next((t["line"] for t in files[w]["io"] if t["entity"] == key and t["dir"] == "write"), 1)
            add("output", key, w, line, line, "check",
                f"{w} writes it and nothing in the repository reads it: a deliverable, or left over?", [])

    for rel in code:
        for a, b in _commented_code(ctx.lines(rel)):
            add("commented_code", f"lines {a}-{b}", rel, a, b, "safe", "commented-out code", [])
    return out


# -- escalation -----------------------------------------------------------------------

def _card(ctx: Ctx, c: dict, label: str, context_lines: int = 0) -> str:
    lines = ctx.lines(c["path"])
    head = lines[c["line"] - 1].strip() if 0 < c["line"] <= len(lines) else ""
    out = [f"[{label}] {c['kind']} `{c['name']}` in {c['path']}:{c['line']}-{c['end']} ({c['lines']} lines)",
           f"  first line: {head[:200]}"]
    if c.get("decorators"):
        out.append(f"  decorators: {', '.join('@' + d for d in c['decorators'])}")
    if c.get("bases"):
        out.append(f"  class built on: {', '.join(c['bases'])}")
    out.append(f"  static analysis: {TIER_LABEL[c['tier']].lower()}, because {c['reason']}")
    if c["refs"]:
        out.append("  every place the name occurs outside its definition:")
        for r in c["refs"]:
            other = ctx.lines(r["path"])
            if context_lines:
                a, b = max(1, r["line"] - context_lines), min(len(other), r["line"] + context_lines)
                out.append(f"  - {r['path']} [{r['kind']}]\n" + evidence.numbered(other, a, b))
            else:
                text = other[r["line"] - 1].strip()[:180] if 0 < r["line"] <= len(other) else ""
                out.append(f"  - {r['path']}:{r['line']} [{r['kind']}] {text}")
    else:
        out.append("  the name occurs nowhere else in the repository")
    return "\n".join(out)


def _dynamic_sites(ctx: Ctx, limit: int = 25) -> list[str]:
    """Places the repository looks names up at run time: where 'unused' can be wrong."""
    pattern = re.compile(r"\b(getattr|importlib|__import__|globals\(\)|locals\(\)|import_module|"
                         r"entry_points|registry|REGISTRY|register)\b")
    out = []
    for rel, rec in ctx.skel["files"].items():
        if rec["skipped"] or rec["role"] != "code":
            continue
        for i, line in enumerate(ctx.lines(rel), 1):
            if pattern.search(line) and not line.lstrip().startswith("#"):
                out.append(f"- {rel}:{i}: {line.strip()[:160]}")
                if len(out) >= limit:
                    return out
    return out


def run_static(ctx: Ctx) -> None:
    """Candidates and tiers without model calls; earlier verdicts still apply."""
    found = candidates(ctx)
    ctx.store.meta_set("cleanup_static", found)
    _settle(ctx, found)


def run_cleanup(ctx: Ctx) -> dict:
    """Static candidates, then the escalation ladder for the ones static analysis cannot settle."""
    cfg = ctx.cfg["cleanup"]
    found = candidates(ctx)
    ctx.store.meta_set("cleanup_static", found)
    stats = {"ran": 0, "skipped": 0, "failed": 0}
    if not cfg["escalate"]:
        _settle(ctx, found)
        return stats
    queue = [c for c in found if c["tier"] != "safe" and c["kind"] not in ("output", "commented_code")]
    if cfg["confirm_safe"]:
        queue += [c for c in found if c["tier"] == "safe" and c["kind"] not in ("commented_code",)]

    # step 2: cheap model, many candidates per call
    system, prompt_hash = ctx.prompt("cleanup")
    model = ctx.llm.model_for("cleanup")
    packs, size = [], cfg["pack"]
    for i in range(0, len(queue), size):
        pack = queue[i:i + size]
        body = "CANDIDATES\n\n" + "\n\n".join(_card(ctx, c, f"K{j}") for j, c in enumerate(pack, 1))
        packs.append(("p-" + sha(*(c["key"] for c in pack))[:12], sha(prompt_hash, model, body), (pack, body)))

    def worker1(payload):
        return ctx.llm.complete_json("cleanup", system, payload[1])

    def apply1(uid, payload, res):
        pack, _ = payload
        for item in dicts(res.get("verdicts")):
            m = re.match(r"K(\d+)$", str(item.get("candidate", "")).strip())
            if m and 1 <= int(m.group(1)) <= len(pack):
                c = pack[int(m.group(1)) - 1]
                ctx.store.meta_set(f"cleanup1:{c['key']}", {
                    "verdict": pick(item.get("verdict"), ("unused", "used", "unsure"), "unsure"),
                    "reason": text_of(item.get("reason"), 400)})

    r1 = ctx.run_units("cleanup", packs, worker1, apply1)
    for k in stats:
        stats[k] += r1.get(k, 0)

    # step 3: the verifier, one candidate at a time, with code in context
    unsure = [c for c in queue if (ctx.store.meta_get(f"cleanup1:{c['key']}") or {}).get("verdict") == "unsure"]
    if cfg["confirm_probable"]:
        unsure += [c for c in queue if c["tier"] != "check"
                   and (ctx.store.meta_get(f"cleanup1:{c['key']}") or {}).get("verdict") == "unused"]
    system2, prompt_hash2 = ctx.prompt("cleanup_check")
    model2 = ctx.llm.model_for("cleanup_check")
    dynamic = _dynamic_sites(ctx)
    # A maintainer's answer goes back to the verifier, for the candidates its question named.
    answered = [(q["unit"][2:], q["text"], q["answer"]) for q in
                ctx.store.questions("stage='cleanup' AND status='answered' AND answer IS NOT NULL")]
    for note in ctx.human_notes(400):  # free-form notes about a file apply to all of it
        if note["subject"] and note["subject"].startswith("file:") and note["kind"] != "answer":
            answered.append((note["subject"][5:], "", note["statement"]))

    def told(c):
        return [a for path, asked, a in answered
                if path == c["path"] and (not asked or f"`{c['name']}`" in asked)]
    units = []
    for c in unsure[:cfg["max_checks"]]:
        listing, _ = code_context(ctx, c["path"], [c["name"].rsplit(".", 1)[-1].lower()], cap=120,
                                  anchors=[c["line"]], other_files=False) if c["kind"] != "file" else \
            (f"### {c['path']} (lines 1-{min(c['end'], 80)} of {c['end']})\n"
             + evidence.numbered(ctx.lines(c["path"]), 1, min(c["end"], 80)), [])
        first = ctx.store.meta_get(f"cleanup1:{c['key']}") or {}
        body = "\n".join([_card(ctx, c, "K1", context_lines=3),
                          f"\nFIRST CHECK said {first.get('verdict', '?')}: {first.get('reason', '')}",
                          "\nDEFINITION", listing,
                          "\nPLACES THE REPOSITORY LOOKS NAMES UP AT RUN TIME", "\n".join(dynamic) or "(none found)"]
                         + (["\nMAINTAINERS SAID ABOUT THIS FILE"] + [f"- {a}" for a in told(c)]
                            if told(c) else []))
        units.append((c["key"], sha(prompt_hash2, model2, body), (c, body)))

    def worker2(payload):
        return ctx.llm.complete_json("cleanup_check", system2, payload[1])

    def apply2(uid, payload, res):
        ctx.store.meta_set(f"cleanup2:{uid}", {
            "verdict": pick(res.get("verdict"), ("unused", "tests_only", "used", "needs_human"), "needs_human"),
            "reason": text_of(res.get("reasoning"), 600), "question": text_of(res.get("question"), 400)})

    r2 = ctx.run_units("cleanup_check", units, worker2, apply2)
    for k in stats:
        stats[k] += r2.get(k, 0)

    # step 4: an outside checker, if one is configured
    open_ = [c for c in unsure if (ctx.store.meta_get(f"cleanup2:{c['key']}") or {}).get("verdict") == "needs_human"]
    if cfg["external_check"] and open_:
        _external(ctx, open_)

    # step 5: maintainers, one question per file
    _ask(ctx, found)
    _settle(ctx, found)
    return stats


def _external(ctx: Ctx, items: list[dict]) -> None:
    """Pipe the unsettled candidates to a command as JSON; read verdicts back.

    stdin:  {"repo": path, "candidates": [{"key", "kind", "name", "path", "line", "end", "reason", "refs"}]}
    stdout: {"verdicts": [{"key": ..., "verdict": "unused" | "used" | "unsure", "reason": ...}]}
    Results are cached by input, like model calls."""
    payload = json.dumps({"repo": ctx.cfg["repo"]["path"],
                          "candidates": [{k: c[k] for k in ("key", "kind", "name", "path", "line", "end",
                                                            "reason", "refs")} for c in items]}, sort_keys=True)
    key = "external:" + sha(json.dumps(ctx.cfg["cleanup"]["external_check"]), payload)
    raw = ctx.store.cache_get(key)
    if raw is None:
        try:
            done = subprocess.run(ctx.cfg["cleanup"]["external_check"], input=payload, capture_output=True,
                                  text=True, timeout=ctx.cfg["cleanup"]["external_timeout_s"])
            json.loads(done.stdout)
            raw = done.stdout
            ctx.store.cache_set(key, raw)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            ctx.log(f"  external clean-up check failed: {type(exc).__name__}: {exc}")
            return
    for item in dicts(json.loads(raw).get("verdicts")):
        verdict = pick(item.get("verdict"), ("unused", "used", "unsure"), None)
        if verdict and any(c["key"] == item.get("key") for c in items):
            ctx.store.meta_set(f"cleanup3:{item['key']}", {"verdict": verdict, "reason": text_of(item.get("reason"), 400)})


def _final(ctx: Ctx, c: dict) -> tuple[str | None, str]:
    """(tier or None if used, who decided) after every step that ran."""
    for level, who in (("cleanup3", "outside check"), ("cleanup2", "verifier"), ("cleanup1", "first check")):
        v = ctx.store.meta_get(f"{level}:{c['key']}")
        if not v or v["verdict"] in ("unsure",):
            continue
        if v["verdict"] == "used":
            return None, who
        if v["verdict"] == "tests_only":
            return "probable", who  # removing it means removing its tests too
        if v["verdict"] == "unused":
            # the first check alone does not make something safe: it reads references, not code
            return ("safe" if level != "cleanup1" or c["tier"] == "safe" else "probable"), who
        if v["verdict"] == "needs_human":
            return "check", who
    return c["tier"], "static analysis"


def _ask(ctx: Ctx, found: list[dict]) -> None:
    by_file: dict[str, list[dict]] = defaultdict(list)
    for c in found:
        tier, who = _final(ctx, c)
        if tier == "check" and (who == "verifier" or (c["kind"] == "output" and ctx.cfg["cleanup"]["ask_about_outputs"])):
            by_file[c["path"]].append(c)
    live = set()
    for path, items in sorted(by_file.items()):
        uid = "q:" + path
        live.add(uid)
        names = ", ".join(f"`{c['name']}`" for c in items[:12]) + (f" and {len(items) - 12} more" if len(items) > 12 else "")
        outputs = all(c["kind"] == "output" for c in items)
        text = (f"Does anyone still use these outputs written by {path}: {names}?" if outputs else
                f"Is anything outside this repository (another repo, a scheduler, a notebook, a framework) "
                f"using these from {path}: {names}?")
        if ctx.store.rows("SELECT 1 FROM questions WHERE stage='cleanup' AND unit=? AND text=?", (uid, text)):
            continue
        ctx.store.clear_unit("cleanup", uid)
        ctx.add_questions("cleanup", uid, [{"text": text, "why": "The repository alone cannot show whether they "
                                                                 "are dead or reached from outside it. Answer per name."}],
                          subject=f"file:{path}", path=path)
    for row in ctx.store.rows("SELECT DISTINCT unit FROM questions WHERE stage='cleanup' AND unit LIKE 'q:%'"):
        if row["unit"] not in live:
            ctx.store.clear_unit("cleanup", row["unit"])


def _settle(ctx: Ctx, found: list[dict]) -> None:
    final = []
    for c in found:
        tier, who = _final(ctx, c)
        if tier:
            final.append(dict(c, tier=tier, decided_by=who,
                              note=next((v["reason"] for level in ("cleanup3", "cleanup2", "cleanup1")
                                         for v in [ctx.store.meta_get(f"{level}:{c['key']}")] if v), "")))
    ctx.store.meta_set("cleanup", final)


# -- output ---------------------------------------------------------------------------

KIND_LABEL = {"file": "file", "function": "function", "class": "class", "method": "method",
              "constant": "constant", "output": "output", "commented_code": "commented-out code"}


def write_cleanup(ctx: Ctx) -> None:
    items = ctx.store.meta_get("cleanup")
    if items is None:
        return
    name = ctx.cfg["repo"].get("name") or ctx.cfg["repo"]["path"].rstrip("/").rsplit("/", 1)[-1]
    out = [f"# Potential clean up: {name}", ""]
    outputs = [c for c in items if c["kind"] == "output"]
    items = [c for c in items if c["kind"] != "output"]
    total = sum(c["lines"] for c in items)
    out.append(f"{len(items)} candidate(s), {total:,} line(s) in all. Nothing here is a bug; it is code and "
               "data that may no longer be needed. Each item says why it is listed and what decided its tier.")
    out.append("")
    for tier in TIERS:
        rows = [c for c in items if c["tier"] == tier]
        out.append(f"- **{TIER_LABEL[tier]}**: {len(rows)} ({sum(c['lines'] for c in rows):,} lines)")
    if outputs:
        out.append(f"- **Outputs nothing in the repository reads**: {len(outputs)} (listed at the end)")
    out += ["", "Tiers: *safe to remove* means nothing in the repository refers to it, or the verifier read the "
            "code and agreed. *Probably unused* means only tests or documentation refer to it, or it is public in "
            "an installable package. *Check first* means it may be reached in a way the repository does not show "
            "(a framework, a string lookup, a notebook, another repository); these have a question in `questions.md`.", ""]
    for tier in TIERS:
        rows = [c for c in items if c["tier"] == tier and c["kind"] != "commented_code"]
        if not rows:
            continue
        out += [f"## {TIER_LABEL[tier]}", ""]
        by_file: dict[str, list[dict]] = defaultdict(list)
        for c in rows:
            by_file[c["path"]].append(c)
        for path in sorted(by_file, key=lambda p: -sum(c["lines"] for c in by_file[p])):
            group = sorted(by_file[path], key=lambda c: c["line"])
            out.append(f"**{path}** ({sum(c['lines'] for c in group if c['kind'] != 'output')} lines)")
            for c in group:
                where = "" if c["kind"] in ("file", "output") else f" lines {c['line']}-{c['end']},"
                extra = f" {c['note']}" if c.get("note") and c["decided_by"] != "static analysis" else ""
                out.append(f"- {KIND_LABEL[c['kind']]} `{c['name']}`{where} {c['lines'] if c['kind'] != 'output' else ''}"
                           f"{' lines' if c['kind'] != 'output' else ''}: {c['reason']}. "
                           f"*Decided by {c['decided_by']}.*{extra}")
            out.append("")
    comments = [c for c in items if c["kind"] == "commented_code"]
    if comments:
        out += ["## Commented-out code", "",
                f"{len(comments)} block(s), {sum(c['lines'] for c in comments)} lines: "
                + ", ".join(f"`{c['path']}:{c['line']}-{c['end']}`" for c in comments[:60])
                + (" …" if len(comments) > 60 else ""), ""]
    if outputs:
        out += ["## Outputs nothing in the repository reads", "",
                "Files and tables the code writes and no code reads. Most are deliverables (tables, results "
                "for a paper or a team); some are left over from an earlier version. Listed by the file that writes them.", ""]
        by_writer: dict[str, list[str]] = defaultdict(list)
        for c in outputs:
            by_writer[c["path"]].append(c["name"])
        for path in sorted(by_writer):
            out.append(f"- **{path}**: " + ", ".join(f"`{n}`" for n in sorted(by_writer[path])))
        out.append("")
    (ctx.out / "cleanup.md").write_text("\n".join(out), encoding="utf-8")
    (ctx.out / "cleanup.json").write_text(json.dumps(items + outputs, indent=2), encoding="utf-8")
