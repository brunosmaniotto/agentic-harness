"""dashboard.html: confirmed findings folded into unique issues and ranked by urgency.

Different stages confirm the same bug from different angles and in different words:
the file wave flags a mismatched array as a local risk, the rule screen flags the
same lines as a broken rule. The report lists both; the dashboard folds them into
one row, ranks rows by how urgent they are, and keeps every original finding id.
"""
from __future__ import annotations

import html
import json
import re

from .core import Ctx

TYPE_LABEL = {
    "conflicting_assumption": "Conflicting assumptions",
    "divergent_use": "Same data, used differently",
    "redundant_logic": "Same logic in more than one place",
    "doc_drift": "Documentation and code disagree",
    "contract_gap": "Relies on something nothing guarantees",
    "unmet_demand": "Required, but nothing supplies it",
    "mismatched_supply": "Supplied differently from what is required",
    "double_supply": "Done twice along the same path",
    "rule_violation": "Breaks a stated rule",
    "branch_divergence": "Branches from the same data that differ",
    "local_risk": "Suspicious code",
    "unused_file": "Unused file",
    "duplicate_function": "Duplicated function",
    "near_duplicate_files": "Near-duplicate files",
    "lead": "Lead from the documentation system",
    "other": "Other",
}

# Types where being right matters for results, ahead of hygiene and documentation.
CORRECTNESS = {"rule_violation", "local_risk", "branch_divergence", "unmet_demand", "mismatched_supply",
               "double_supply", "conflicting_assumption", "divergent_use", "contract_gap", "lead", "other"}
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
# Stages whose findings are about one field of one shared thing. Branch and rule findings
# carry a broad entity (the forked table), which would join unrelated issues.
TYING_STAGES = {"reconcile", "contracts", "supply"}
TITLE_OVERLAP = 0.22
_STOP = set("the a an in of to and but not is are be by from with while at for on as than its it this that "
            "other others both one two all only when into".split())


def _words(f: dict) -> set[str]:
    # File names are shared by every finding about the same files, so they say nothing
    # about whether two findings are the same problem.
    stems = {re.sub(r"\.\w+$", "", p.rsplit("/", 1)[-1]).lower() for p in f["files"]}
    words = set(re.findall(r"[a-z0-9_]+", f["title"].lower().replace(".py", "")))
    return {w for w in words if len(w) > 2 and w not in _STOP} - stems


def _same_issue(a: dict, b: dict) -> bool:
    shared = set(a["files"]) & set(b["files"])
    if not shared:
        return False
    if (a["entity"] and a["entity"] == b["entity"] and "#" in a["entity"]
            and {a["stage"], b["stage"]} <= TYING_STAGES):
        return True  # reconcile or contracts already tied both to one field of one shared thing
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / max(1, len(wa | wb)) >= TITLE_OVERLAP


def _urgency(f: dict) -> tuple:
    return SEVERITY_RANK.get(f["severity"], 3), f["type"] not in CORRECTNESS, f["n"]


def cluster(findings: list[dict]) -> list[list[dict]]:
    """Fold findings that describe one issue; each group ordered most urgent first.

    A finding joins a group only if it matches most of the group's members. Plain
    transitive merging (union-find) chains A~B and B~C into one row even when A and C
    have nothing in common, which on a real run put three unrelated data-pipeline bugs
    in one row because each shared a file and a few words with the next."""
    groups: list[list[dict]] = []
    for f in sorted(findings, key=_urgency):
        best, score = None, 0.0
        for g in groups:
            share = sum(_same_issue(f, h) for h in g) / len(g)
            if share > 0.5 and share > score:
                best, score = g, share
        if best is None:
            groups.append([f])
        else:
            best.append(f)
    out = [sorted(g, key=_urgency) for g in groups]
    # rank: severity, then correctness over hygiene, then how many independent stages found it
    out.sort(key=lambda g: (SEVERITY_RANK.get(g[0]["severity"], 3), g[0]["type"] not in CORRECTNESS,
                            -len({f["stage"] for f in g}), g[0]["n"]))
    return out


def _row(group: list[dict]) -> dict:
    lead = group[0]
    evidence, seen = [], set()
    for f in group:
        for e in f["evidence"]:
            key = (e["path"], e["line_start"])
            if key not in seen and len(evidence) < 10:
                seen.add(key)
                evidence.append({"where": f"{e['path']}:{e['line_start']}", "quote": (e.get("quote") or "")[:400]})
    return {
        "severity": lead["severity"], "type": lead["type"], "label": TYPE_LABEL.get(lead["type"], lead["type"]),
        "title": lead["title"], "detail": lead["detail"], "notes": lead["notes"] or "",
        "files": sorted({p for f in group for p in f["files"]}),
        "ids": [f["id"] for f in group], "stages": sorted({f["stage"] for f in group}),
        "others": [{"id": f["id"], "title": f["title"], "label": TYPE_LABEL.get(f["type"], f["type"])}
                   for f in group[1:]],
        "evidence": evidence,
    }


