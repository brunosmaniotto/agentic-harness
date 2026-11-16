"""Runs the stages in order. Safe to re-run: finished work is skipped."""
from __future__ import annotations

import json
from pathlib import Path

from . import evidence, report, skeleton, stages
from .core import Ctx, PendingWork
from .llm import LLM, BudgetExceeded, LLMError
from .store import Store

MODEL_STAGES = ["docs", "leads", "system", "paths", "files", "reconcile", "rules", "verify",
                "themes", "questions"]


def open_store(cfg: dict) -> Store:
    out = Path(cfg["output"]["dir"])
    out.mkdir(parents=True, exist_ok=True)
    return Store(str(out / "store.db"))


def build_context(cfg: dict, responder=None, log=print) -> Ctx:
    store = open_store(cfg)
    log("Static analysis ...")
    skel = skeleton.build(cfg, log)
    out = Path(cfg["output"]["dir"])
    (out / "skeleton.json").write_text(json.dumps(skel, indent=1), encoding="utf-8")
    stats = skel["stats"]
    log(f"  {stats['analyzed']} files, {stats['lines']} lines, {len(skel['entry_points'])} entry point(s), "
        f"{len(skel['entities'])} data entities, {len(skel['unused'])} unreached module(s)")
    return Ctx(cfg, store, LLM(cfg, store, responder), skel, log)


def run(cfg: dict, only: set[str] | None = None, static_only: bool = False,
        responder=None, log=print) -> Ctx:
    ctx = build_context(cfg, responder, log)
    stages.static_ingest(ctx)
    stopped = ""

    def want(name: str) -> bool:
        return not static_only and (only is None or name in only)

    def step(name: str, fn) -> int:
        stats = fn(ctx)
        log(f"{name}: {stats['ran']} run, {stats['skipped']} unchanged"
            + (f", {stats['failed']} failed" if stats["failed"] else ""))
        return stats["ran"]

    try:
        if not static_only:
            ctx.llm.preflight()
        if want("docs"):
            step("docs", stages.run_docs)
        if want("leads"):
            step("leads", stages.run_leads)
        if want("system"):
            step("system", stages.run_system)
        if want("paths"):
            step("paths", stages.run_paths)
        if want("files"):
            if step("files", stages.run_files):
                ctx.store.meta_set("probe_rounds_done", 0)  # new reading earns new probes
        if want("reconcile"):
            # The round count lives in the store, so a run that pauses and resumes
            # (queue provider, budget stop) cannot loop past the limit.
            rounds = ctx.cfg["limits"]["probe_rounds"]
            while True:
                step("canonicalize", stages.run_canonicalize)
                step("reconcile", stages.run_reconcile)
                step("contracts", stages.run_contracts)
                done = ctx.store.meta_get("probe_rounds_done", 0)
                if not stages.pending_probes(ctx):
                    break
                if done >= rounds:
                    ctx.store.run("UPDATE probes SET status='skipped' WHERE status='pending'")
                    step("contracts", stages.run_contracts)  # no more answers coming: settle
                    break
                step(f"probe (round {done + 1})", stages.run_probes)
                ctx.store.meta_set("probe_rounds_done", done + 1)
            step("supply", stages.run_supply)
        if want("rules"):
            step("rules", stages.run_rules)
        if want("verify"):
            step("verify", stages.run_verify)
        if want("themes"):
            step("themes", stages.run_themes)
        if want("questions"):
            step("questions", stages.run_questions)
    except PendingWork as exc:
        stopped = f"{exc}; answer them in {ctx.out / 'queue' / 'pending'} and run again"
        log(f"Paused: {exc}")
    except BudgetExceeded as exc:
        stopped = f"budget reached ({exc})"
        log(f"Stopped: {stopped}. Raise [budget] and run again to continue.")
    except LLMError as exc:
        stopped = f"model access failed ({exc})"
        log(f"Stopped: {stopped}")

    report.write_report(ctx, stopped)
    usage = ctx.store.usage()
    log(f"Report: {ctx.out / 'report.md'}")
    log(f"Questions: {ctx.out / 'questions.md'} ({len(stages.open_questions(ctx))} open in total)")
    log(f"Model usage so far: {usage['calls']} calls, {usage['tokens']:,} tokens"
        + (f", ${usage['usd']:.2f}" if usage["usd"] else ""))
    return ctx


def plan(cfg: dict, log=print) -> dict:
    """Estimate the size of a run without calling any model."""
    ctx = build_context(cfg, log=lambda *_: None)
    limits, files = cfg["limits"], ctx.skel["files"]
    chosen = stages.select_files(ctx)
    file_units = file_tokens = 0
    for rel in chosen:
        ranges = evidence.chunk_ranges(files[rel]["lines"], limits["chunk_lines"], limits["chunk_overlap"])
        file_units += len(ranges)
        file_tokens += files[rel]["bytes"] // 4 + 1500 * len(ranges)
    docs = stages._doc_sources(ctx)
    doc_units = sum(len(evidence.chunk_ranges(len(ctx.lines(d)), limits["chunk_lines"],
                                              limits["chunk_overlap"])) for d in docs)
    data_entities = sum(1 for k in ctx.skel["entities"] if not k.startswith("env:"))
    static_findings = (min(len(ctx.skel["unused"]), limits["max_static_findings"])
                       + min(len(ctx.skel["dup_functions"]), limits["max_static_findings"])
                       + min(len(ctx.skel["near_dup_files"]), limits["max_static_findings"]))
    n_pipes = max(1, min(len(ctx.skel["entry_points"]), limits["max_pipelines"]))
    est = {
        "files_selected": len(chosen), "files_analyzed": ctx.skel["stats"]["analyzed"],
        "stages": {
            "docs": {"calls": doc_units, "tier": cfg["stages"]["docs"]},
            "system": {"calls": 1, "tier": cfg["stages"]["system"]},
            "paths": {"calls": n_pipes, "tier": cfg["stages"]["paths"]},
            "files": {"calls": file_units, "input_tokens": file_tokens, "tier": cfg["stages"]["files"]},
            "reconcile (lower bound)": {"calls": data_entities, "tier": cfg["stages"]["reconcile"]},
            "verify (lower bound)": {"calls": static_findings if cfg["verify"]["static"] else 0,
                                     "tier": cfg["stages"]["verify"]},
        },
    }
    log(f"Repository: {ctx.skel['stats']['analyzed']} files analyzed, {ctx.skel['stats']['lines']} lines")
    log(f"File wave would read {len(chosen)} file(s) in {file_units} chunk(s), "
        f"about {file_tokens:,} input tokens")
    for name, row in est["stages"].items():
        log(f"  {name:<26} {row['calls']:>6} call(s)   tier: {row['tier']}")
    log("Reconcile and verify grow with what the file wave finds; the numbers above are floors.")
    return est
