# Design

## The idea

A person asked to audit a large repository does not read it front to back. They get a rough idea of what it is for, follow the main paths, read the files along them, and keep notes. Problems show up when two notes disagree. This tool does the same thing, with the notes kept in a database so that many models can contribute and none has to hold the whole repository.

Two consequences shape everything else:

- **The notes are the product.** Stages communicate only through the store. A stage can be re-run, replaced, or given to a different model without the others knowing.
- **Disagreements are found by comparing notes, not files.** Comparing every pair of files does not scale. Instead each file's assumptions are recorded against the data they concern (`table:orders#price`, unit, cents), and comparison happens per data entity.

## The board (`store.py`)

One SQLite file. The tables that matter:

**claims**: one statement about the repository.

| Column | Meaning |
|---|---|
| `source` | `static` (computed), `llm` (read from code), `docs` (stated in documentation), `human` (told by a maintainer) |
| `kind` | `purpose`, `input`, `output`, `step`, `assumption`, `behavior`, `answer` |
| `subject` | `system`, `path:<name>`, `file:<path>`, or `block:<id>` (a branch of the data flow) |
| `entity` | `kind:name#field`, e.g. `table:orders#price` or `param:sample_start`; null if not about anything shared |
| `attribute`, `value` | for assumptions: `unit` / `cents`, `timezone` / `UTC` |
| `statement` | one self-contained sentence |
| `evidence` | list of `{path, line_start, line_end, quote}` |
| `evidence_ok` | 1 if a quote was found in the file, 0 if none was, null where none is required |

**findings**: `type`, `title`, `detail`, `severity`, cited `claims`, `evidence`, and `status`: `candidate` → `confirmed` | `refuted` | `needs_human` | `unconfirmed`.

**questions**: `text`, `why`, the `entity` or `subject` it concerns, the `finding` it decides if any, `priority`, `status`: `open` | `answered` | `merged` | `dropped`.

**units**: one row per unit of work per stage, with a hash of its input. This is what makes runs resumable and incremental.

Also `probes` (requests for a second look at a file), `aliases` (entity names judged to mean the same thing), `cache` (model replies by prompt), `calls` (usage log).

## Stages

Each stage builds a list of units `(id, input_hash, payload)` and hands it to `Ctx.run_units`. A unit runs only if its hash differs from the one stored for it. Model calls happen in worker threads; all writes happen on the main thread.

| Stage | Unit | Hash covers | Writes |
|---|---|---|---|
| static | (whole repo, no model) | — | `static` claims for every read and write; candidate findings for unused and duplicated code; questions about files loaded by convention |
| docs | doc chunk | chunk text, prompt, model | `docs` claims |
| leads | chunk of the findings file | same | `lead` findings |
| system | one | static digest, prompt, model | system claims, proposed pipelines, questions |
| paths | pipeline | file cards along the path | ordered steps, questions |
| files | file chunk | file content, prompt, model | `llm` claims, `local_risk` findings, questions |
| canonicalize | batch of entity keys | the keys | aliases |
| reconcile | a subject (entity, shared parameter, or a file that docs or people made statements about); subjects with few claims share one unit | the claims shown | findings, probes, questions |
| contracts | a subject with at least one `requires` claim | the demand and supply claims shown | `unmet_demand`, `mismatched_supply`, `double_supply` findings; supplier probes; questions |
| probe | probe | probe text, file content | `llm` claims, questions |
| supply | a subject with requirements still unclear after the loop | the requirements and the producer excerpt | `unmet_demand`, `mismatched_supply` findings |
| blocks | one branch of a fork in the data lineage | the branch's files, their claims, and code chosen from the call graph | ordered steps (in `meta`, and as `step` claims under `block:<id>` with no entity, so reconcile and contracts ignore them) |
| branches | one fork | the step lists of its branches and the rules | expected-match verdicts per group; `branch_divergence` findings; questions |
| rules | a file's claims (packed) against the rule list | the claims and the rules | `rule_violation` findings |
| verify | candidate finding | finding, file contents, relevant maintainer statements, any extra code it asked for | verdict, corrected evidence, a question if it needs a person; `needs_code` earns one extra pass with the requested code |
| second opinion | a confirmed or refuted finding at or above `second_opinion_min_severity`, when `verifier_2` is set | same input as verify, plus the first verdict | agreement noted; disagreement sets `needs_human` and asks a question |
| themes | one | the confirmed findings | groups stored in `meta`, shown first in the report |
| questions | batch of open questions | their text | merges, drops, priority |

The file-wave hash deliberately leaves out the context card (pipeline role, importers). Otherwise an answered question that changes the map would force every file to be read again.

Reconcile and probe alternate up to `limits.probe_rounds` times: reconcile asks for a second look at a file, the probe adds claims, and the subjects with new claims are reconciled again. The loop ends when no probes are pending. The round count is kept in the store so it holds across paused runs, and probes are capped per round.