def write_dashboard(ctx: Ctx) -> tuple[str, int, int]:
    """Write dashboard.html. Returns its path, the number of unique issues, and how many
    findings were folded into another."""
    findings = ctx.store.findings("status='confirmed'")
    rows = [_row(g) for g in cluster(findings)]
    folded = len(findings) - len(rows)
    name = ctx.cfg["repo"].get("name") or ctx.cfg["repo"]["path"].rstrip("/").rsplit("/", 1)[-1]
    tidy = [{k: c.get(k) for k in ("tier", "kind", "name", "path", "line", "end", "lines", "reason",
                                   "decided_by", "note", "refs")}
            for c in (ctx.store.meta_get("cleanup") or [])]
    data = json.dumps({"repo": name, "rows": rows, "folded": folded, "cleanup": tidy}).replace("</", "<\\/")
    path = ctx.out / "dashboard.html"
    path.write_text(PAGE.replace("__TITLE__", html.escape(f"Audit: {name}")).replace("__DATA__", data),
                    encoding="utf-8")
    return str(path), len(rows), folded


PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#fafaf8;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e4e3de;--high:#b3261e;--medium:#b26a00;--low:#5b6b7a;--accent:#2f5d8a}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe6;--mute:#a19f98;--line:#33322f;--high:#ff8a80;--medium:#f6b44c;--low:#9fb3c8;--accent:#8cb4e0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1080px;margin:0 auto;padding:28px 20px 60px}h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mute);margin:0 0 20px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:16px}
.chip{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:5px 12px;cursor:pointer;font:inherit}
.chip[aria-pressed=true]{border-color:var(--ink);font-weight:600}
select,input{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
input{flex:1;min-width:200px}
.row{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:8px}
.head{display:grid;grid-template-columns:76px 1fr auto;gap:12px;padding:12px 14px;cursor:pointer;align-items:baseline}
.sev{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.04em}.sev.high{color:var(--high)}.sev.medium{color:var(--medium)}.sev.low{color:var(--low)}
.t{font-weight:600}.meta{color:var(--mute);font-size:13px}.n{color:var(--mute);font-size:13px;white-space:nowrap}
.body{display:none;padding:0 14px 14px 102px}.row.open .body{display:block}
.body p{white-space:pre-wrap;margin:6px 0 10px}.ev{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;border-left:3px solid var(--line);padding:4px 10px;margin:6px 0;white-space:pre-wrap;overflow-x:auto}
.ev b{color:var(--accent);font-weight:600}h4{margin:12px 0 4px;font-size:13px;color:var(--mute);text-transform:uppercase;letter-spacing:.04em}
.empty{color:var(--mute);padding:20px 0}
.tabs{margin:14px 0 18px;border-bottom:1px solid var(--line);gap:0}.tab{border:0;background:none;color:var(--mute);font:inherit;padding:8px 14px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px}
.tab[aria-pressed=true]{color:var(--ink);border-bottom-color:var(--ink);font-weight:600}
.tier{font-size:12px;font-weight:700}.tier.safe{color:var(--accent)}.tier.probable{color:var(--medium)}.tier.check{color:var(--high)}
@media (max-width:640px){.head{grid-template-columns:1fr}.body{padding-left:14px}}
</style></head><body><main>
<h1 id="h"></h1>
<div class="bar tabs"><button class="tab" id="tab-issues" aria-pressed="true">Issues</button><button class="tab" id="tab-clean" aria-pressed="false">Potential clean up</button></div>
<section id="issues"><p class="sub" id="sub"></p>
<div class="bar" id="chips"></div>
<div class="bar"><select id="type"><option value="">All types</option></select><input id="q" type="search" placeholder="Search titles, files, details"></div>
<div id="list"></div></section>
<section id="clean" hidden><p class="sub" id="csub"></p>
<div class="bar" id="cchips"></div>
<div class="bar"><select id="ckind"><option value="">All kinds</option></select><input id="cq" type="search" placeholder="Search names and files"></div>
<div id="clist"></div></section>
</main>
<script>
const D=__DATA__;const sev=new Set(["high","medium","low"]);
const $=id=>document.getElementById(id);
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
$("h").textContent="Audit: "+D.repo;
const counts={high:0,medium:0,low:0};D.rows.forEach(r=>counts[r.severity]=(counts[r.severity]||0)+1);
$("sub").textContent=D.rows.length+" unique issue(s) from confirmed findings ("+counts.high+" high, "+counts.medium+" medium, "+counts.low+" low); "+D.folded+" duplicate finding(s) folded in. Ranked by severity, then correctness over hygiene, then how many independent passes found it.";
["high","medium","low"].forEach(s=>{const b=el("button","chip",s[0].toUpperCase()+s.slice(1)+" ("+counts[s]+")");b.setAttribute("aria-pressed","true");
 b.onclick=()=>{sev.has(s)?sev.delete(s):sev.add(s);b.setAttribute("aria-pressed",sev.has(s));draw()};$("chips").append(b)});
[...new Set(D.rows.map(r=>r.label))].sort().forEach(l=>{const o=el("option",null,l);o.value=l;$("type").append(o)});
$("type").onchange=draw;$("q").oninput=draw;
function draw(){const q=$("q").value.toLowerCase(),t=$("type").value,list=$("list");list.textContent="";let shown=0;
 D.rows.forEach(r=>{if(!sev.has(r.severity)||(t&&r.label!==t))return;
  const hay=(r.title+" "+r.detail+" "+r.files.join(" ")+" "+r.ids.join(" ")).toLowerCase();if(q&&!hay.includes(q))return;shown++;
  const row=el("div","row"),head=el("div","head"),mid=el("div");
  head.append(el("span","sev "+r.severity,r.severity));mid.append(el("div","t",r.title));
  mid.append(el("div","meta",r.label+" · "+r.files.slice(0,3).join(", ")+(r.files.length>3?" +"+(r.files.length-3):"")));
  head.append(mid);head.append(el("span","n",r.ids.length>1?r.ids.length+" findings":r.ids[0]));
  const body=el("div","body");body.append(el("p",null,r.detail));
  if(r.notes)body.append(el("h4",null,"Verifier"),el("p",null,r.notes));
  if(r.evidence.length){body.append(el("h4",null,"Evidence"));r.evidence.forEach(e=>{const d=el("div","ev");d.append(el("b",null,e.where));if(e.quote)d.append(document.createTextNode("\\n"+e.quote));body.append(d)})}
  body.append(el("h4",null,"Findings folded into this row"));
  body.append(el("p",null,[r.ids[0]+" (shown)"].concat(r.others.map(o=>o.id+" ["+o.label+"] "+o.title)).join("\\n")));
  body.append(el("p","meta","Found by: "+r.stages.join(", ")));
  head.onclick=()=>row.classList.toggle("open");row.append(head,body);list.append(row)});
 if(!shown)list.append(el("div","empty","Nothing matches these filters."))}
draw();
const C=D.cleanup.filter(c=>c.kind!=="output"),OUT=D.cleanup.filter(c=>c.kind==="output");
const TL={safe:"Safe to remove",probable:"Probably unused",check:"Check first"},tiers=new Set(["safe","probable","check"]);
$("tab-clean").textContent="Potential clean up ("+C.length+")";$("tab-issues").textContent="Issues ("+D.rows.length+")";
function show(which){$("issues").hidden=which!=="issues";$("clean").hidden=which!=="clean";
 $("tab-issues").setAttribute("aria-pressed",which==="issues");$("tab-clean").setAttribute("aria-pressed",which==="clean")}
$("tab-issues").onclick=()=>show("issues");$("tab-clean").onclick=()=>show("clean");
const lines=t=>C.filter(c=>c.tier===t).reduce((a,c)=>a+c.lines,0);
$("csub").textContent=C.length+" candidate(s), "+C.reduce((a,c)=>a+c.lines,0).toLocaleString()+" lines. Not bugs: code and data that may no longer be needed. "+(OUT.length?OUT.length+" output(s) nothing in the repository reads are listed in cleanup.md.":"");
["safe","probable","check"].forEach(t=>{const n=C.filter(c=>c.tier===t).length;const b=el("button","chip",TL[t]+" ("+n+", "+lines(t).toLocaleString()+" lines)");
 b.setAttribute("aria-pressed","true");b.onclick=()=>{tiers.has(t)?tiers.delete(t):tiers.add(t);b.setAttribute("aria-pressed",tiers.has(t));cdraw()};$("cchips").append(b)});
[...new Set(C.map(c=>c.kind))].sort().forEach(k=>{const o=el("option",null,k.replace("_"," "));o.value=k;$("ckind").append(o)});
$("ckind").onchange=cdraw;$("cq").oninput=cdraw;
function cdraw(){const q=$("cq").value.toLowerCase(),k=$("ckind").value,list=$("clist");list.textContent="";let shown=0;
 const order={safe:0,probable:1,check:2};
 C.slice().sort((a,b)=>order[a.tier]-order[b.tier]||b.lines-a.lines).forEach(c=>{
  if(!tiers.has(c.tier)||(k&&c.kind!==k))return;if(q&&!(c.name+" "+c.path).toLowerCase().includes(q))return;shown++;
  const row=el("div","row"),head=el("div","head"),mid=el("div");
  head.append(el("span","tier "+c.tier,TL[c.tier]));
  mid.append(el("div","t",c.kind.replace("_"," ")+" "+c.name));
  mid.append(el("div","meta",c.path+(c.kind==="file"?"":":"+c.line+"-"+c.end)+" · "+c.reason));
  head.append(mid);head.append(el("span","n",c.lines+" lines"));
  const body=el("div","body");body.append(el("p","meta","Decided by "+c.decided_by+"."));
  if(c.note)body.append(el("p",null,c.note));
  if(c.refs&&c.refs.length){body.append(el("h4",null,"Where the name occurs"));
   body.append(el("p",null,c.refs.map(r=>r.path+":"+r.line+" ["+r.kind+"]").join("\\n")))}
  head.onclick=()=>row.classList.toggle("open");row.append(head,body);list.append(row)});
 if(!shown)list.append(el("div","empty","Nothing matches these filters."))}
cdraw();
</script></body></html>
"""
