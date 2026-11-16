# Handoff notes

For the Claude instance (or engineer) adapting this inside the company. It was built outside, with no access to the company's code, models, or the existing documentation system. This file says what state it is in and what needs deciding before it is pointed at a real repository.

## What this is for

Bruno already has a system that generates baseline documentation for a repository and keeps it updated on every merge to main. That solves documentation. This tool is the other half: a deliberate, occasional, possibly expensive pass over a repository that has drifted, to find where its parts no longer agree with each other, and to pull out of people's heads the facts the code does not record.

The design goal is a system that does the work, not an assistant that helps someone do it. Nobody should have to steer it through the repository. People come in at one point only: answering the questions it could not answer itself.

`README.md` covers usage and `DESIGN.md` covers the architecture and the invariants. Read both first.

## How far it has been taken

It has been run end to end on three research repositories (10k to 40k lines of Python, with LaTeX papers) and revised after each run. In those runs, quotes from code were found in the file 99% of the time, the verifier refuted roughly a third of cross-file candidates, and the confirmed high-severity findings checked by hand were real.

Not yet tried:

- **An HTTP provider.** No request has been sent to OpenRouter or any other endpoint. The runs used the `queue` provider with Claude subagents answering prompts from disk (`harness/subagents/`). The prompts and pipeline are the same; the transport is not.
- **Models other than Claude Haiku, Sonnet and Opus.** A weaker cheap model may quote less exactly or name entities less consistently.
- **Anything but Python research code**: no services, no SQL warehouse, no large TypeScript, no repository above 40k lines. The prompts were tuned on code where the shared things are parameters and data files. On a codebase where they are tables and service contracts, check that entity keys come out sensibly before trusting the cross-file findings.
- **Supply and demand on real hand-offs.** On the research repositories, two thirds of `requires` claims ended "unclear" or "not visible", and the stage confirmed almost nothing the other stages missed. Those repositories had few hand-offs between people, so this is not yet a fair test. Before trusting an "all met" result, count how many requirements end unclear; above a third, the stage is not seeing enough of the producer's code.

Treat the first run on company code as calibration.

## Decisions to make before the first run

1. **Where the models come from.** Sending company source code to OpenRouter may not be permitted. Check policy before setting a key. The client talks to any OpenAI-compatible endpoint, so an approved internal gateway works by changing `[llm] base_url` and `api_key_env`. If the only approved route is a command-line tool, use `provider = "command"`, which pipes each prompt to a command and reads the reply from its output. If neither fits, `LLM._raw` in `llm.py` is the one place to add a provider.

2. **Which models.** `[models]` needs three ids. `cheap` handles the highest volume (one call per file chunk) and needs to follow a structured-output prompt reliably and quote code exactly. `strong` does the reasoning. `verifier` should be a different model family from `strong` if one is available, so that a finding is not checked by the model that proposed it.

3. **The baseline documentation.** `[baseline] docs_dir` expects a folder of markdown files and `findings_file` expects one markdown file of things worth investigating. That is an assumption about the existing system's output; adjust `_doc_sources` and `run_leads` in `stages.py` if the real format differs. If the generated docs are derived from the code, expect few documentation-versus-code findings from them: they will mostly agree with the code by construction. Hand-written docs and maintainer answers are where intent comes from.

4. **Languages.** Static analysis is good for Python and SQL, import-only for JavaScript and TypeScript, and reference-scanning only for everything else. If the target is mostly another language, write an analyzer for it first (`DESIGN.md`, "Adding a language"); without one the tool still works but loses the import graph, the unused-file findings, and the static data lineage for those files.

5. **Company conventions the static pass should know.** Look at `SPECIAL_NAMES`, `CONVENTION_DIRS`, `TEST_DIRS`, `READ_FUNCS`, `WRITE_FUNCS` in `skeleton.py`. Internal data-access wrappers (a house function for reading a table, say) will not be recognized as reads and writes until they are added. Scheduler definitions (Airflow, cron files, internal job configs) are how entry points get discovered; make sure their file types are recognized so the reference scan reads them.

