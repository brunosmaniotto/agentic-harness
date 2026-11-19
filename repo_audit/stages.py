"""The waves. Each one reads the board, does one narrow job, and writes back.

Order: static -> docs/leads (wave 0) -> system (wave 1) -> paths (wave 2)
-> files (wave 3) -> canonicalize -> reconcile <-> probe -> verify -> questions.

Every stage is split into units keyed by a hash of their input, so a second
run only redoes units whose input changed (a file was edited, a maintainer
answered a question, a prompt was rewritten).
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict, deque
from pathlib import Path, PurePosixPath

from . import evidence
from .context import code_context, terms_for
from .core import (CONFIDENCES, SEVERITIES, Ctx, as_list, dicts, norm_words, pick, sha, text_of)
from .names import base_entity, entity_field, norm_entity
from .store import id_num

FINDING_TYPES = {"conflicting_assumption", "divergent_use", "redundant_logic", "doc_drift",
                 "contract_gap", "other"}
CONTRACT_STATUS = {"met", "unmet", "mismatched", "double", "unclear"}
# A file being unused is an absence; there is no line to quote for it.
NO_QUOTE_TYPES = {"unused_file"}


# ==========================================================================
# static facts -> board
# ==========================================================================
def static_ingest(ctx: Ctx) -> None:
    store, skel = ctx.store, ctx.skel
    files = skel["files"]
    # Insert-or-ignore, then drop what is gone: claims that still hold keep their
    # ids, so nothing downstream sees a change when the repository has not changed.
    live: set[str] = set()
    store.run("BEGIN")
    for rel, rec in files.items():
        if rec["skipped"] or not rec["io"]:
            continue
        if rec["role"] == "test" and not ctx.cfg["limits"]["include_tests"]:
            continue
        lines = ctx.lines(rel)
        for touch in rec["io"]:
            if not touch["entity"]:
                continue
            ln = touch["line"]
            quote = lines[ln - 1].strip()[:300] if 0 < ln <= len(lines) else ""
            verb = {"read": "reads", "write": "writes"}.get(touch["dir"], "uses")
            fp = sha("static", rel, touch["entity"], touch["dir"], ln, quote)
            live.add(fp)
            store.add_claim(
                fingerprint=fp, stage="static", unit=rel, source="static",
                kind={"read": "input", "write": "output"}.get(touch["dir"], "behavior"),
                subject=f"file:{rel}", entity=touch["entity"],
                statement=f"{rel} {verb} {touch['entity']} (found by static analysis).",
                evidence=[{"path": rel, "line_start": ln, "line_end": ln, "quote": quote}],
                evidence_ok=1, confidence="high")
    for row in store.rows("SELECT n, fingerprint FROM claims WHERE stage='static'"):
        if row["fingerprint"] not in live:
            store.run("DELETE FROM claims WHERE n=?", (row["n"],))
    store.run("COMMIT")

    cap = ctx.cfg["limits"]["max_static_findings"]
    current: set[str] = set()

    def add(kind: str, key: str, **fields) -> None:
        fp = sha("static", kind, key)
        current.add(fp)
        store.add_finding(fingerprint=fp, stage="static", unit=kind, type=kind, **fields)

    def head(rel: str) -> dict:
        return {"path": rel, "line_start": 1, "line_end": min(files[rel]["lines"], 40), "quote": ""}

    # Unused files are reported in cleanup.md when the clean-up analysis is on.
    for rel in ([] if ctx.cfg["cleanup"]["enabled"] else sorted(skel["unused"], key=lambda r: -files[r]["lines"])[:cap]):
        git = files[rel].get("git") or {}
        history = ""
        if git:
            history = (f" Last changed {git['last']}, {git['commits']} commit(s) by "
                       f"{git['authors']} author(s)"
                       + (", none of whom has committed recently." if git["all_authors_inactive"] else "."))
        add("unused_file", rel, title=f"Nothing uses {rel}",
            detail=(f"No file imports or references {rel}, and it is not an entry point or a "
                    f"test ({files[rel]['lines']} lines).{history}"),
            severity="low", confidence="medium", files=[rel], evidence=[head(rel)])
    for group in skel["dup_functions"][:cap]:
        where = ", ".join(f"{g['path']}:{g['line']} ({g['name']})" for g in group)
        add("duplicate_function", "|".join(f"{g['path']}:{g['name']}" for g in group),
            title=f"Same function body in {len(group)} places: {group[0]['name']}",
            detail=f"These functions have identical bodies: {where}. A change to one will not reach the others.",
            severity="low", confidence="high", files=sorted({g["path"] for g in group}),
            evidence=[{"path": g["path"], "line_start": g["line"], "line_end": g["end"], "quote": ""}
                      for g in group[:6]])
    for pair in skel["near_dup_files"][:cap]:
        add("near_duplicate_files", f"{pair['a']}|{pair['b']}",
            title=f"{pair['a']} and {pair['b']} are {int(pair['jaccard'] * 100)}% the same",
            detail=f"The two files share {pair['shared_lines']} distinct lines of code.",
            severity="low", confidence="high", files=[pair["a"], pair["b"]],
            evidence=[head(pair["a"]), head(pair["b"])])
    for row in store.rows("SELECT n, fingerprint FROM findings WHERE stage='static'"):
        if row["fingerprint"] not in current:
            store.run("DELETE FROM findings WHERE n=?", (row["n"],))

    # Files that share a name and are never imported by name: ask how they are loaded.
    asked = set()
    for label, paths in sorted(skel.get("convention_loaded", {}).items()):
        fp = sha("q", "convention", label)
        asked.add(fp)
        store.add_question(
            fingerprint=fp, stage="static", unit=f"convention:{label}",
            text=(f"{len(paths)} {label} are never imported or referenced by name "
                  f"(for example {', '.join(paths[:3])}). How are they loaded, and are all of them still in use?"),
            why=("If they are loaded by convention (a plugin directory, a computed import, a "
                 "framework that scans the folder) they are fine, and they were left out of the "
                 "unused-file findings. If not, they are dead code."))
    for row in store.rows("SELECT n, fingerprint FROM questions WHERE stage='static' AND status='open'"):
        if row["fingerprint"] not in asked:
            store.run("DELETE FROM questions WHERE n=?", (row["n"],))


# ==========================================================================
# wave 0: what the documentation says
# ==========================================================================
def _doc_sources(ctx: Ctx) -> list[str]:
    cfg = ctx.cfg["baseline"]
    sources: list[str] = []
    docs_dir = Path(cfg["docs_dir"]) if cfg["docs_dir"] else None
    leads = Path(cfg["findings_file"]).resolve() if cfg["findings_file"] else None
    if docs_dir and docs_dir.is_dir():
        for p in sorted(docs_dir.rglob("*")):
            if p.is_file() and p.suffix.lower() in (".md", ".rst", ".txt") and p.resolve() != leads:
                sources.append("docs:" + p.relative_to(docs_dir).as_posix())
    if cfg["include_repo_markdown"]:
        sources += [rel for rel, r in ctx.skel["files"].items()
                    if r["role"] == "doc" and r["lang"] in ("markdown", "tex") and not r["skipped"]]
    return sources


def _chunked_units(ctx: Ctx, stage: str, sources: list[str]) -> list[tuple[str, str, tuple]]:
    _, prompt_hash = ctx.prompt(stage)
    model = ctx.llm.model_for(stage)
    limits = ctx.cfg["limits"]
    units = []
    for src in sources:
        lines = ctx.lines(src)
        for a, b in evidence.chunk_ranges(len(lines), limits["chunk_lines"], limits["chunk_overlap"]):
            body = evidence.numbered(lines, a, b)
            units.append((f"{src}#{a}", sha(prompt_hash, model, body), (src, a, b, body)))
    return units


def run_docs(ctx: Ctx) -> dict:
    system, _ = ctx.prompt("docs")

    def worker(payload):
        src, a, b, body = payload
        return ctx.llm.complete_json("docs", system, f"DOCUMENT: {src} (lines {a}-{b})\n\nLISTING\n{body}")

    def subject_from(item: dict) -> str:
        path = ctx.resolve_path(item.get("about"))
        return f"file:{path}" if path else "system"

    def apply(uid, payload, res):
        ctx.store.clear_unit("docs", uid)
        ctx.add_claims("docs", uid, res.get("claims"), subject="system", path=payload[0],
                       source="docs", subject_from=subject_from)

    return ctx.run_units("docs", _chunked_units(ctx, "docs", _doc_sources(ctx)), worker, apply)


def run_leads(ctx: Ctx) -> dict:
    """Turn the documentation system's 'worth investigating' notes into candidates."""
    path = ctx.cfg["baseline"]["findings_file"]
    if not path or not Path(path).is_file():
        return ctx.run_units("leads", [], None, None)
    system, _ = ctx.prompt("leads")

    def worker(payload):
        return ctx.llm.complete_json("leads", system, "NOTES\n" + payload[3])

    def apply(uid, payload, res):
        ctx.store.clear_unit("leads", uid)
        keep = set()
        for item in dicts(res.get("leads")):
            title = text_of(item.get("title"), 200)
            if not title:
                continue
            files = [p for p in (ctx.resolve_path(x) for x in as_list(item.get("files"))) if p]
            fp = sha("lead", norm_words(title), *files)
            keep.add(fp)
            ctx.store.add_finding(
                fingerprint=fp, stage="leads", unit=uid, type="lead", title=title,
                detail=text_of(item.get("detail")), severity=pick(item.get("severity"), SEVERITIES, "medium"),
                confidence="low", files=files,
                evidence=[{"path": p, "line_start": 1, "line_end": min(len(ctx.lines(p)), 200), "quote": ""}
                          for p in files[:4]])
        ctx.store.sync_findings("leads", uid, keep)

    return ctx.run_units("leads", _chunked_units(ctx, "leads", ["leads:"]), worker, apply)


