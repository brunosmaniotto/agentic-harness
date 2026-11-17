"""Blocks and branches: the wave between files and the whole system.

A file worker sees one file; a path worker sees a whole pipeline but only as
summaries. Neither notices two branches that start from the same data and are
supposed to treat it the same way (training and scoring, backtest and production,
every model in a horse race) but do not: each file looks fine on its own.

Blocks are cut where the data lineage forks. For a piece of data read by several
files, each reader and the files downstream of it that no other reader also
feeds form one branch. The block wave describes each branch as an ordered list of
steps applied to the shared input, each step quoting its line. The branch stage
then lines the branches of one fork up side by side, decides from the code, the
documentation and the maintainers' rules which of them should treat the input
the same way, and reports where those differ.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque

from .context import code_context, terms_for
from .core import CONFIDENCES, SEVERITIES, Ctx, as_list, dicts, pick, sha, text_of
from .names import base_entity

DATA_PREFIXES = ("table:", "file:", "topic:", "api:")
# Outputs for people, not inputs for code: a figure read by two scripts is not a fork.
NOT_INPUTS = re.compile(r"\.(png|jpe?g|svg|pdf|gif|html?|log|tex|md|txt)$", re.I)
DIVERGENCE_KINDS = ("missing_step", "different_order", "different_parameter", "different_definition")


def _code_files(ctx: Ctx) -> set[str]:
    return {rel for rel, rec in ctx.skel["files"].items()
            if not rec["skipped"] and rec["role"] in ("code", "config")}


def data_flow(ctx: Ctx) -> tuple[dict[str, set], dict[str, set], dict[str, dict[str, int]]]:
    """Who reads and who writes each piece of data: static analysis plus what the
    file wave recorded as inputs and outputs. Returns readers, writers, and the line
    where each reader reads each entity (when known)."""
    from .stages import USABLE, alias_map
    code = _code_files(ctx)
    aliases = alias_map(ctx)
    canon = lambda key: aliases.get(key, key)
    readers: dict[str, set] = defaultdict(set)
    writers: dict[str, set] = defaultdict(set)
    read_at: dict[str, dict[str, int]] = defaultdict(dict)
    for key, entry in ctx.skel["entities"].items():
        key = canon(key)
        if not key.startswith(DATA_PREFIXES):
            continue
        for rel, line in entry["readers"]:
            if rel in code:
                readers[key].add(rel)
                read_at[key].setdefault(rel, line)
        for rel, _ in entry["writers"]:
            if rel in code:
                writers[key].add(rel)
    for claim in ctx.store.claims(USABLE + " AND kind IN ('input','output') AND entity IS NOT NULL"
                                           " AND subject LIKE 'file:%'"):
        key, rel = canon(base_entity(claim["entity"])), claim["subject"][5:]
        if not key.startswith(DATA_PREFIXES) or rel not in code:
            continue
        (readers if claim["kind"] == "input" else writers)[key].add(rel)
        if claim["kind"] == "input" and claim["evidence"]:
            read_at[key].setdefault(rel, claim["evidence"][0]["line_start"])
    for key in list(readers):  # a repository file read by path is written by itself
        if key.startswith("file:") and key[5:] in code:
            writers[key].add(key[5:])
    return readers, writers, read_at


def forks(ctx: Ctx) -> list[dict]:
    """Points where the lineage splits, each with its branches (lists of files, head first)."""
    limits = ctx.cfg["limits"]
    readers, writers, read_at = data_flow(ctx)
    downstream: dict[str, set] = defaultdict(set)
    for key, ws in writers.items():
        for w in ws:
            downstream[w] |= readers.get(key, set()) - {w}

    def reach(head: str) -> list[str]:
        order, seen, queue = [head], {head}, deque([(head, 0)])
        while queue and len(order) < limits["block_max_files"]:
            rel, depth = queue.popleft()
            if depth >= 4:
                continue
            for nxt in sorted(downstream.get(rel, ())):
                if nxt not in seen and len(order) < limits["block_max_files"]:
                    seen.add(nxt)
                    order.append(nxt)
                    queue.append((nxt, depth + 1))
        return order

    by_readers: dict[frozenset, list[str]] = defaultdict(list)
    for key, rs in readers.items():
        name = key.split(":", 1)[1]
        if len(rs) >= 2 and not NOT_INPUTS.search(name):
            by_readers[frozenset(rs)].append(key)

    out = []
    for heads, keys in by_readers.items():
        heads = sorted(heads)
        reached = {h: reach(h) for h in heads}
        count: dict[str, int] = defaultdict(int)
        for files in reached.values():
            for rel in set(files):
                count[rel] += 1
        branches, seen = [], set()
        for h in heads:
            # a branch is its head plus what only it feeds; shared downstream files are joins
            files = [h] + [r for r in reached[h][1:] if count[r] == 1 and r not in heads]
            if tuple(files) not in seen:
                seen.add(tuple(files))
                branches.append(files)
        if len(branches) < 2:
            continue
        branches.sort(key=lambda b: (-len(b), b[0]))
        branches = branches[:limits["max_branches"]]
        joins = sorted(r for r, c in count.items() if c > 1 and r not in heads)
        out.append({"id": "K-" + sha(*sorted(keys), *heads)[:10], "inputs": sorted(keys),
                    "branches": branches, "joins": joins,
                    "read_at": {k: read_at.get(k, {}) for k in keys},
                    "score": len(branches) * (1 + sum(len(b) for b in branches))})
    out.sort(key=lambda f: (-f["score"], f["id"]))
    return out[:limits["max_forks"]]


_STOP = set("the a an in of to and but not is are by from with while at for on as than other others "
             "both one two three all shown".split())


def _title_words(title: str, files: list[str]) -> set[str]:
    stems = {re.sub(r"\.\w+$", "", p.split("/")[-1]).lower() for p in files}
    words = {w for w in re.findall(r"[a-z0-9_.\[\]-]+", title.lower().replace(".py", ""))
             if w not in _STOP and len(w) > 2}
    return words - stems  # file names are shared by every difference between the same files


def same_difference(a: dict, b: dict) -> bool:
    """Two branch findings that describe one difference, found from overlapping forks.
    Same files (or one set inside the other) and similar wording once file names are set aside."""
    fa, fb = set(a["files"]), set(b["files"])
    if len(fa & fb) < 2 and not (fa <= fb or fb <= fa):
        return False
    ta, tb = _title_words(a["title"], a["files"]), _title_words(b["title"], b["files"])
    return len(ta & tb) / max(1, len(ta | tb)) >= 0.25


def block_id(files: list[str]) -> str:
    return "B-" + sha(*files)[:10]


def _block_body(ctx: Ctx, files: list[str], inputs: list[str], read_lines: dict[str, int]) -> str:
    from .stages import USABLE, _format_claim
    limits = ctx.cfg["limits"]
    body = [f"STARTS FROM: {', '.join(inputs)}", "FILES (in data-flow order)"]
    for i, rel in enumerate(files, 1):
        io = ctx.skel["files"][rel]["io"]
        reads = sorted({t["entity"] for t in io if t["dir"] == "read" and t["entity"]})[:6]
        writes = sorted({t["entity"] for t in io if t["dir"] == "write" and t["entity"]})[:6]
        body.append(f"{i}. {rel}" + (f"; reads {', '.join(reads)}" if reads else "")
                    + (f"; writes {', '.join(writes)}" if writes else ""))
    wanted = ("purpose", "step", "behavior", "assumption", "requires", "ensures", "input", "output")
    claims = []
    for rel in files:
        claims += [c for c in ctx.store.claims(USABLE + " AND subject=? AND stage != 'blocks'", (f"file:{rel}",))
                   if c["kind"] in wanted]
    words = [w for k in inputs for w in terms_for(k)]
    claims.sort(key=lambda c: (not any(w in (c["statement"] + (c["entity"] or "")).lower() for w in words),
                               wanted.index(c["kind"])))
    body.append("\nWHAT THE FILE WAVE RECORDED ABOUT THESE FILES (may be wrong)")
    body += [_format_claim(c) for c in claims[:50]] or ["(nothing)"]
    body.append("\nCODE")
    # Every file of the branch gets a share: the head (where the shared input is read)
    # the largest, the rest evenly. A branch described from its first file alone is
    # described from the notes for the rest, and the notes are what this wave is checking.
    total = limits["block_code_lines"]
    head = total if len(files) == 1 else int(total * 0.45)
    rest = max(40, (total - head) // max(1, len(files) - 1))
    for i, rel in enumerate(files):
        anchors = [read_lines[rel]] if rel in read_lines else []
        listing, _ = code_context(ctx, rel, words, cap=head if i == 0 else rest, anchors=anchors,
                                  other_files=(i == 0))
        body.append(listing)
    return "\n".join(body)


def run_blocks(ctx: Ctx) -> dict:
    system, prompt_hash = ctx.prompt("blocks")
    model = ctx.llm.model_for("blocks")
    found = forks(ctx)
    ctx.store.meta_set("forks", found)
    blocks: dict[str, dict] = {}
    for fork in found:
        for files in fork["branches"]:
            entry = blocks.setdefault(block_id(files), {"files": files, "inputs": set(), "read": {}})
            entry["inputs"] |= set(fork["inputs"])
            for key in fork["inputs"]:
                if files[0] in fork["read_at"].get(key, {}):
                    entry["read"].setdefault(files[0], fork["read_at"][key][files[0]])
    units = []
    for bid, entry in sorted(blocks.items()):
        body = _block_body(ctx, entry["files"], sorted(entry["inputs"]), entry["read"])
        units.append((bid, sha(prompt_hash, model, body), (entry["files"], body)))

    def worker(payload):
        return ctx.llm.complete_json("blocks", system, f"BLOCK\n{payload[1]}")

    def apply(uid, payload, res):
        files, _ = payload
        ctx.store.clear_unit("blocks", uid)
        steps = []
        for item in dicts(res.get("steps"))[:25]:
            desc = text_of(item.get("description"), 400)
            if not desc:
                continue
            ev = ctx.check_evidence(item.get("evidence"), files[0])
            params = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
            steps.append({"description": desc, "evidence": [e for e in ev if e["path"] in files][:2] or ev[:2],
                          "parameters": {text_of(k, 60): text_of(v, 120) for k, v in list(params.items())[:8]}})
        ctx.store.meta_set(f"block:{uid}", {"files": files, "purpose": text_of(res.get("purpose"), 500),
                                            "steps": steps})
        # On the board as well, under the block: not tied to an entity, so reconcile and
        # contracts (which group by entity) are not disturbed.
        ctx.add_claims("blocks", uid, [
            {"kind": "step", "statement": f"{uid} step {i}: {s['description']}", "evidence": s["evidence"]}
            for i, s in enumerate(steps, 1)], subject=f"block:{uid}", path=files[0])
        ctx.add_questions("blocks", uid, res.get("questions"), subject=f"file:{files[0]}", path=files[0])

    return ctx.run_units("blocks", units, worker, apply)


def _branch_card(label: str, bid: str, block: dict) -> str:
    out = [f"{label} ({bid}): {' -> '.join(block['files'])}",
           f"  purpose: {block['purpose'] or '(not stated)'}"]
    for i, step in enumerate(block["steps"], 1):
        where = f" (at {step['evidence'][0]['path']}:{step['evidence'][0]['line_start']})" if step["evidence"] else ""
        params = "; ".join(f"{k}={v}" for k, v in step["parameters"].items())
        out.append(f"  {label}.S{i}. {step['description']}" + (f" [{params}]" if params else "") + where)
    if not block["steps"]:
        out.append("  (no steps recorded)")
    return "\n".join(out)


def run_branches(ctx: Ctx) -> dict:
    system, prompt_hash = ctx.prompt("branches")
    model = ctx.llm.model_for("branches")
    rules = [c["statement"] for c in ctx.store.claims("kind='rule' AND (evidence_ok=1 OR source='human')")][:15]
    units = []
    for fork in ctx.store.meta_get("forks", []):
        labelled = []
        for i, files in enumerate(fork["branches"], 1):
            block = ctx.store.meta_get(f"block:{block_id(files)}")
            if block:
                labelled.append((f"B{i}", block_id(files), block))
        if len(labelled) < 2:
            continue
        body = (f"SHARED INPUT: {', '.join(fork['inputs'])}\n"
                + (f"FILES FED BY MORE THAN ONE BRANCH (joins): {', '.join(fork['joins'])}\n" if fork["joins"] else "")
                + "\nRULES STATED BY MAINTAINERS OR DOCUMENTATION\n"
                + ("\n".join(f"- {r}" for r in rules) or "(none)")
                + "\n\nBRANCHES\n" + "\n\n".join(_branch_card(*x) for x in labelled))
        units.append((fork["id"], sha(prompt_hash, model, body), (fork, labelled, body)))

    def worker(payload):
        return ctx.llm.complete_json("branches", system, payload[2])

    def apply(uid, payload, res):
        fork, labelled, _ = payload
        by_label = {label: (bid, block) for label, bid, block in labelled}
        ctx.store.clear_unit("branches", uid)
        groups = []
        for g in dicts(res.get("groups")):
            members = [b for b in as_list(g.get("branches")) if b in by_label]
            if len(members) >= 2:
                groups.append({"branches": [by_label[b][0] for b in members],
                               "expectation": pick(g.get("expectation"), ("should_match", "may_differ", "unclear"),
                                                   "unclear"),
                               "why": text_of(g.get("why"), 400)})
        ctx.store.meta_set(f"fork_result:{uid}", groups)
        keep = set()
        known = {rel.rsplit("/", 1)[-1] for _, _, block in labelled for rel in block["files"]}
        known |= {k.split(":", 1)[1].rsplit("/", 1)[-1] for k in fork["inputs"]} | set(fork["joins"])
        for d in dicts(res.get("divergences")):
            labels = [b for b in dict.fromkeys(as_list(d.get("branches"))) if b in by_label]
            if len(labels) < 2:
                continue
            named = set(re.findall(r"[\w.-]+\.(?:py|sql|r|R|ipynb|js|ts|scala)\b", str(d.get("title", ""))))
            if named and not named & known:
                continue  # about files that are in none of these branches: an answer to another input
            refs, ev = [], []
            for ref in as_list(d.get("steps")):
                m = re.match(r"(B\d+)\.S(\d+)$", str(ref).strip())
                if not m or m.group(1) not in by_label:
                    continue
                steps = by_label[m.group(1)][1]["steps"]
                k = int(m.group(2))
                if 1 <= k <= len(steps):
                    refs.append(f"{m.group(1)}.S{k}")
                    ev += steps[k - 1]["evidence"]
            if not ev:
                continue  # a difference has to point at code on at least one side
            have = {e["path"] for e in ev}
            for label in labels:  # a branch that lacks the step: show where it reads the input
                files = by_label[label][1]["files"]
                if not have & set(files):
                    line = next((fork["read_at"][k][files[0]] for k in fork["inputs"]
                                 if files[0] in fork["read_at"].get(k, {})), 1)
                    ev.append({"path": files[0], "line_start": line, "line_end": line, "quote": ""})
            kind = pick(d.get("kind"), DIVERGENCE_KINDS, "different_definition")
            ids = sorted(by_label[b][0] for b in labels)
            # Forks overlap (a file reading two shared inputs is in both), so the same difference
            # can be found from several of them: identify it by the code it points at.
            fp = sha("branches", kind, *sorted({f"{e['path']}:{e['line_start']}" for e in ev}))
            title = text_of(d.get("title"), 200) or f"Branches from {fork['inputs'][0]} differ"
            files_ = sorted({e["path"] for e in ev})
            if fp in keep or any(same_difference({"title": title, "files": files_}, other) for other in
                                 ctx.store.findings("type='branch_divergence' AND unit != ?", (uid,))
                                 + [f for f in ctx.store.findings("unit=? AND stage='branches'", (uid,))
                                    if f["fingerprint"] in keep]):
                continue
            keep.add(fp)
            cards = "\n".join(f"{b}: {' -> '.join(by_label[b][1]['files'])}" for b in labels)
            expectation = next((g["expectation"] for g in groups
                                if set(ids) <= set(g["branches"])), "unclear")
            ctx.store.add_finding(
                fingerprint=fp, stage="branches", unit=uid, type="branch_divergence",
                title=title,
                detail=f"Shared input: {', '.join(fork['inputs'])}\n{cards}\nSteps: {', '.join(refs)}\n"
                       f"Expected to match: {expectation.replace('_', ' ')}\n\n" + text_of(d.get("explanation")),
                severity=pick(d.get("severity"), SEVERITIES, "medium"),
                confidence=pick(d.get("confidence"), CONFIDENCES, "medium"),
                claims=[], files=files_, entity=fork["inputs"][0], evidence=ev[:8])
        ctx.store.sync_findings("branches", uid, keep)
        ctx.add_questions("branches", uid, res.get("questions"), subject="system", path=None,
                          entity=fork["inputs"][0])

    return ctx.run_units("branches", units, worker, apply)