**Supply and demand.** The file wave records three claim kinds beyond what a file does: `requires` (a precondition on an input), `ensures` (a guarantee about an output), and `assumption`. In contracts, the demand side is every `requires` claim on a subject and the supply side is every `ensures`, `behavior`, `output`, human answer and rule on it. A demand with no visible supplier does not become a finding straight away: the stage first probes the files that write the subject (`limits.supplier_probes`), and reports only if the demand is still unmet afterwards. If no file in the repository writes the subject, the supplier is outside the repository and the stage asks a question. Reconcile compares statements of the same kind; contracts compares across kinds. They share the loop and the probe budget.

**Rules.** A rule is a claim of kind `rule` from a person (`repo-audit rule`) or from a document (capped by `limits.max_doc_rules`), with subject `system`. It has no file, so it is checked against every file's claims. Rules also enter the verifier's prompt as maintainer statements.

A subject is reconciled only if at least two different places said something comparable about it: an assumption, a behavior, or a statement in a document or from a person. A list of readers and writers alone gives nothing to disagree about.

## Invariants

These are what the output's reliability rests on. Changes should keep them.

1. **Quotes are located mechanically** (`evidence.locate_quote`), ignoring whitespace. A claim with no located quote gets `evidence_ok = 0` and is excluded from reconciliation.
2. **A reconcile finding must cite at least two claims from different origins.** Claim ids that were not in the prompt are dropped.
3. **`confirmed` requires the verifier to quote code that is located in the file.** A "confirmed" verdict without it becomes `unconfirmed`. The exception is `unused_file`, where the evidence is an absence.
4. **Maintainer statements are authoritative about intent** and are passed to reconcile and verify wherever they are relevant.
5. **The target repository is never written to.** Author names from git history are never stored or sent; only counts and flags.
6. **Obvious credentials are masked before any text is sent** (`evidence.redact`), preserving line numbers.
7. **Re-running with nothing changed makes no model calls.** Finding ids survive re-runs as long as the finding is produced again.

## Static analysis (`skeleton.py`)

Per file: language, role (code, test, config, doc), imports resolved to repository files, references to other files by path or module name, top-level definitions, and data touched (tables from SQL strings and dataframe/Spark calls, files, environment variables, HTTP calls, command-line arguments).

Across files:

- **Unreached modules.** Everything that is not a plain importable module (entry points, tests, scripts, CI and config files, templates) is a root. Modules not reachable from any root through imports or references are candidates. Docs are not roots: a README mentioning a script does not prove it runs.
- **Convention-loaded files.** When five or more unreached files share a name, or make up at least half of a directory, they are almost certainly discovered by a framework. They become one question to maintainers, not many findings.
- **Data lineage.** Entities read but never written are external inputs; written but never read are final outputs. Test files are left out.
- **Duplicates.** Functions with identical bodies (by syntax tree), and file pairs sharing most of their lines.
- **Call graph (Python).** Every function and class at any depth, and every call to a function defined in the repository, with the calling function. A call is resolved to a definition in the same file first, then in an imported file, then to the only definition of that name; ambiguous names are left unresolved rather than guessed. `context.py` uses this to choose code for workers.

**Forks and blocks** (`blocks.py`). Readers and writers of each table, file, topic and API come from static analysis plus the `input` and `output` claims of the file wave. A fork is a set of two or more code files reading the same data (pieces of data with the same readers are one fork; figures and documents are not inputs). A branch is one reader plus the files downstream of it that no other reader also feeds; files fed by several branches are joins, shown but not compared. Differences found from overlapping forks are merged when they name the same files and are worded alike once file names are set aside; a difference whose title names only files outside its branches is dropped as an answer to another input.

### Adding a language

Write `analyze_<lang>(rel, text, rec, env)` that fills `rec["imports"]` (repository paths), `rec["io"]` (via `_add_io`), `rec["defs"]`, and `rec["entry"]`; register it in `ANALYZERS`; add the language to `GRAPH_LANGS` if its imports are resolved well enough to trust "nothing imports this". Languages without an analyzer are still read by the file wave and still take part in reconciliation; they only miss static facts.

## Layout

```
repo_audit/
  config.py     defaults and TOML loading
  store.py      the board
  evidence.py   file loading, redaction, quote location, chunking
  names.py      entity key normalization
  llm.py        model client: OpenAI-compatible HTTP, command, or mock; cache; budget
  skeleton.py   static analysis
  core.py       stage context, resumable units, claim and question intake
  context.py    which code a worker sees: call graph, whole functions, callers and callees
  stages.py     every stage but blocks and branches
  blocks.py     forks in the data lineage, the block wave, branch comparison
  human.py      answers and notes
  report.py     report.md, questions.md, findings.json, claims.jsonl
  pipeline.py   stage order, plan
  cli.py        commands
  prompts/      one text file per stage, plus _common.md prepended to each
```