# ==========================================================================
# wave 1: what goes in, what comes out
# ==========================================================================
def _who(pairs: list, limit: int = 4) -> str:
    paths = sorted({p for p, _ in pairs})
    more = f" (+{len(paths) - limit} more)" if len(paths) > limit else ""
    return ", ".join(paths[:limit]) + more


def system_digest(ctx: Ctx) -> str:
    skel, files, entities = ctx.skel, ctx.skel["files"], ctx.skel["entities"]
    stats = skel["stats"]
    out = [f"REPOSITORY: {stats['analyzed']} files, {stats['lines']} lines. By language: "
           + ", ".join(f"{k} {v}" for k, v in stats["by_lang"].items())]
    dirs: Counter = Counter()
    for rel, rec in files.items():
        if not rec["skipped"]:
            parts = PurePosixPath(rel).parts
            dirs["/".join(parts[:2]) + "/" if len(parts) > 2 else (parts[0] + "/" if len(parts) > 1 else "./")] += 1
    out.append("LAYOUT (directory: file count): " + "; ".join(f"{d} {n}" for d, n in dirs.most_common(40)))

    out.append("\nENTRY POINTS (files meant to be run):")
    for rel in skel["entry_points"][:40]:
        header = " ".join(l.strip() for l in ctx.lines(rel)[:6] if l.strip())[:160]
        out.append(f"- {rel} | imports {len(files[rel]['imports'])} repo files | starts: {header}")
    if not skel["entry_points"]:
        out.append("- none detected")

    def listing(title: str, keys: list[str], slot: str) -> None:
        out.append(f"\n{title}")
        for key in keys[:60]:
            out.append(f"- {key} <- {_who(entities[key][slot])}")
        if not keys:
            out.append("- none detected")
        elif len(keys) > 60:
            out.append(f"- ... and {len(keys) - 60} more")

    listing("EXTERNAL INPUTS (read here, never written here):", skel["external_inputs"], "readers")
    listing("FINAL OUTPUTS (written here, never read here):", skel["final_outputs"], "writers")
    internal = sorted(k for k, e in entities.items() if e["readers"] and e["writers"]
                      and not k.startswith("env:"))
    out.append("\nINTERNAL DATA (written and read here):")
    out += [f"- {k}: written by {_who(entities[k]['writers'])}; read by {_who(entities[k]['readers'])}"
            for k in internal[:60]] or ["- none detected"]
    env = sorted(k for k in entities if k.startswith("env:"))
    out.append("\nENVIRONMENT VARIABLES: " + (", ".join(k[4:] for k in env[:60]) or "none detected"))
    args = sorted({f"{a['name']} ({a['path']})" for a in skel["cli_args"]})
    out.append("COMMAND-LINE ARGUMENTS: " + (", ".join(args[:60]) or "none detected"))

    doc_claims = ctx.store.claims("source='docs' AND subject='system' AND evidence_ok=1")
    out.append("\nWHAT THE DOCUMENTATION SAYS ABOUT THE SYSTEM:")
    out += [f"- {c['statement']}" for c in doc_claims[:40]] or ["- nothing recorded"]
    notes = [c for c in ctx.human_notes() if c["subject"] == "system"]
    if notes:
        out.append("\nMAINTAINER NOTES (authoritative):")
        out += [f"- {c['statement']}" for c in notes]
    if skel["notes"]:
        out.append("\nSTATIC ANALYSIS CAVEATS:")
        out += [f"- {n}" for n in skel["notes"]]
    return "\n".join(out)


def run_system(ctx: Ctx) -> dict:
    system, prompt_hash = ctx.prompt("system")
    digest = system_digest(ctx)

    def worker(_):
        return ctx.llm.complete_json("system", system, digest)

    def apply(uid, _, res):
        ctx.store.clear_unit("system", uid)
        ctx.add_claims("system", uid, res.get("claims"), subject="system", path=None, need_evidence=False)
        ctx.add_questions("system", uid, res.get("questions"), subject="system", path=None)
        pipes = []
        for item in dicts(res.get("pipelines"))[:ctx.cfg["limits"]["max_pipelines"]]:
            name = text_of(item.get("name"), 80)
            entries = [p for p in (ctx.resolve_path(x) for x in as_list(item.get("entry_files"))) if p]
            if name and entries:
                pipes.append({"name": name, "description": text_of(item.get("description"), 600),
                              "entry_files": list(dict.fromkeys(entries))})
        ctx.store.meta_set("pipelines", pipes)

    unit = ("system", sha(prompt_hash, ctx.llm.model_for("system"), digest), None)
    return ctx.run_units("system", [unit], worker, apply)


def pipelines(ctx: Ctx) -> list[dict]:
    """Pipelines proposed by wave 1, or one per entry point if it proposed none."""
    found = ctx.store.meta_get("pipelines") or []
    if found:
        return found
    return [{"name": PurePosixPath(rel).stem, "description": "", "entry_files": [rel]}
            for rel in ctx.skel["entry_points"][:ctx.cfg["limits"]["max_pipelines"]]]


# ==========================================================================
# wave 2: how it gets from input to output
# ==========================================================================
def _io_summary(rec: dict, limit: int = 12) -> str:
    seen, parts = set(), []
    for touch in rec["io"]:
        label = touch["entity"] or f"{touch['kind']}:{touch['name']}"
        key = (touch["dir"], label)
        if key in seen:
            continue
        seen.add(key)
        verb = {"read": "reads", "write": "writes"}.get(touch["dir"], "uses")
        parts.append(f"{verb} {label} (line {touch['line']})")
    more = f"; +{len(parts) - limit} more" if len(parts) > limit else ""
    return "; ".join(parts[:limit]) + more


def _brief_card(ctx: Ctx, rel: str) -> str:
    rec = ctx.skel["files"][rel]
    out = [f"- {rel} ({rec['lang']}, {rec['lines']} lines)"]
    if rec["defs"]:
        out.append("    defines: " + ", ".join(d["name"] for d in rec["defs"][:15]))
    if rec["imports"]:
        out.append("    imports: " + ", ".join(rec["imports"][:10]))
    if rec["io"]:
        out.append("    data: " + _io_summary(rec))
    return "\n".join(out)


def _reachable(ctx: Ctx, entries: list[str], limit: int) -> tuple[list[str], int]:
    files = ctx.skel["files"]
    seen, order, queue = set(entries), [], deque(entries)
    while queue:
        current = queue.popleft()
        order.append(current)
        for nxt in files[current]["imports"] + files[current]["references"]:
            if nxt in files and nxt not in seen and not files[nxt]["skipped"]:
                seen.add(nxt)
                queue.append(nxt)
    return order[:limit], len(order)


def run_paths(ctx: Ctx) -> dict:
    system, prompt_hash = ctx.prompt("paths")
    model = ctx.llm.model_for("paths")
    units = []
    for pipe in pipelines(ctx):
        shown, total = _reachable(ctx, pipe["entry_files"], ctx.cfg["limits"]["path_files"])
        body = (f"PIPELINE: {pipe['name']}\nDESCRIPTION: {pipe['description'] or '(none given)'}\n"
                f"ENTRY FILES: {', '.join(pipe['entry_files'])}\n\n"
                f"FILES REACHABLE FROM THE ENTRY FILES ({len(shown)} of {total} shown, nearest first)\n"
                + "\n".join(_brief_card(ctx, rel) for rel in shown))
        units.append((pipe["name"], sha(prompt_hash, model, body), (pipe, body)))

    def worker(payload):
        return ctx.llm.complete_json("paths", system, payload[1])

    def apply(uid, payload, res):
        ctx.store.clear_unit("paths", uid)
        steps = []
        for item in dicts(res.get("steps")):
            desc = text_of(item.get("description"), 500)
            if not desc:
                continue
            files = [p for p in (ctx.resolve_path(x) for x in as_list(item.get("files"))) if p]
            steps.append({"description": desc, "files": files,
                          "reads": [text_of(x, 120) for x in as_list(item.get("reads"))][:10],
                          "writes": [text_of(x, 120) for x in as_list(item.get("writes"))][:10]})
        ctx.store.meta_set(f"steps:{uid}", steps)
        ctx.add_claims("paths", uid, [
            {"kind": "step", "statement": f"Step {i} of {uid}: {s['description']}"
                                          + (f" (files: {', '.join(s['files'])})" if s["files"] else "")}
            for i, s in enumerate(steps, 1)], subject=f"path:{uid}", path=None, need_evidence=False)
        ctx.add_questions("paths", uid, res.get("questions"), subject="system", path=None)

    return ctx.run_units("paths", units, worker, apply)


