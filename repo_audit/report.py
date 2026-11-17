"""Turn the board into things people read: report.md, questions.md, findings.json."""
from __future__ import annotations

import json
import time
from collections import Counter

from .core import Ctx
from .dashboard import TYPE_LABEL, write_dashboard
from .human import PLACEHOLDER
from .stages import open_questions, pipelines

_SEV = {"high": 0, "medium": 1, "low": 2}
# Labels live with the dashboard, which report.py imports; the other way round would be circular.
_TYPE_LABEL = TYPE_LABEL


def _evidence_lines(evidence: list[dict], limit: int = 6) -> list[str]:
    out = []
    for ev in evidence[:limit]:
        span = str(ev["line_start"]) if ev["line_start"] == ev["line_end"] else f"{ev['line_start']}-{ev['line_end']}"
        quote = " ".join((ev.get("quote") or "").split())
        out.append(f"- `{ev['path']}:{span}`" + (f" — `{quote[:200]}`" if quote else ""))
    if len(evidence) > limit:
        out.append(f"- … and {len(evidence) - limit} more location(s)")
    return out


def _finding_block(f: dict) -> list[str]:
    out = [f"### {f['id']} · {f['title']}",
           f"**{_TYPE_LABEL.get(f['type'], f['type'])}** · severity {f['severity']}"
           + (f" · about `{f['entity']}`" if f["entity"] else ""),
           "", f["detail"] or "", ""]
    out += _evidence_lines(f["evidence"])
    if f["notes"]:
        out += ["", f"*Verifier:* {f['notes']}"]
    out.append("")
    return out


def _question_block(q: dict, waiting: dict, merged_count) -> list[str]:
    out = [f"## {q['id']}", "", f"**Question:** {q['text']}", ""]
    if q["why"]:
        out += [f"**Why it matters:** {q['why']}", ""]
    about = q["entity"] or (q["subject"] if q["subject"] not in (None, "system") else "")
    if about:
        out += [f"**About:** `{about}`", ""]
    if q["finding"] in waiting:
        out += [f"**Decides:** {q['finding']} — {waiting[q['finding']]['title']}", ""]
    if q["evidence"]:
        out += _evidence_lines(q["evidence"], 3) + [""]
    if merged_count[q["id"]]:
        out += [f"*{merged_count[q['id']]} similar question(s) were folded into this one.*", ""]
    return out + [f"**Answer:** {PLACEHOLDER}", ""]


def write_questions(ctx: Ctx) -> int:
    """questions.md holds the ones worth a person's time; questions_all.md holds the rest."""
    waiting = {f["id"]: f for f in ctx.store.findings("status='needs_human'")}
    pending = open_questions(ctx)
    merged_count = Counter(q["merged_into"] for q in ctx.store.questions("status='merged'"))
    pending.sort(key=lambda q: (q["finding"] not in waiting, q.get("priority") != "high",
                                -merged_count[q["id"]], q["n"]))
    top = [q for q in pending if q["finding"] in waiting or q.get("priority") == "high"]
    rest = [q for q in pending if q not in top]
    how = ("Type an answer under any question you can answer, leave the rest untouched, then run "
           "`repo-audit answers <this file>` followed by `repo-audit run`. Only the parts of the "
           "audit that depend on your answers are redone.")
    out = ["# Questions for people who know this repository", "",
           "The audit could not settle these from the code alone. " + how, "",
           f"{len(top)} question(s) here: those that decide a finding, then those triage marked "
           f"as important. {len(rest)} lower-priority question(s) are in `questions_all.md`.", ""]
    for q in top:
        out += _question_block(q, waiting, merged_count)
    (ctx.out / "questions.md").write_text("\n".join(out), encoding="utf-8")
    out = ["# Lower-priority questions", "", how, ""]
    for q in rest:
        out += _question_block(q, waiting, merged_count)
    (ctx.out / "questions_all.md").write_text("\n".join(out), encoding="utf-8")
    return len(top)