6. **The rules.** Before the first full run, collect the five to ten requirements that the team treats as non-negotiable and that the code cannot state for itself (units, grain, which tables are off limits, what may not look at what). Enter each with `repo-audit rule`. In testing, this was the difference between missing a known flaw and finding it: without the rule, every file involved looked fine on its own.

## Suggested first run

1. `repo-audit run --static-only` on the target. Free. Read `report.md` and `skeleton.json`. If the unused-file list is long or obviously wrong, entry points are being missed; fix that in `skeleton.py` before spending anything, because the import graph also decides which files the path wave sees.
2. `repo-audit plan` for the size of a full run.
3. Set `[limits] max_files = 30` and a small `[budget]`, then `repo-audit run`.
4. Check these before scaling up:
   - **Discarded claims.** `repo-audit status` shows how many claims had evidence that could not be found. More than a few percent means the cheap model is paraphrasing instead of quoting; tighten `files.md` or change the model.
   - **Entity naming.** Open `claims.jsonl` and look at the `entity` values. If the same table appears under several spellings, reconciliation is missing comparisons. `canonicalize` is meant to catch this; check what it merged (`aliases` table) and whether any merge is wrong.
   - **Refutation rate.** If the verifier refutes nearly everything, reconcile is over-reporting; if it refutes nothing, it is not being critical. Read a sample of each verdict.
   - **Requires and ensures.** Count the `requires` and `ensures` claims in `claims.jsonl`. If files mostly produce `assumption` and few `requires`, the contracts stage has nothing to match; tighten `files.md` with two examples from the target codebase.
   - **Question quality.** `questions.md` should contain things a maintainer can answer in a sentence. Vague questions mean the prompts need tightening.
5. Remove the file cap and run in full. The run can be interrupted and resumed at any point.

## Known weak points

- **Entity resolution is the limiting factor for recall.** Two files that touch the same data are only compared if their claims land on the same entity key. Names built at runtime, data passed in memory between modules, and ORM models are the likely gaps. An analyzer that maps ORM classes to tables would help a lot where ORMs are used.
- **Large entities are truncated.** A table touched by hundreds of files produces more claims than fit in one reconcile call; `limits.max_entity_claims` keeps the highest-priority ones. Splitting per field would be the next improvement.
- **Path tracing sees file cards, not code.** Deep or dynamic call chains will be traced incompletely, capped at `limits.path_files` files per path.
- **Convention-loaded detection is a heuristic.** It turns groups of unreached files into a question, which is the safe direction, but a genuinely dead plugin directory will be asked about, not reported.
- **Verifier context is narrow.** It sees 30 lines around each cited line. On a large repository many first verdicts come back as `needs_code` and cost a second round; raise `limits.verify_context_lines` or add the callers found by static analysis.
- **Single-file concerns scale with repository size** (several hundred on 40k lines). Verifying all of them roughly doubles the cost; by default only medium and above are verified.
- **LaTeX quotes.** Cheap models tend to normalize LaTeX markup when quoting, and those claims are discarded. Stripping markup before showing the text would recover them.
- **Packed reconcile units are rehashed** when any member changes, so a probe round re-runs most of them. Stable pack membership would save calls.
- **Supply excerpts are chosen by name match**, 260 lines per producer. The part that builds the object is often missed; using the call graph to pick the whole producing function is the next improvement.
- **Chunking is by line count.** A function split across two chunks is seen in halves, with overlap.

## What to keep

The invariants listed in `DESIGN.md` are what make the output believable to the people who have to act on it. In particular: quotes located mechanically, two independent sources per finding, and nothing called confirmed until a second model has tried to refute it. Prompts, models, limits, languages, and stage order are all meant to be changed.