def file_steps(ctx: Ctx) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for pipe in pipelines(ctx):
        for i, step in enumerate(ctx.store.meta_get(f"steps:{pipe['name']}") or [], 1):
            for rel in step["files"]:
                out[rel].append(f"{pipe['name']} / step {i}: {step['description']}")
    return out


# ==========================================================================
# wave 3: what each file does and takes for granted
# ==========================================================================
def select_files(ctx: Ctx) -> list[str]:
    limits, files = ctx.cfg["limits"], ctx.skel["files"]
    chosen = []
    for rel, rec in files.items():
        if rec["skipped"] or rec["bytes"] < 40:
            continue
        if (rec["role"] == "code" or (rec["role"] == "test" and limits["include_tests"])
                or (rec["role"] == "config" and rec["lines"] <= limits["config_max_lines"])):
            chosen.append(rel)
    if limits["max_files"]:
        in_pipeline = set(file_steps(ctx))
        importers = ctx.skel["importers"]
        chosen.sort(key=lambda r: (r not in in_pipeline, files[r]["role"] != "code",
                                   -len(importers.get(r, [])), r))
        chosen = chosen[:limits["max_files"]]
    return sorted(chosen)


def _file_card(ctx: Ctx, rel: str, a: int, b: int, steps: dict) -> str:
    rec = ctx.skel["files"][rel]
    importers = ctx.skel["importers"].get(rel, [])
    known = sorted({t["entity"] for t in rec["io"] if t["entity"]})
    out = ["FILE CARD",
           f"path: {rel}",
           f"language: {rec['lang']} | role: {rec['role']} | lines: {rec['lines']} | showing lines {a}-{b}",
           f"entry point: {'yes' if rec['entry'] else 'no'}",
           "imported by: " + (", ".join(importers[:8]) + (f" (+{len(importers) - 8} more)" if len(importers) > 8 else "") or "nothing"),
           "imports: " + (", ".join(rec["imports"][:12]) or "no repo files"),
           "data touched (static analysis): " + (_io_summary(rec, 20) or "none detected"),
           "known entities: " + (", ".join(known[:30]) or "none")]
    if steps.get(rel):
        out.append("role in pipelines: " + " | ".join(steps[rel][:4]))
    return "\n".join(out)


def run_files(ctx: Ctx) -> dict:
    system, prompt_hash = ctx.prompt("files")
    model = ctx.llm.model_for("files")
    limits, files = ctx.cfg["limits"], ctx.skel["files"]
    steps = file_steps(ctx)
    units = []
    for rel in select_files(ctx):
        for a, b in evidence.chunk_ranges(files[rel]["lines"], limits["chunk_lines"], limits["chunk_overlap"]):
            # The card is deliberately not part of the hash: an answered question
            # that reshapes the map must not force every file to be read again.
            units.append((f"{rel}#{a}", sha(prompt_hash, model, files[rel]["sha"], a, b), (rel, a, b)))

    def worker(payload):
        rel, a, b = payload
        body = evidence.numbered(ctx.lines(rel), a, b)
        return ctx.llm.complete_json("files", system, _file_card(ctx, rel, a, b, steps) + "\n\nLISTING\n" + body)

    def apply(uid, payload, res):
        rel = payload[0]
        ctx.store.clear_unit("files", uid)
        ctx.add_claims("files", uid, res.get("claims"), subject=f"file:{rel}", path=rel)
        ctx.add_questions("files", uid, res.get("questions"), subject=f"file:{rel}", path=rel)
        keep = set()
        for item in dicts(res.get("concerns")):
            title = text_of(item.get("title"), 200)
            ev = ctx.check_evidence(item.get("evidence"), rel)
            if not title or not ev:
                continue
            fp = sha("local_risk", rel, norm_words(title))
            keep.add(fp)
            ctx.store.add_finding(
                fingerprint=fp, stage="files", unit=uid, type="local_risk", title=title,
                detail=text_of(item.get("detail")), severity=pick(item.get("severity"), SEVERITIES, "medium"),
                confidence=pick(item.get("confidence"), CONFIDENCES, "medium"), files=[rel], evidence=ev)
        ctx.store.sync_findings("files", uid, keep)

    return ctx.run_units("files", units, worker, apply)


# ==========================================================================
# reconciliation: do the claims fit together?
# ==========================================================================
USABLE = "(evidence_ok=1 OR source IN ('human','static'))"


def _origin(claim: dict) -> str:
    return claim["evidence"][0]["path"] if claim["evidence"] else f"{claim['source']}:{claim['unit']}"


def alias_map(ctx: Ctx) -> dict[str, str]:
    return {r["alias"]: r["canonical"] for r in ctx.store.rows("SELECT alias, canonical FROM aliases")}


def run_canonicalize(ctx: Ctx) -> dict:
    """Ask a model which entity keys are different names for the same thing."""
    system, prompt_hash = ctx.prompt("canonicalize")
    model = ctx.llm.model_for("canonicalize")
    counts: Counter = Counter()
    example: dict[str, str] = {}
    for claim in ctx.store.claims(f"entity IS NOT NULL AND {USABLE}"):
        key = base_entity(claim["entity"])
        if key.startswith("env:"):
            continue
        counts[key] += 1
        example.setdefault(key, _origin(claim))
    if len(counts) < 2:
        return ctx.run_units("canonicalize", [], None, None, prune=False)
    # Asking again for a handful of new keys (probes add a few each round) is not worth a
    # strong-model call; wait until enough have accumulated.
    asked = set(ctx.store.meta_get("canonicalize_keys") or [])
    if asked and len(set(counts) - asked) < ctx.cfg["limits"]["canonicalize_min_new_keys"]:
        return {"total": 0, "ran": 0, "failed": 0, "deferred": 0, "skipped": 1}

    def similar_first(key: str) -> str:
        tail = re.split(r"[/.]", key.split(":", 1)[1].rstrip("/"))
        tail = [t for t in tail if t and t not in ("csv", "parquet", "json", "{}")]
        return (tail[-1] if tail else key) + "|" + key

    names = sorted(counts, key=similar_first)
    units = []
    for i in range(0, len(names), 250):
        batch = names[i:i + 250]
        body = "ENTITY KEYS (key | one place that uses it)\n" + "\n".join(
            f"{k} | {example[k]}" for k in batch)
        # Hash the keys only: more claims about known keys is not a reason to ask again.
        units.append((f"batch{i // 250}", sha(prompt_hash, model, *batch), (batch, body)))

    def worker(payload):
        return ctx.llm.complete_json("canonicalize", system, payload[1])

    def apply(uid, payload, res):
        batch = set(payload[0])
        for name in batch:
            ctx.store.run("DELETE FROM aliases WHERE alias=?", (name,))
        for group in dicts(res.get("groups")):
            canonical = norm_entity(group.get("canonical"))
            if canonical not in batch:
                continue
            for alias in as_list(group.get("aliases")):
                alias = norm_entity(alias)
                if alias in batch and alias != canonical:
                    ctx.store.run("INSERT OR REPLACE INTO aliases VALUES (?,?,?)",
                                  (alias, canonical, text_of(group.get("reason"), 300)))

    stats = ctx.run_units("canonicalize", units, worker, apply)
    ctx.store.meta_set("canonicalize_keys", sorted(counts))
    return stats


_SOURCE_RANK = {"human": 0, "docs": 1, "llm": 2, "static": 3}
_KIND_RANK = {"requires": 0, "ensures": 0, "rule": 0, "assumption": 0, "behavior": 1, "answer": 1,
              "input": 2, "output": 2, "purpose": 3, "step": 4}


def _format_claim(claim: dict) -> str:
    ev = claim["evidence"][0] if claim["evidence"] else None
    where = f"{ev['path']}:{ev['line_start']}" if ev else f"{claim['source']}:{claim['unit']}"
    head = f"[{claim['id']}] source={claim['source']} origin={where} kind={claim['kind']}"
    if claim["entity"]:
        head += f" entity={claim['entity']}"
        if entity_field(claim["entity"]):
            head += f" field={entity_field(claim['entity'])}"
    if claim["attribute"]:
        head += f" attribute={claim['attribute']}"
    if claim["value"]:
        head += f" value={claim['value']}"
    out = [head, f"    statement: {claim['statement']}"]
    if ev and ev["quote"]:
        out.append("    quote: " + " ".join(ev["quote"].split())[:300])
    return "\n".join(out)