def write_report(ctx: Ctx, stopped: str = "") -> None:
    store, skel = ctx.store, ctx.skel
    stats = skel["stats"]
    findings = store.findings()
    by_status: dict[str, list[dict]] = {}
    for f in findings:
        by_status.setdefault(f["status"], []).append(f)
    for group in by_status.values():
        group.sort(key=lambda f: (_SEV.get(f["severity"], 3), f["n"]))
    confirmed = by_status.get("confirmed", [])
    n_questions = write_questions(ctx)

    file_units = store.rows("SELECT unit, status FROM units WHERE stage='files'")
    files_read = len({u["unit"].rsplit("#", 1)[0] for u in file_units if u["status"] == "done"})
    failed = store.rows("SELECT stage, COUNT(*) c FROM units WHERE status='failed' GROUP BY stage")
    claims_total = store.rows("SELECT COUNT(*) c FROM claims")[0]["c"]
    claims_bad = store.rows("SELECT COUNT(*) c FROM claims WHERE evidence_ok=0")[0]["c"]
    usage = store.usage()

    out = [f"# Repository audit: {skel['repo']}", "",
           f"Generated {time.strftime('%Y-%m-%d %H:%M')}" + (f" at commit `{skel['head'][:12]}`" if skel["head"] else "") + ".", ""]
    if stopped:
        out += [f"> **This run stopped early:** {stopped}. What follows is what had been "
                "established by then. Run again to continue from where it stopped.", ""]

    out += ["## Summary", "",
            f"- **{len(confirmed)} confirmed finding(s)**: "
            + (", ".join(f"{n} {s}" for s, n in sorted(Counter(f["severity"] for f in confirmed).items(),
                                                       key=lambda kv: _SEV.get(kv[0], 3))) or "none"),
            f"- **{n_questions} question(s)** for maintainers in `questions.md` "
            f"({len(open_questions(ctx)) - n_questions} more in `questions_all.md`), "
            f"{len(by_status.get('needs_human', []))} finding(s) waiting on them",
            f"- {len(by_status.get('refuted', []))} candidate(s) refuted by verification, "
            f"{len(by_status.get('unconfirmed', []))} unconfirmed, "
            f"{len(by_status.get('candidate', []))} not yet verified",
            f"- Coverage: {files_read} file(s) read in detail out of {stats['analyzed']} analyzed "
            f"({stats['files']} in the repository, {stats['lines']} lines)",
            f"- Board: {claims_total} claim(s); {claims_bad} discarded because the quoted "
            "evidence was not found in the file",
            f"- Model usage: {usage['calls']} call(s), {usage['tokens']:,} tokens"
            + (f", ${usage['usd']:.2f}" if usage["usd"] else "")]
    seconds = [store.meta_get(f"second:{f['fingerprint']}") for f in findings]
    seconds = [s_ for s_ in seconds if s_]
    if seconds:
        first = {f["fingerprint"]: store.meta_get(f"verdict:{f['fingerprint']}") or {} for f in findings}
        agree = sum(1 for f in findings for s_ in [store.meta_get(f"second:{f['fingerprint']}")]
                    if s_ and s_["verdict"] == first[f["fingerprint"]].get("status"))
        split = sum(1 for f in findings for s_ in [store.meta_get(f"second:{f['fingerprint']}")]
                    if s_ and s_["verdict"] in ("confirmed", "refuted")
                    and s_["verdict"] != first[f["fingerprint"]].get("status"))
        out.append(f"- Second verifier ({seconds[0]['model']}): checked {len(seconds)} finding(s); "
                   f"agreed on {agree}, disagreed on {split} (sent to maintainers), "
                   f"undecided on {len(seconds) - agree - split}")
    forks = store.meta_get("forks") or []
    if forks:
        verdicts = [g for fk in forks for g in (store.meta_get(f"fork_result:{fk['id']}") or [])]
        out.append(f"- Branches: {len(forks)} point(s) where the data flow splits, "
                   f"{sum(len(fk['branches']) for fk in forks)} branch(es) described; "
                   f"{sum(1 for g in verdicts if g['expectation'] == 'should_match')} group(s) judged "
                   f"meant to match, {sum(1 for g in verdicts if g['expectation'] == 'unclear')} unclear")
    out.append("")
    v = ctx.cfg["verify"]
    held = [f for f in by_status.get("candidate", [])
            if (f["type"] == "local_risk" and _SEV.get(f["severity"], 3) > _SEV[v["local_risk_min_severity"]])
            or (v["skip_low_redundant"] and f["type"] == "redundant_logic" and f["severity"] == "low")]
    if held:
        out += [f"{len(held)} of the unverified candidates were left unverified on purpose by the "
                f"configuration: single-file concerns rated below {v['local_risk_min_severity']}"
                + (", and low-severity repeated constants" if v["skip_low_redundant"] else "")
                + ". They are listed under \"Not confirmed\" and in `findings.json`. Lower "
                "`[verify] local_risk_min_severity` to have them checked.", ""]
    if failed:
        out += ["Some units failed and will be retried on the next run: "
                + ", ".join(f"{r['stage']} ×{r['c']}" for r in failed) + ".", ""]

    # Findings that point at the same lines are the same problem seen twice.
    where: dict = {}
    for f in confirmed:
        for ev in f["evidence"]:
            for line in range(ev["line_start"], ev["line_end"] + 1):
                where.setdefault((ev["path"], line), set()).add(f["id"])
    related: dict = {}
    for ids in where.values():
        if len(ids) > 1:
            for i in ids:
                related.setdefault(i, set()).update(ids - {i})

    def full(f: dict) -> list[str]:
        out_ = _finding_block(f)
        if related.get(f["id"]):
            out_.insert(-1, "*Touches the same lines as:* " + ", ".join(sorted(related[f["id"]])))
            out_.insert(-1, "")
        return out_

    repeated = [f for f in confirmed if f["severity"] == "low"
                and f["type"] in ("redundant_logic", "duplicate_function", "near_duplicate_files")]
    cross = [f for f in confirmed if f["type"] != "local_risk" and f not in repeated]
    local = [f for f in confirmed if f["type"] == "local_risk"]
    themes = store.meta_get("themes") or []
    if themes:
        by_id = {f["id"]: f for f in confirmed}
        themes = [t for t in themes if sum(i in by_id for i in t["findings"]) >= 2]
        themes.sort(key=lambda t: (min(_SEV.get(by_id[i]["severity"], 3) for i in t["findings"] if i in by_id),
                                   -len(t["findings"])))
        out += ["## Themes", "",
                "Confirmed findings grouped by what lies behind them. Fixing the cause usually "
                "settles the whole group.", ""]
        for t in themes:
            members = [i for i in t["findings"] if i in by_id]
            out += [f"**{t['title']}** ({len(members)} findings: {', '.join(members)})", "", t["summary"], ""]

    out += ["## Confirmed findings: across files", "",
            "Places where two or more files, or the documentation and the code, do not fit together.", ""]
    if not cross:
        out += ["None.", ""]
    for f in cross:
        out += full(f)
    out += ["## Confirmed findings: within one file", ""]
    if not local:
        out += ["None.", ""]
    for f in local:
        out += full(f)
    if repeated:
        out += ["## Confirmed: repeated constants and duplicated logic", "",
                "The same value or rule written in more than one place. Nothing is wrong today; "
                "a change to one copy will not reach the others. Details are in `findings.json`.", ""]
        for f in repeated:
            places = ", ".join(dict.fromkeys(f"`{e['path']}:{e['line_start']}`" for e in f["evidence"][:6]))
            out.append(f"- **{f['id']}** {f['title']} ({places})")
        out.append("")

    waiting = by_status.get("needs_human", [])
    if waiting:
        out += ["## Waiting on a maintainer", "",
                "The code is consistent with these, but whether they are problems depends on a "
                "fact the repository does not contain. Each has a question in `questions.md`.", ""]
        for f in waiting:
            out += _finding_block(f)

    loose = by_status.get("unconfirmed", []) + by_status.get("candidate", [])
    if loose:
        out += ["## Not confirmed", "",
                "Candidates that verification did not confirm with quoted code, or that have "
                "not been through verification. Treat as hints.", ""]
        for f in loose[:100]:
            where = ", ".join(f["files"][:3])
            out.append(f"- {f['id']} [{_TYPE_LABEL.get(f['type'], f['type'])}] {f['title']}"
                       + (f" ({where})" if where else ""))
        if len(loose) > 100:
            out.append(f"- … and {len(loose) - 100} more in `findings.json`")
        out.append("")

    out += ["## How the system fits together", ""]
    purposes = store.claims("stage='system' AND kind='purpose'")
    if purposes:
        out += [c["statement"] for c in purposes[:3]] + [""]
    for label, kind in (("Inputs", "input"), ("Outputs", "output")):
        rows = store.claims("stage='system' AND kind=?", (kind,))
        if rows:
            out += [f"**{label}**", ""] + [f"- {c['statement']}" for c in rows[:25]] + [""]
    for pipe in pipelines(ctx):
        steps = store.meta_get(f"steps:{pipe['name']}") or []
        out += [f"**Path: {pipe['name']}** (starts at {', '.join(pipe['entry_files'])})", ""]
        if pipe["description"]:
            out += [pipe["description"], ""]
        out += [f"{i}. {s['description']}" + (f" — `{'`, `'.join(s['files'][:4])}`" if s["files"] else "")
                for i, s in enumerate(steps, 1)] or ["(not traced)"]
        out.append("")

    out += ["## Static facts", "",
            f"- Entry points: {len(skel['entry_points'])}",
            f"- External inputs: {len(skel['external_inputs'])}; final outputs: {len(skel['final_outputs'])}",
            f"- Modules nothing reaches: {len(skel['unused'])} of {skel['eligible_for_unused']}",
            f"- Duplicated function bodies: {len(skel['dup_functions'])}; "
            f"near-duplicate file pairs: {len(skel['near_dup_files'])}",
            f"- Languages: " + ", ".join(f"{k} {v}" for k, v in stats["by_lang"].items())]
    if stats["skipped"]:
        out.append("- Skipped: " + ", ".join(f"{v} {k}" for k, v in stats["skipped"].items()))
    out += [f"- Caveat: {n}" for n in skel["notes"]]
    out += ["", "## How to read this", "",
            "Findings are candidates raised by one model and then attacked by another that was "
            "asked to refute them against the code. Only those that survived, with a quote that "
            "was mechanically found in the file, are listed as confirmed. The audit is read-only "
            "and gives no fixes or effort estimates. A file the audit did not read in detail "
            "(see Coverage) can only appear in static findings: unused or duplicated code.", ""]
    (ctx.out / "report.md").write_text("\n".join(out), encoding="utf-8")

    (ctx.out / "findings.json").write_text(json.dumps(
        [{k: f[k] for k in ("id", "type", "status", "severity", "title", "detail", "entity",
                            "files", "evidence", "claims", "notes")} for f in findings],
        indent=2), encoding="utf-8")
    with open(ctx.out / "claims.jsonl", "w", encoding="utf-8") as fh:
        for c in store.claims():
            fh.write(json.dumps({k: c[k] for k in (
                "id", "source", "stage", "kind", "subject", "entity", "attribute", "value",
                "statement", "evidence", "evidence_ok", "confidence")}) + "\n")
    write_dashboard(ctx)
