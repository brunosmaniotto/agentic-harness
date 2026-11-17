"""Choosing which code a model sees, from the call graph.

A worker asked "does this file winsorize the totals it writes?" needs the function
that builds the totals, whole, plus the helpers it calls and one place it is called
from. Windows of lines around a name match miss exactly those. Python files carry
their functions and call sites from the static pass; other languages fall back to
name-matched windows.
"""
from __future__ import annotations

import re
from collections import defaultdict

from . import evidence


class CallGraph:
    def __init__(self, files: dict):
        self.files = files
        self.defs: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        self.sites: dict[str, list[tuple[str, int, str | None]]] = defaultdict(list)
        for rel, rec in files.items():
            for fn in rec.get("functions", []):
                self.defs[fn["name"]].append((rel, fn))
            for name, line, caller in rec.get("calls", []):
                self.sites[name].append((rel, line, caller))

    def functions(self, rel: str) -> list[dict]:
        return self.files.get(rel, {}).get("functions", [])

    def resolve(self, name: str, from_rel: str) -> tuple[str, dict] | None:
        """The definition a call to `name` in `from_rel` most likely reaches, or None if unsure."""
        found = self.defs.get(name, [])
        same = [d for d in found if d[0] == from_rel]
        if same:
            return same[0]
        imported = set(self.files.get(from_rel, {}).get("imports", []))
        via_import = [d for d in found if d[0] in imported]
        if len(via_import) == 1:
            return via_import[0]
        return found[0] if len(found) == 1 else None

    def enclosing(self, rel: str, line: int) -> dict | None:
        """The innermost function containing a line; a class only if no function does."""
        inside = [f for f in self.functions(rel) if f["line"] <= line <= f["end"]]
        funcs = [f for f in inside if f["kind"] == "function"]
        pool = funcs or inside
        return max(pool, key=lambda f: f["line"]) if pool else None

    @staticmethod
    def _within(caller: str | None, fn: dict) -> bool:
        return bool(caller) and (caller == fn["qual"] or caller.startswith(fn["qual"] + "."))

    def callers(self, rel: str, fn: dict) -> list[tuple[str, int, str | None]]:
        out = []
        for path, line, caller in self.sites.get(fn["name"], []):
            if path == rel and self._within(caller, fn):
                continue  # recursion, or a nested helper
            hit = self.resolve(fn["name"], path)
            if hit and hit[0] == rel and hit[1]["qual"] == fn["qual"]:
                out.append((path, line, caller))
        return out

    def callees(self, rel: str, fn: dict) -> list[tuple[str, dict]]:
        out = []
        for name, _, caller in self.files.get(rel, {}).get("calls", []):
            if self._within(caller, fn):
                hit = self.resolve(name, rel)
                if hit and not (hit[0] == rel and hit[1]["qual"] == fn["qual"]) and hit not in out:
                    out.append(hit)
        return out


def terms_for(entity: str = "", question: str = "", rel: str = "") -> list[str]:
    """Lower-case words worth looking for: the subject's name parts and identifiers in the question."""
    words = [] if (entity.startswith("file:") and entity[5:] == rel) else [
        w.lower() for w in re.split(r"[^A-Za-z0-9_]+", entity.split(":", 1)[-1]) if len(w) >= 3]
    words += [w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", question)
              if len(w) >= 4 and ("_" in w or re.search(r"[a-z][A-Z]", w) or w.isupper())]
    return list(dict.fromkeys(words))


def _render(lines: list[str], rel: str, a: int, b: int, note: str = "") -> str:
    return f"### {rel} (lines {a}-{b} of {len(lines)})" + (f": {note}" if note else "") + "\n" \
        + evidence.numbered(lines, a, b)


def code_context(ctx, rel: str, terms: list[str], cap: int = 300, anchors: list[int] | None = None,
                 other_files: bool = True) -> tuple[str, list[str]]:
    """Code from `rel` relevant to `terms` (and to the lines in `anchors`), as whole functions
    where they fit, then the repository functions those call and one or two places that call
    them. Returns the listing and the files it shows."""
    limits = ctx.cfg["limits"]
    lines = ctx.lines(rel)
    n = len(lines)
    graph: CallGraph = ctx.callgraph
    use_graph = limits["call_graph"] and graph.functions(rel)
    fmax = limits["function_max_lines"]
    main_cap = int(cap * 0.7) if (other_files and use_graph) else cap

    hit_lines = [i for i, line in enumerate(lines, 1) if any(t in line.lower() for t in terms)] if terms else []
    chosen: list[dict] = []  # functions the listing is about, most relevant first
    if use_graph:
        score: dict[str, float] = defaultdict(float)
        by_qual = {f["qual"]: f for f in graph.functions(rel)}
        for line in anchors or []:
            fn = graph.enclosing(rel, line)
            if fn:
                score[fn["qual"]] += 100
        for line in hit_lines:
            fn = graph.enclosing(rel, line)
            if fn:
                score[fn["qual"]] += 1
        for f in by_qual.values():
            if f["name"].lower() in terms:
                score[f["qual"]] += 10
        chosen = [by_qual[q] for q in sorted(score, key=lambda q: -score[q])]

    spans: list[tuple[int, int]] = []
    used = 0
    if n <= main_cap:
        spans, used = [(1, n)], n
    else:
        for fn in chosen:
            length = fn["end"] - fn["line"] + 1
            if length <= fmax and used + length <= main_cap:
                spans.append((fn["line"], fn["end"]))
                used += length
        covered = lambda i: any(a <= i <= b for a, b in spans)
        loose = [(i, i) for i in (anchors or []) + hit_lines if not covered(i)]
        for a, b in evidence.merge_windows(loose, 15, n):
            if used >= main_cap:
                break
            b = min(b, a + main_cap - used - 1)
            spans.append((a, b))
            used += b - a + 1
        if not spans:
            spans, used = [(1, min(n, main_cap))], min(n, main_cap)
    spans = evidence.merge_windows(spans, 0, n)
    parts = [_render(lines, rel, a, b) for a, b in spans]
    shown = [rel]

    if other_files and use_graph:
        budget = cap - used
        in_view = lambda path, i: path == rel and any(a <= i <= b for a, b in spans)
        seen: set[tuple[str, str]] = set()
        for fn in chosen[:4]:  # helpers the chosen code calls
            for path, callee in graph.callees(rel, fn)[:limits["context_callees"]]:
                if budget <= 0 or (path, callee["qual"]) in seen or in_view(path, callee["line"]):
                    continue
                seen.add((path, callee["qual"]))
                other = ctx.lines(path)
                a, b = callee["line"], min(callee["end"], callee["line"] + 29, callee["line"] + budget - 1)
                parts.append(_render(other, path, a, b, f"{callee['qual']}, called by {fn['qual']}"))
                shown.append(path)
                budget -= b - a + 1
        taken = 0
        for fn in chosen[:2]:  # one level of callers
            for path, line, caller in graph.callers(rel, fn):
                if budget <= 0 or taken >= limits["context_callers"] or in_view(path, line):
                    continue
                other = ctx.lines(path)
                box = graph.enclosing(path, line)
                a = max(line - 10, box["line"] if box else 1)
                b = min(line + 12, box["end"] if box else len(other), line + budget - 1)
                parts.append(_render(other, path, a, b, f"calls {fn['name']}"
                                     + (f" from {caller}" if caller else "")))
                shown.append(path)
                budget -= b - a + 1
                taken += 1
    return "\n".join(parts), list(dict.fromkeys(shown))