def run_reconcile(ctx: Ctx) -> dict:
    system, prompt_hash = ctx.prompt("reconcile")
    model = ctx.llm.model_for("reconcile")
    cap = ctx.cfg["limits"]["max_entity_claims"]
    aliases = alias_map(ctx)
    usable = ctx.store.claims(USABLE)

    groups: dict[str, list[dict]] = defaultdict(list)
    for claim in usable:
        if claim["entity"]:
            base = base_entity(claim["entity"])
            groups[aliases.get(base, base)].append(claim)
    # File-level groups exist only where a doc or a maintainer said something
    # about the file: that statement is compared with what the code shows.
    stated = {c["subject"] for c in usable
              if c["source"] in ("docs", "human") and c["subject"].startswith("file:")}
    for claim in usable:
        if claim["subject"] in stated and claim["kind"] != "step":
            groups[claim["subject"]].append(claim)

    def block(subject: str, claims: list[dict], chosen: list[dict]) -> str:
        known_as = sorted(a for a, canonical in aliases.items() if canonical == subject)
        return (f"SUBJECT: {subject}\n"
                + (f"ALSO KNOWN AS: {', '.join(known_as)}\n" if known_as else "")
                + (f"NOTE: {len(claims) - len(chosen)} lower-priority claims were left out.\n"
                   if len(claims) > len(chosen) else "")
                + "CLAIMS\n" + "\n".join(_format_claim(c) for c in chosen))

    limits = ctx.cfg["limits"]
    solo, small = [], []
    for subject, claims in sorted(groups.items()):
        # Something has to be comparable: statements of assumption or behavior (or what a
        # document or a person said) from at least two places. A list of who reads and who
        # writes a file, with nothing said about its contents, has nothing to disagree about.
        telling = [c for c in claims if c["source"] in ("docs", "human")
                   or (c["source"] != "static"
                       and c["kind"] in ("assumption", "behavior", "requires", "ensures", "answer"))]
        if len({_origin(c) for c in telling}) < 2:
            continue
        chosen = sorted(claims, key=lambda c: (_SOURCE_RANK.get(c["source"], 9),
                                               _KIND_RANK.get(c["kind"], 9), c["n"]))[:cap]
        chosen.sort(key=lambda c: (entity_field(c["entity"] or ""), c["attribute"] or "", c["n"]))
        (small if len(chosen) <= limits["reconcile_pack_small"] else solo).append((subject, claims, chosen))

    units = []
    for subject, claims, chosen in solo:
        body = block(subject, claims, chosen)
        units.append((subject, sha(prompt_hash, model, body), ([(subject, chosen)], body)))
    # Subjects with only a few claims share a call: most have nothing to report, and one
    # call per subject would cost more than reading the files did.
    pack, size = [], 0
    def flush():
        nonlocal pack, size
        if pack:
            body = "\n\n".join(block(*item) for item in pack)
            uid = "pack:" + sha(*(item[0] for item in pack))[:12]
            units.append((uid, sha(prompt_hash, model, body), ([(i[0], i[2]) for i in pack], body)))
        pack, size = [], 0
    for item in small:
        if pack and size + len(item[2]) > limits["reconcile_pack_claims"]:
            flush()
        pack.append(item)
        size += len(item[2])
    flush()

    def worker(payload):
        return ctx.llm.complete_json("reconcile", system, payload[1])

    def apply(uid, payload, res):
        subjects, _ = payload
        ctx.store.clear_unit("reconcile", uid)
        by_id, subject_of = {}, {}
        for subject, chosen in subjects:
            for c in chosen:
                by_id[c["id"]] = c
                subject_of[c["id"]] = subject
        names = [subject for subject, _ in subjects]

        def pick_subject(item: dict, ids: list[str]) -> str:
            given = text_of(item.get("subject"), 300)
            if given in names:
                return given
            return subject_of[ids[0]] if ids else names[0]

        keep = set()
        for item in dicts(res.get("findings")):
            ids = list(dict.fromkeys(i for i in as_list(item.get("claim_ids")) if i in by_id))
            if ids:  # a finding concerns one subject: keep the claims of the first one cited
                ids = [i for i in ids if subject_of[i] == subject_of[ids[0]]]
            cited = [by_id[i] for i in ids]
            title = text_of(item.get("title"), 200)
            if not title or len({_origin(c) for c in cited}) < 2:
                continue  # a finding must rest on at least two independent sources
            subject = pick_subject(item, ids)
            kind = pick(item.get("type"), FINDING_TYPES, "other")
            fp = sha("reconcile", kind, *sorted(c["fingerprint"] for c in cited))
            keep.add(fp)
            ev = [e for c in cited for e in c["evidence"]][:8]
            ctx.store.add_finding(
                fingerprint=fp, stage="reconcile", unit=uid, type=kind, title=title,
                detail=text_of(item.get("detail")), severity=pick(item.get("severity"), SEVERITIES, "medium"),
                confidence=pick(item.get("confidence"), CONFIDENCES, "medium"), claims=ids,
                files=sorted({e["path"] for e in ev}),
                entity=None if subject.startswith("file:") and subject[5:] in ctx.skel["files"] else subject,
                evidence=ev)
        ctx.store.sync_findings("reconcile", uid, keep)
        for item in dicts(res.get("probes")):
            path, question = ctx.resolve_path(item.get("file")), text_of(item.get("question"), 600)
            subject = pick_subject(item, [])
            if path and question:
                ctx.store.run(
                    "INSERT OR IGNORE INTO probes (fingerprint,file,entity,question) VALUES (?,?,?,?)",
                    (sha("probe", path, subject, norm_words(question)), path, subject, question))
        for item in dicts(res.get("questions")):
            subject = pick_subject(item, [])
            is_file = subject.startswith("file:") and subject[5:] in ctx.skel["files"]
            ctx.add_questions("reconcile", uid, [item], subject=subject if is_file else None,
                              path=None, entity=None if is_file else subject)

    return ctx.run_units("reconcile", units, worker, apply)


# ==========================================================================
# contracts: does what each consumer requires match what a producer ensures?
# ==========================================================================
def _static_writers(ctx: Ctx, subject: str, aliases: dict) -> dict[str, list[dict]]:
    """Files that static analysis saw writing this subject, with the lines where they do."""
    out: dict[str, list[dict]] = defaultdict(list)
    for key, entry in ctx.skel["entities"].items():
        if aliases.get(key, key) != subject:
            continue
        for path, line in entry["writers"]:
            out[path].append({"path": path, "line_start": line, "line_end": line, "quote": ""})
    return out


