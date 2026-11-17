"""Command line: repo-audit init | plan | run | status | answers | answer | note | rule | report"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, human, pipeline, report, stages


def _cfg(args) -> dict:
    path = args.config
    if not path and Path("repo-audit.toml").exists():
        path = "repo-audit.toml"
    overrides: dict = {}
    if getattr(args, "repo", None):
        overrides.setdefault("repo", {})["path"] = str(Path(args.repo).resolve())
    if getattr(args, "out", None):
        overrides.setdefault("output", {})["dir"] = str(Path(args.out).resolve())
    return config.load(path, overrides)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repo-audit", description=__doc__)
    parser.add_argument("--config", help="path to repo-audit.toml (default: ./repo-audit.toml)")
    parser.add_argument("--repo", help="repository to audit (overrides the config)")
    parser.add_argument("--out", help="output directory (overrides the config)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="write a starter repo-audit.toml here")
    sub.add_parser("plan", help="static analysis only; estimate how many model calls a run needs")
    run = sub.add_parser("run", help="run or resume the audit")
    run.add_argument("--static-only", action="store_true", help="no model calls at all")
    run.add_argument("--only", help="comma-separated stages: " + ",".join(pipeline.MODEL_STAGES))
    sub.add_parser("status", help="what is on the board")
    sub.add_parser("report", help="rewrite report.md and questions.md from the board")
    answers = sub.add_parser("answers", help="import answers typed into questions.md")
    answers.add_argument("file")
    one = sub.add_parser("answer", help="answer one question")
    one.add_argument("id")
    one.add_argument("text")
    note = sub.add_parser("note", help="tell the audit something the repository does not say")
    note.add_argument("text")
    note.add_argument("--entity", help="e.g. table:orders#price")
    note.add_argument("--file", help="repository path the note is about")

    rule = sub.add_parser("rule", help="state something that must hold everywhere; every file is checked against it")
    rule.add_argument("text")

    args = parser.parse_args(argv)

    if args.cmd == "init":
        target = Path("repo-audit.toml")
        if target.exists():
            print("repo-audit.toml already exists", file=sys.stderr)
            return 1
        target.write_text(config.EXAMPLE, encoding="utf-8")
        print("Wrote repo-audit.toml. Set [repo].path and [models], then: repo-audit plan")
        return 0

    cfg = _cfg(args)
    if args.cmd == "plan":
        pipeline.plan(cfg)
    elif args.cmd == "run":
        only = set(args.only.split(",")) if args.only else None
        if only and only - set(pipeline.MODEL_STAGES):
            parser.error(f"unknown stage(s): {', '.join(sorted(only - set(pipeline.MODEL_STAGES)))}")
        pipeline.run(cfg, only=only, static_only=args.static_only)
    elif args.cmd == "report":
        ctx = pipeline.build_context(cfg)
        report.write_report(ctx)
        print(f"Report: {ctx.out / 'report.md'}")
        print(f"Dashboard: {ctx.out / 'dashboard.html'}")
    elif args.cmd == "status":
        store = pipeline.open_store(cfg)
        print("Claims by source:")
        for row in store.rows("SELECT source, COUNT(*) c, SUM(evidence_ok=0) bad FROM claims GROUP BY source"):
            print(f"  {row['source']:<8} {row['c']:>7}   ({row['bad'] or 0} with unverifiable evidence)")
        print("Findings by status:")
        for row in store.rows("SELECT status, COUNT(*) c FROM findings GROUP BY status"):
            print(f"  {row['status']:<12} {row['c']:>5}")
        print(f"Open questions: {len(stages.open_questions(store))}")
        print("Model usage by stage:")
        for row in store.usage_by_stage():
            print(f"  {row['stage']:<13} {row['model']:<40} {row['calls']:>5} calls "
                  f"({row['cached']} cached)  {row['prompt_tokens'] + row['completion_tokens']:>10,} tokens")
        failed = store.rows("SELECT stage, unit, error FROM units WHERE status='failed' LIMIT 20")
        if failed:
            print("Failed units (retried on next run):")
            for row in failed:
                print(f"  {row['stage']} {row['unit']}: {row['error']}")
    elif args.cmd == "answers":
        count = human.import_answers(pipeline.open_store(cfg), args.file)
        print(f"Recorded {count} answer(s). Run `repo-audit run` to apply them.")
    elif args.cmd == "answer":
        ok = human.answer(pipeline.open_store(cfg), args.id, args.text)
        print("Recorded." if ok else f"No such question: {args.id}")
        return 0 if ok else 1
    elif args.cmd == "rule":
        human.rule(pipeline.open_store(cfg), args.text)
        print("Recorded. Run `repo-audit run` to check the repository against it.")
    elif args.cmd == "note":
        human.note(pipeline.open_store(cfg), args.text, args.entity, args.file)
        print("Recorded. Run `repo-audit run` to apply it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
