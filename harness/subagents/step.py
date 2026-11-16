"""Run the audit with Claude Code subagents standing in for the models.

usage: python harness/subagents/step.py <config.toml> [extra `repo-audit run` args, e.g. --only verify]

Runs the pipeline once with the queue provider. When it pauses, groups the pending
prompts into batch files by tier and prints one line per batch:

    BATCH <tier> <batch file> prompts=N bytes=B

Hand each batch to one subagent (cheap -> a small model, strong -> a mid model,
verifier -> the strongest, ideally another family) with the prompt:

    Read harness/subagents/AGENT.md and follow it exactly. Your batch file is <batch file>

Then run this script again. Repeat until it prints pending=0.
"""
import glob
import os
import subprocess
import sys
try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
cfg_path = os.path.abspath(sys.argv[1])
with open(cfg_path, "rb") as f:
    out = tomllib.load(f)["output"]["dir"]

r = subprocess.run([sys.executable, "-W", "ignore", "-m", "repo_audit", "--config", cfg_path, "run"]
                   + sys.argv[2:], cwd=ROOT, capture_output=True, text=True)
print(r.stdout[-2500:], r.stderr[-1500:])

for old in glob.glob(f"{out}/queue/batch-*.txt"):
    os.remove(old)
pending = sorted(glob.glob(f"{out}/queue/pending/*.md"))
by_tier: dict[str, list[str]] = {}
for p in pending:
    head = open(p, encoding="utf-8").read(300).splitlines()
    tier = next(l for l in head if l.startswith("TIER:")).split()[1]
    by_tier.setdefault(tier, []).append(p)

n = 0
for tier, paths in by_tier.items():
    # small models get fewer, shorter prompts per batch
    cap_n, cap_b = (6, 90_000) if tier == "cheap" else (9, 125_000)
    batch, size = [], 0
    for p in paths + [None]:
        b = os.path.getsize(p) if p else 0
        if batch and (p is None or len(batch) >= cap_n or size + b > cap_b):
            n += 1
            f = f"{out}/queue/batch-{n:02d}.txt"
            open(f, "w").write("\n".join(batch) + "\n")
            print(f"BATCH {tier} {f} prompts={len(batch)} bytes={size}")
            batch, size = [], 0
        if p:
            batch.append(p)
            size += b
print(f"pending={len(pending)}")