def run_contracts(ctx: Ctx) -> dict:
    """Match demand (what readers require of a piece of data) against supply (what
    producers ensure). An unmet requirement first sends a targeted question to the
    files that write the data; only if they do not supply it does it become a finding."""
    system, prompt_hash = ctx.prompt("contracts")
    model = ctx.llm.model_for("contracts")
    limits = ctx.cfg["limits"]
    aliases = alias_map(ctx)
    can_probe = ctx.store.meta_get("probe_rounds_done", 0) < limits["probe_rounds"]
    probes_done = Counter(r["entity"] for r in ctx.store.rows(
        "SELECT entity FROM probes WHERE status != 'pending'"))

    groups: dict[str, list[dict]] = defaultdict(list)
    for claim in ctx.store.claims(USABLE):
        if claim["entity"]:
            base = base_entity(claim["entity"])
            groups[aliases.get(base, base)].append(claim)

    items = []
    for subject, claims in sorted(groups.items()):
        demand = [c for c in claims if c["kind"] == "requires"]
        if not demand:
            continue
        supply = [c for c in claims if c["kind"] in ("ensures", "behavior", "output", "answer", "rule")
                  and c["source"] != "static"]
        writers = _static_writers(ctx, subject, aliases)
        for c in claims:  # a file that says it writes or ensures something is a writer too
            if c["kind"] in ("ensures", "output") and c["evidence"] and c["evidence"][0]["path"] not in writers:
                writers[c["evidence"][0]["path"]] = [c["evidence"][0]]
        demand, supply = demand[:25], supply[:35]
        body = (f"SUBJECT: {subject}\n"
                f"WRITTEN BY: {', '.join(sorted(writers)) or 'no file in this repository'}\n"
                "DEMAND (what consumers require)\n" + "\n".join(_format_claim(c) for c in demand)
                + "\nSUPPLY (what producers say they do)\n"
                + ("\n".join(_format_claim(c) for c in supply) or "(nothing recorded)"))
        items.append((subject, demand, supply, writers, body))

    units, pack, size = [], [], 0
    def flush():
        nonlocal pack, size
        if pack:
            body = "\n\n".join(i[4] for i in pack)
            uid = pack[0][0] if len(pack) == 1 else "pack:" + sha(*(i[0] for i in pack))[:12]
            state = [probes_done[i[0]] for i in pack]
            units.append((uid, sha(prompt_hash, model, body, can_probe, *state), list(pack)))
        pack, size = [], 0
    for item in items:
        n = len(item[1]) + len(item[2])
        if pack and (n > limits["reconcile_pack_small"] or size + n > limits["reconcile_pack_claims"]):
            flush()
        pack.append(item)
        size += n
        if n > limits["reconcile_pack_small"]:
            flush()
    flush()

    def worker(payload):
        return ctx.llm.complete_json("contracts", system, "\n\n".join(i[4] for i in payload))

    def apply(uid, payload, res):
        ctx.store.clear_unit("contracts", uid)
        by_subject = {i[0]: i for i in payload}
        keep = set()
        unresolved = []  # requirements the claims could not settle: the supply stage reads the producer

        def defer(subject, requirement, wants, producers):
            unresolved.append({"subject": subject, "requirement": requirement,
                               "demand": [c["id"] for c in wants], "producers": producers[:3]})
        for item in dicts(res.get("requirements")):
            subject = text_of(item.get("subject"), 300)
            if subject not in by_subject:
                subject = payload[0][0] if len(payload) == 1 else None
            status = pick(item.get("status"), CONTRACT_STATUS, None)
            requirement = text_of(item.get("requirement"), 300)
            if not subject or not status or status == "met" or not requirement:
                continue
            _, demand, supply, writers, _ = by_subject[subject]
            d_ids = {c["id"]: c for c in demand}
            s_ids = {c["id"]: c for c in supply}
            wants = [d_ids[i] for i in dict.fromkeys(as_list(item.get("demand_ids"))) if i in d_ids]
            gives = [s_ids[i] for i in dict.fromkeys(as_list(item.get("supply_ids"))) if i in s_ids]
            if status != "double" and not wants:
                continue
            consumers = {c["evidence"][0]["path"] for c in wants if c["evidence"]}
            producers = [w for w in sorted(writers) if w not in consumers]

            if status in ("unmet", "unclear"):
                if not producers:
                    # A function or class has no "writer"; the file that defines it is its producer.
                    name = subject.split(":", 1)[-1].split("#")[0]
                    defined = [s_["path"] for s_ in _search_repo(ctx, "", name) if s_["path"] not in consumers] \
                        if subject.startswith(("obj:", "api:")) else []
                    if defined:
                        defer(subject, requirement, wants, list(dict.fromkeys(defined)))
                        continue
                    # Nothing here produces it: the requirement falls on whoever supplies the data.
                    ctx.add_questions("contracts", uid, [{
                        "text": f"Nothing in this repository produces {subject}, and "
                                f"{', '.join(sorted(consumers)) or 'its readers'} require: {requirement}. "
                                "What guarantees this?",
                        "why": text_of(item.get("explanation"), 500)}],
                        subject=None, path=None, entity=subject)
                    continue
                asked = 0
                for writer in producers[:limits["supplier_probes"]]:
                    fp = sha("supplier", writer, subject, wants[0]["fingerprint"])
                    if ctx.store.rows("SELECT 1 FROM probes WHERE fingerprint=?", (fp,)):
                        continue
                    if can_probe:
                        ctx.store.run(
                            "INSERT OR IGNORE INTO probes (fingerprint,file,entity,question) VALUES (?,?,?,?)",
                            (fp, writer, subject,
                             f"A consumer of {subject} requires: {requirement}. Does this file make that "
                             f"true before it writes or returns {subject}? Record exactly what it does "
                             "establish about it (as kind \"ensures\"), or return nothing if it does not."))
                        asked += 1
                if asked:
                    continue  # wait for the producers' answer before calling it a gap
                if status == "unclear":
                    defer(subject, requirement, wants, producers)
                    continue
            kind = {"unmet": "unmet_demand", "mismatched": "mismatched_supply",
                    "double": "double_supply"}[status]
            cited = wants + gives if status != "double" else gives
            if status in ("mismatched", "double") and len({_origin(c) for c in cited}) < 2:
                continue
            fp = sha("contracts", kind, subject, *sorted(c["fingerprint"] for c in cited))
            keep.add(fp)
            ev = [e for c in cited for e in c["evidence"]][:6]
            if status == "unmet":  # show the verifier where the data is produced
                ev += [e for w in producers[:3] for e in writers[w][:1]]
            ctx.store.add_finding(
                fingerprint=fp, stage="contracts", unit=uid, type=kind,
                title=text_of(item.get("title"), 200) or f"{subject}: {requirement}",
                detail=(f"Requirement: {requirement}\n\n" + text_of(item.get("explanation"))),
                severity=pick(item.get("severity"), SEVERITIES, "medium"),
                confidence=pick(item.get("confidence"), CONFIDENCES, "medium"),
                claims=[c["id"] for c in cited], files=sorted({e["path"] for e in ev}),
                entity=subject, evidence=ev)
        ctx.store.sync_findings("contracts", uid, keep)
        pending = ctx.store.meta_get("unclear", {})
        pending[uid] = unresolved
        ctx.store.meta_set("unclear", pending)

    result = ctx.run_units("contracts", units, worker, apply)
    live = {u[0] for u in units}
    pending = ctx.store.meta_get("unclear", {})
    ctx.store.meta_set("unclear", {k: v for k, v in pending.items() if k in live})
    return result


def run_supply(ctx: Ctx) -> dict:
    """Settle the requirements that claims alone could not: show the producer's code next
    to what its consumers require and ask whether the code makes it true."""
    system, prompt_hash = ctx.prompt("supply")
    model = ctx.llm.model_for("supply")
    claims = {c["id"]: c for c in ctx.store.claims("kind='requires'")}
    by_subject: dict[str, list[dict]] = defaultdict(list)
    for items in ctx.store.meta_get("unclear", {}).values():
        for item in items:
            item = dict(item, demand=[claims[i] for i in item["demand"] if i in claims])
            if item["demand"] and item["producers"]:
                by_subject[item["subject"]].append(item)

    units = []
    for subject, items in sorted(by_subject.items()):
        items = items[:12]
        producers = list(dict.fromkeys(p for i in items for p in i["producers"]))[:2]
        asked = " ".join(i["requirement"] for i in items)
        body = [f"SUBJECT: {subject}", "REQUIREMENTS"]
        for n, item in enumerate(items, 1):
            first = item["demand"][0]
            where = first["evidence"][0] if first["evidence"] else {}
            body.append(f"R{n}. {item['requirement']}\n    required by {where.get('path', '?')}: "
                        f"{(where.get('quote') or '').strip()[:200]}")
        listed = []
        for rel in producers:
            listing, files_shown = code_context(ctx, rel, terms_for(subject, asked, rel), cap=260)
            body.append(f"\nPRODUCER: {rel}\n{listing}")
            listed += files_shown
        text = "\n".join(body)
        units.append((subject, sha(prompt_hash, model, text), (subject, items, producers, text, listed)))

    def worker(payload):
        return ctx.llm.complete_json("supply", system, payload[3])

    def apply(uid, payload, res):
        subject, items, producers, _, listed = payload
        ctx.store.clear_unit("supply", uid)
        keep = set()
        for check in dicts(res.get("checks")):
            m = re.match(r"R?(\d+)$", str(check.get("requirement", "")).strip())
            status = pick(check.get("status"), ("met", "unmet", "mismatched", "not_visible"), None)
            if not m or not 1 <= int(m.group(1)) <= len(items) or status not in ("unmet", "mismatched"):
                continue
            item = items[int(m.group(1)) - 1]
            shown = ctx.check_evidence(check.get("evidence"), producers[0])
            shown = [e for e in shown if e["path"] in listed]
            if status == "mismatched" and not shown:
                continue  # "does it differently" must point at the line that does it
            ev = [e for c in item["demand"] for e in c["evidence"]][:3] + shown[:3]
            if not shown:  # unmet: show the verifier where the producer lives
                ev += [{"path": producers[0], "line_start": 1, "line_end": 1, "quote": ""}]
            kind = "unmet_demand" if status == "unmet" else "mismatched_supply"
            fp = sha("supply", kind, subject, *sorted(c["fingerprint"] for c in item["demand"]))
            keep.add(fp)
            ctx.store.add_finding(
                fingerprint=fp, stage="supply", unit=uid, type=kind,
                title=text_of(check.get("title"), 200) or f"{subject}: {item['requirement']}",
                detail=f"Requirement: {item['requirement']}\n\n" + text_of(check.get("explanation")),
                severity=pick(check.get("severity"), SEVERITIES, "medium"),
                confidence=pick(check.get("confidence"), CONFIDENCES, "medium"),
                claims=[c["id"] for c in item["demand"]], files=sorted({e["path"] for e in ev}),
                entity=subject, evidence=ev)
        ctx.store.sync_findings("supply", uid, keep)

    return ctx.run_units("supply", units, worker, apply)


# ==========================================================================
# rules: things an owner says must hold everywhere
# ==========================================================================
def owner_rules(ctx: Ctx) -> list[dict]:
    own = ctx.store.claims("source='human' AND kind='rule'")
    from_docs = ctx.store.claims("source='docs' AND kind='rule' AND evidence_ok=1")
    return own + from_docs[:ctx.cfg["limits"]["max_doc_rules"]]


def run_rules(ctx: Ctx) -> dict:
    """Screen every file's recorded behaviour against each rule. A rule has no file
    of its own, so nothing would ever be compared with it otherwise. Suspects go to
    the verifier, which reads the code."""
    rules = owner_rules(ctx)
    if not rules:
        return ctx.run_units("rules", [], None, None)
    system, prompt_hash = ctx.prompt("rules")
    model = ctx.llm.model_for("rules")
    rule_ids = {f"R-{i}": r for i, r in enumerate(rules, 1)}
    rules_text = "RULES\n" + "\n".join(
        f"[{rid}] ({'maintainer' if r['source'] == 'human' else 'documentation'}) {r['statement']}"
        for rid, r in rule_ids.items())

    per_file: dict[str, list[dict]] = defaultdict(list)
    for claim in ctx.store.claims("source='llm' AND evidence_ok=1 AND subject LIKE 'file:%'"):
        if claim["kind"] != "purpose" or len(per_file[claim["subject"]]) == 0:
            per_file[claim["subject"]].append(claim)
    units, pack, size = [], [], 0
    def flush():
        nonlocal pack, size
        if pack:
            body = "\n\n".join(f"FILE: {subject[5:]}\n" + "\n".join(_format_claim(c) for c in claims)
                               for subject, claims in pack)
            uid = "pack:" + sha(*(subject for subject, _ in pack))[:12]
            units.append((uid, sha(prompt_hash, model, rules_text, body), (list(pack), body)))
        pack, size = [], 0
    for subject, claims in sorted(per_file.items()):
        claims = sorted(claims, key=lambda c: (_KIND_RANK.get(c["kind"], 9), c["n"]))[:40]
        if pack and size + len(claims) > 70:
            flush()
        pack.append((subject, claims))
        size += len(claims)
    flush()

    def worker(payload):
        return ctx.llm.complete_json("rules", system, rules_text + "\n\n" + payload[1])

    def apply(uid, payload, res):
        by_id = {c["id"]: c for _, claims in payload[0] for c in claims}
        keep = set()
        for item in dicts(res.get("suspects")):
            rule = rule_ids.get(text_of(item.get("rule_id"), 10))
            cited = [by_id[i] for i in dict.fromkeys(as_list(item.get("claim_ids"))) if i in by_id]
            title = text_of(item.get("title"), 200)
            if not rule or not cited or not title:
                continue
            path = cited[0]["evidence"][0]["path"]
            fp = sha("rule", rule["fingerprint"], path, *sorted(c["fingerprint"] for c in cited))
            keep.add(fp)
            ev = [e for c in cited for e in c["evidence"]][:6]
            ctx.store.add_finding(
                fingerprint=fp, stage="rules", unit=uid, type="rule_violation", title=title,
                detail=f"{rule['statement']}\n\n" + text_of(item.get("why")),
                severity=pick(item.get("severity"), SEVERITIES, "medium"), confidence="medium",
                claims=[c["id"] for c in cited] , files=sorted({e["path"] for e in ev}), evidence=ev)
        ctx.store.sync_findings("rules", uid, keep)

    return ctx.run_units("rules", units, worker, apply)


# ==========================================================================
# themes: confirmed findings grouped by what lies behind them
# ==========================================================================
def run_themes(ctx: Ctx) -> dict:
    confirmed = ctx.store.findings("status='confirmed'")
    if len(confirmed) < ctx.cfg["limits"]["themes_min_findings"]:
        ctx.store.meta_set("themes", [])
        return ctx.run_units("themes", [], None, None)
    system, prompt_hash = ctx.prompt("themes")
    model = ctx.llm.model_for("themes")
    body = "CONFIRMED FINDINGS\n" + "\n".join(
        f"[{f['id']}] ({f['type']}, {f['severity']}"
        + (f", about {f['entity']}" if f["entity"] else "")
        + f"; {', '.join(f['files'][:3])}) {f['title']}" for f in confirmed[:250])
    ids = {f["id"] for f in confirmed}

    def worker(_):
        return ctx.llm.complete_json("themes", system, body)

    def apply(uid, _, res):
        themes = []
        for item in dicts(res.get("themes")):
            members = [i for i in dict.fromkeys(as_list(item.get("finding_ids"))) if i in ids]
            if len(members) >= 2 and text_of(item.get("title"), 150):
                themes.append({"title": text_of(item.get("title"), 150),
                               "summary": text_of(item.get("summary"), 600), "findings": members})
        ctx.store.meta_set("themes", themes)

    return ctx.run_units("themes", [("themes", sha(prompt_hash, model, body), None)], worker, apply)


# ==========================================================================
# probes: a targeted second look at one file
# ==========================================================================
def pending_probes(ctx: Ctx) -> int:
    return ctx.store.rows("SELECT COUNT(*) c FROM probes WHERE status='pending'")[0]["c"]


def run_probes(ctx: Ctx) -> dict:
    system, prompt_hash = ctx.prompt("probe")
    model = ctx.llm.model_for("probe")
    files = ctx.skel["files"]
    units = []
    # Reconcile can ask for far more second looks than are worth paying for:
    # one per subject, up to a cap per round; the rest are dropped.
    cap, taken = ctx.cfg["limits"]["max_probes_per_round"], set()
    for probe in ctx.store.rows("SELECT * FROM probes WHERE status='pending'"):
        if probe["file"] not in files:
            ctx.store.run("UPDATE probes SET status='gone' WHERE n=?", (probe["n"],))
            continue
        if ctx.store.unit_hash("probe", f"P{probe['n']}") is None and (
                probe["entity"] in taken or len(taken) >= cap):
            ctx.store.run("UPDATE probes SET status='skipped' WHERE n=?", (probe["n"],))
            continue
        taken.add(probe["entity"])
        units.append((f"P{probe['n']}", sha(prompt_hash, model, probe["fingerprint"],
                                            files[probe["file"]]["sha"]), probe))

    def worker(probe):
        return ctx.llm.complete_json(
            "probe", system,
            f"FILE: {probe['file']}\nSUBJECT: {probe['entity']}\nQUESTION: {probe['question']}\n\n"
            f"LISTING (the parts of the file that mention the subject)\n"
            + code_context(ctx, probe["file"], terms_for(probe["entity"], probe["question"], probe["file"]),
                           cap=300, other_files=False)[0])

    def apply(uid, probe, res):
        rel = probe["file"]
        ctx.store.clear_unit("probe", uid)
        ctx.add_claims("probe", uid, res.get("claims"), subject=f"file:{rel}", path=rel)
        entity = None if probe["entity"].startswith("file:") else probe["entity"]
        ctx.add_questions("probe", uid, res.get("questions"), subject=f"file:{rel}", path=rel, entity=entity)
        ctx.store.run("UPDATE probes SET status='done' WHERE n=?", (probe["n"],))

    return ctx.run_units("probe", units, worker, apply, prune=False)


# ==========================================================================
# verification: try to refute every candidate
# ==========================================================================
def _text_search(ctx: Ctx, terms: set[str], limit: int = 15) -> dict[str, list[tuple[str, int, str]]]:
    hits: dict[str, list] = defaultdict(list)
    if not terms:
        return hits
    pattern = re.compile(r"(?<![\w])(" + "|".join(
        re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r")(?![\w])")
    for rel, rec in ctx.skel["files"].items():
        if rec["skipped"]:
            continue
        for i, line in enumerate(ctx.lines(rel), 1):
            for m in pattern.finditer(line):
                if len(hits[m.group(1)]) < limit * 3:
                    hits[m.group(1)].append((rel, i, line.strip()[:160]))
    return hits


def _search_term(rel: str) -> str:
    return PurePosixPath(rel).stem


def _relevant_notes(finding: dict, notes: list[dict], linked: set[str]) -> list[dict]:
    files = {f"file:{p}" for p in finding["files"]}
    out, general = [], 0
    for note in notes:
        if (note["unit"] in linked or note["subject"] in files
                or (finding["entity"] and note["entity"]
                    and base_entity(note["entity"]) == finding["entity"])):
            out.append(note)
        elif note["subject"] == "system" and not note["entity"] and general < 15:
            out.append(note)
            general += 1
    return out


def run_verify(ctx: Ctx) -> dict:
    system, prompt_hash = ctx.prompt("verify")
    model = ctx.llm.model_for("verify")
    limits, files = ctx.cfg["limits"], ctx.skel["files"]
    notes = ctx.human_notes(200)
    units = []
    for finding in ctx.store.findings("status IN ('candidate','needs_human')"):
        if finding["stage"] == "static" and not ctx.cfg["verify"]["static"]:
            continue
        if not finding["evidence"]:
            continue  # nothing to check against, e.g. a lead that names no file
        # Findings about something the owner said they care most about are always verified.
        text = (finding["title"] + " " + finding["detail"]).lower()
        in_focus = any(term.lower() in text for term in ctx.cfg["verify"]["focus_terms"])
        if (not in_focus and finding["type"] == "local_risk" and SEVERITIES.index(finding["severity"])
                > SEVERITIES.index(ctx.cfg["verify"]["local_risk_min_severity"])):
            continue  # minor single-file concerns are listed, not verified
        if (not in_focus and finding["type"] == "rule_violation" and SEVERITIES.index(finding["severity"])
                > SEVERITIES.index(ctx.cfg["verify"]["rule_min_severity"])):
            continue  # minor suspected rule breaches are listed, not verified
        if (not in_focus and finding["type"] == "branch_divergence" and SEVERITIES.index(finding["severity"])
                > SEVERITIES.index(ctx.cfg["verify"]["branch_min_severity"])):
            continue  # minor differences between branches are listed, not verified
        if (not in_focus and ctx.cfg["verify"]["skip_low_redundant"] and finding["severity"] == "low"
                and finding["type"] == "redundant_logic"):
            continue  # "same constant typed twice" is rarely worth a verifier call on a large repo
        linked = {q["id"] for q in ctx.store.questions("finding=? AND status='answered'", (finding["id"],))}
        units.append((finding, _relevant_notes(finding, notes, linked),
                      ctx.store.meta_get(f"more:{finding['fingerprint']}") or []))

    search = _text_search(ctx, {_search_term(f["files"][0]) for f, _, _ in units if f["type"] == "unused_file"})
    # The input is built up front and hashed whole: it includes callers from other files,
    # so a change anywhere in what the verifier sees is a reason to verify again.
    units = [(f["fingerprint"], sha(prompt_hash, model, body), (f, rel, extra, body))
             for f, rel, extra in units for body in [_verify_body(ctx, f, rel, extra, search)]]

    def worker(payload):
        return ctx.llm.complete_json("verify", system, payload[3])

    def apply(uid, payload, res):
        finding, _, extra, _ = payload
        verdict = pick(res.get("verdict"), {"confirmed", "refuted", "needs_human", "needs_code"}, None)
        if not verdict:
            raise ValueError("verifier returned no verdict")
        if verdict == "needs_code":
            # One extra look: fetch what it asked for, then verify again next pass.
            spans = [] if extra else _locate_requests(ctx, res.get("requests"))
            if spans:
                ctx.store.meta_set(f"more:{finding['fingerprint']}", spans)
                ctx.rerun_verify = True
                return
            verdict = "needs_human"
        ev = ctx.check_evidence(res.get("evidence"), None)
        status = verdict
        if verdict == "confirmed" and not ev and finding["type"] not in NO_QUOTE_TYPES:
            status = "unconfirmed"  # said yes but could not point at the code
        ctx.store.meta_set(f"verdict:{finding['fingerprint']}", {
            "status": status, "severity": finding["severity"], "reasoning": text_of(res.get("reasoning"), 1500)})
        title = text_of(res.get("title"), 200) if verdict == "confirmed" else ""
        detail = text_of(res.get("detail")) if verdict == "confirmed" else ""
        ctx.store.run(
            "UPDATE findings SET status=?, notes=?, severity=?, title=?, detail=?, evidence=? WHERE n=?",
            (status, text_of(res.get("reasoning"), 1500),
             pick(res.get("severity"), SEVERITIES, finding["severity"]),
             title or finding["title"], detail or finding["detail"],
             json.dumps(ev or finding["evidence"]), finding["n"]))
        ctx.store.run("DELETE FROM questions WHERE stage='verify' AND unit=? AND status='open'", (uid,))
        if verdict == "needs_human":
            question = res.get("question") if isinstance(res.get("question"), dict) else {}
            if len(text_of(question.get("text"))) < 8:
                question = {"text": f"Is this a real problem? {finding['title']}",
                            "why": text_of(res.get("reasoning"), 600)}
            ctx.add_questions("verify", uid, [question],
                              subject=f"file:{finding['files'][0]}" if finding["files"] else None,
                              path=None, entity=finding["entity"], finding=finding["id"])

    ctx.rerun_verify = False
    stats = ctx.run_units("verify", units, worker, apply)
    if ctx.rerun_verify:  # some findings asked for more code: one more pass for those
        ctx.rerun_verify = False
        return run_verify_again(ctx, stats)
    return stats


def _verify_body(ctx: Ctx, finding: dict, relevant: list[dict], extra: list[dict], search: dict) -> str:
    limits = ctx.cfg["limits"]
    out = ["CANDIDATE FINDING", f"type: {finding['type']}", f"title: {finding['title']}",
           f"detail: {finding['detail']}"]
    nums = [n for n in (id_num(c) for c in finding["claims"]) if n is not None]
    if nums:
        out.append("\nSUPPORTING CLAIMS (recorded by other workers; they may be wrong)")
        out += [_format_claim(c) for c in ctx.store.claims(f"n IN ({','.join(map(str, nums))})")]
    if relevant:
        out.append("\nMAINTAINER ANSWERS AND NOTES (authoritative about intent)")
        out += [f"- {n['statement']}" for n in relevant]
    if finding["type"] == "unused_file":
        target = finding["files"][0]
        found = [h for h in search.get(_search_term(target), []) if h[0] != target][:15]
        out.append(f"\nTEXT SEARCH for '{_search_term(target)}' in the rest of the repository")
        out += [f"- {p}:{i}: {t}" for p, i, t in found] or ["- no matches"]
    out.append("\nCODE EXCERPTS")
    if extra:
        out.append("(includes the additional code you asked for; no further requests are possible)")
    budget = limits["verify_max_lines"]
    graph = ctx.callgraph
    by_path: dict[str, list] = defaultdict(list)
    for ev in finding["evidence"] + extra:
        by_path[ev["path"]].append((ev["line_start"], ev["line_end"]))
    cited_functions = []  # (path, function) of the evidence, for showing callers afterwards
    shown: dict[str, list] = defaultdict(list)
    for path, spans in by_path.items():
        lines = ctx.lines(path)
        windows = []
        for a, b in spans:
            fn = graph.enclosing(path, a) if limits["call_graph"] else None
            if fn and fn["kind"] == "function" and fn["end"] - fn["line"] < limits["function_max_lines"] \
                    and fn["end"] >= b:
                windows.append((fn["line"], fn["end"]))  # the whole function the line sits in
                cited_functions.append((path, fn))
            else:
                windows += evidence.merge_windows([(a, b)], limits["verify_context_lines"], len(lines))
        for a, b in evidence.merge_windows(windows, 0, len(lines)):
            if budget <= 0:
                break
            b = min(b, a + budget - 1)
            out.append(f"### {path} (lines {a}-{b} of {len(lines)})")
            out.append(evidence.numbered(lines, a, b))
            shown[path].append((a, b))
            budget -= b - a + 1
    taken, seen = 0, set()
    for path, fn in cited_functions:  # one level of callers: how the cited code is used
        for site, line, caller in graph.callers(path, fn):
            if (budget <= 0 or taken >= limits["context_callers"] or (site, line) in seen
                    or any(a <= line <= b for a, b in shown[site])):
                continue
            seen.add((site, line))
            other = ctx.lines(site)
            box = graph.enclosing(site, line)
            a, b = max(line - 10, box["line"] if box else 1), min(line + 12, box["end"] if box else len(other))
            b = min(b, a + budget - 1)
            out.append(f"### {site} (lines {a}-{b} of {len(other)}): calls {fn['name']}"
                       + (f" from {caller}" if caller else ""))
            out.append(evidence.numbered(other, a, b))
            budget -= b - a + 1
            taken += 1
    return "\n".join(out)


SECOND_NOTE = "\n\nSecond verifier"


def run_second_opinion(ctx: Ctx) -> dict:
    """Check important verdicts again with a model from another family.

    Two models from one family tend to make the same mistakes. Where the second
    disagrees with the first, neither wins: the finding goes to a person."""
    model2 = ctx.cfg["models"].get("verifier_2")
    if not model2:
        return {"ran": 0, "skipped": 0, "failed": 0}
    system, prompt_hash = ctx.prompt("verify")
    model = ctx.llm.model_for("verify2")
    cutoff = SEVERITIES.index(ctx.cfg["verify"]["second_opinion_min_severity"])
    notes = ctx.human_notes(200)
    picked = []
    for finding in ctx.store.findings("status IN ('confirmed','refuted','needs_human')"):
        first = ctx.store.meta_get(f"verdict:{finding['fingerprint']}")
        if not first or first["status"] not in ("confirmed", "refuted"):
            continue
        # either verifier's view of severity qualifies it: a refuted finding is often downgraded
        if min(SEVERITIES.index(finding["severity"]), SEVERITIES.index(first["severity"])) > cutoff:
            continue
        picked.append((finding, first))
    search = _text_search(ctx, {_search_term(f["files"][0]) for f, _ in picked if f["type"] == "unused_file"})
    units = []
    for finding, first in picked:
        linked = {q["id"] for q in ctx.store.questions("finding=? AND status='answered'", (finding["id"],))}
        extra = ctx.store.meta_get(f"more:{finding['fingerprint']}") or []
        body = _verify_body(ctx, finding, _relevant_notes(finding, notes, linked), extra, search) + (
            "\n\nThis is an independent second check. No further code can be requested: "
            "if the code shown does not settle it, answer needs_human.")
        units.append((finding["fingerprint"], sha(prompt_hash, model, body, first["status"]),
                      (finding, first, body)))

    def worker(payload):
        return ctx.llm.complete_json("verify2", system, payload[2])

    def apply(uid, payload, res):
        finding, first, _ = payload
        ctx.store.clear_unit("verify2", uid)
        verdict = pick(res.get("verdict"), {"confirmed", "refuted", "needs_human", "needs_code"}, "needs_human")
        second = {"model": model2, "verdict": verdict, "reasoning": text_of(res.get("reasoning"), 1500)}
        ctx.store.meta_set(f"second:{uid}", second)
        if verdict in ("confirmed", "refuted") and verdict != first["status"]:
            ctx.add_questions("verify2", uid, [{
                "text": f"Two independent checks disagree on whether this is a real problem: {finding['title']}",
                "why": f"First check ({first['status']}): {first['reasoning']}\n"
                       f"Second check ({verdict}): {second['reasoning']}"}],
                subject=f"file:{finding['files'][0]}" if finding["files"] else None,
                path=None, entity=finding["entity"], finding=finding["id"])

    stats = ctx.run_units("verify2", units, worker, apply)
    # Settle every picked finding from the stored verdicts, including those whose second check was
    # cached: the first verifier may have run again and reset the status since.
    for finding, first in picked:
        second = ctx.store.meta_get(f"second:{finding['fingerprint']}")
        if not second:
            continue
        current = ctx.store.findings("n=?", (finding["n"],))[0]
        base = (current["notes"] or "").split(SECOND_NOTE)[0]
        if second["verdict"] == first["status"]:
            note, status = f"{SECOND_NOTE} ({second['model']}) agreed.", first["status"]
        elif second["verdict"] in ("confirmed", "refuted"):
            note = f"{SECOND_NOTE} ({second['model']}) disagreed: {second['reasoning']}"
            status = "needs_human"
        else:
            note, status = f"{SECOND_NOTE} ({second['model']}) could not decide.", first["status"]
        ctx.store.run("UPDATE findings SET status=?, notes=? WHERE n=?", (status, base + note, finding["n"]))
    return stats


def run_verify_again(ctx: Ctx, first: dict) -> dict:
    second = run_verify(ctx)
    return {k: first.get(k, 0) + second.get(k, 0) for k in set(first) | set(second)}


def _search_repo(ctx: Ctx, folder: str, needle: str, limit: int = 3) -> list[dict]:
    """Find where a name is defined (else used) under a folder, when no file was named."""
    names = [w for w in re.split(r"[^A-Za-z0-9_.]+", needle) if len(w) >= 4 and w not in ("def", "class")]
    if not names:
        return []
    folder = folder.strip("/ ").replace("\\", "/")
    word = names[-1].split(".")[-1] if names[-1].split(".")[-1] not in ("json", "csv", "py") else names[-1]
    defs, uses = [], []
    for rel, rec in ctx.skel["files"].items():
        if rec["skipped"] or rec["role"] not in ("code", "config") or (folder and not rel.startswith(folder)):
            continue
        lines = ctx.lines(rel)
        for i, line in enumerate(lines, 1):
            if word not in line:
                continue
            span = {"path": rel, "line_start": max(1, i - 5), "line_end": min(len(lines), i + 60), "quote": ""}
            if re.search(rf"\\b(def|class)\\s+{re.escape(word)}\\b", line):
                defs.append(span)
            elif len(uses) < 40:
                uses.append(span)
    seen, out = set(), []
    for span in defs + uses:  # definitions first, then one use per file
        if span["path"] in seen and span not in defs:
            continue
        seen.add(span["path"])
        out.append(span)
        if len(out) >= limit:
            break
    return out


def _locate_requests(ctx: Ctx, raw) -> list[dict]:
    """Turn a verifier's request for more code ('this file, around this text') into line spans."""
    spans = []
    for item in dicts(raw)[:4]:
        path = ctx.resolve_path(item.get("path"))
        if not path:
            # "Somewhere under Code/, wherever summary.json is written": search for it.
            spans += _search_repo(ctx, text_of(item.get("path"), 200), text_of(item.get("find"), 200))
            continue
        lines = ctx.lines(path)
        needle = text_of(item.get("find"), 200)
        # Prefer the definition of any name mentioned (last name first: Class.method -> method),
        # then the literal text, then a line number if one was given.
        names = [w for w in re.split(r"[^A-Za-z0-9_]+", needle) if len(w) >= 3 and w not in ("def", "class")]
        hits = []
        for word in reversed(names):
            hits = [i for i, line in enumerate(lines, 1)
                    if re.search(rf"\b(def|class)\s+{re.escape(word)}\b|^\s*{re.escape(word)}\s*=", line)][:2]
            if hits:
                break
        if not hits:
            hits = [i for i, line in enumerate(lines, 1) if needle and needle in line][:3]
        if not hits and names:
            hits = [i for i, line in enumerate(lines, 1) if names[-1] in line][:3]
        if not hits and isinstance(item.get("line_start"), int):
            hits = [item["line_start"]]
        for hit in hits:
            spans.append({"path": path, "line_start": max(1, hit - 5), "line_end": min(len(lines), hit + 80), "quote": ""})
    return spans


# ==========================================================================
# questions for people
# ==========================================================================
def open_questions(ctx_or_store) -> list[dict]:
    store = getattr(ctx_or_store, "store", ctx_or_store)
    waiting = {f["id"] for f in store.findings("status='needs_human'")}
    return [q for q in store.questions("status='open'") if not q["finding"] or q["finding"] in waiting]


def run_questions(ctx: Ctx) -> dict:
    """Triage: merge duplicates, drop what the code can answer, mark what matters most."""
    system, prompt_hash = ctx.prompt("questions")
    model = ctx.llm.model_for("questions")
    pending = sorted(open_questions(ctx), key=lambda q: (q["entity"] or "", q["subject"] or "", q["n"]))
    units = []
    if pending:
        for i in range(0, len(pending), 60):
            batch = pending[i:i + 60]
            body = "QUESTIONS\n" + "\n".join(
                f"[{q['id']}] (raised by: {q['stage']} wave; about {q['entity'] or q['subject'] or 'the system'}"
                + ("; decides a finding" if q["finding"] else "") + f") {q['text']}"
                + (f"\n    why: {q['why']}" if q["why"] else "") for q in batch)
            units.append((f"batch{i // 60}", sha(prompt_hash, model, body), (batch, body)))

    def worker(payload):
        return ctx.llm.complete_json("questions", system, payload[1])

    def apply(uid, payload, res):
        by_id = {q["id"]: q for q in payload[0]}
        for group in dicts(res.get("merge")):
            keep = group.get("keep")
            if keep not in by_id:
                continue
            for dup in as_list(group.get("duplicates")):
                if dup in by_id and dup != keep:
                    ctx.store.run("UPDATE questions SET status='merged', merged_into=? "
                                  "WHERE n=? AND status='open'", (keep, id_num(dup)))
        for qid in as_list(res.get("drop")):
            # A question that decides a finding is never dropped by triage.
            if qid in by_id and not by_id[qid]["finding"]:
                ctx.store.run("UPDATE questions SET status='dropped' WHERE n=? AND status='open'",
                              (id_num(qid),))
        for qid in as_list(res.get("important")):
            if qid in by_id:
                ctx.store.run("UPDATE questions SET priority='high' WHERE n=?", (id_num(qid),))

    return ctx.run_units("questions", units, worker, apply)
