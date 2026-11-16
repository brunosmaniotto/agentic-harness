Task: read one file and record what it does and what it takes for granted.

You receive a file, or one chunk of a large file, with line numbers, and a card of facts about it from static analysis and from the earlier waves. Workers reading other files are doing the same thing right now. Afterwards, the notes about each piece of shared data are laid side by side to find places where two files assume different things. Those disagreements are the main thing this audit is looking for, and they can only be found if the assumptions were written down.

So the most valuable thing you can record is what this code relies on without checking. Look for:
- units and currencies (is this amount in cents or dollars; seconds or milliseconds)
- timezones and where a day or month begins and ends
- what one row represents, and whether a key is assumed unique
- how missing values, empty inputs and duplicates are treated
- which rows count: filters that define "active", "paid", "valid", and the like
- hard-coded thresholds, dates, IDs, rates and limits
- columns or fields expected to exist, and their types and formats
- rounding, ordering, and how identifiers are built or cleaned
- defaults that apply when something is absent
- a value typed in here that is also defined somewhere central (a constant repeated as a literal)
- the exact column names, index name and file layout this code writes, or expects to read
- in numerical and statistical code: the sample period, the seed, the number of draws, transformations (logs, differences, scaling by 100, annualizing), and which estimator or standard error is used

A claim with no entity key is invisible to the comparison step. So whenever an assumption or behavior concerns something another file could also touch (a data file, a column, a shared parameter, the return value of a function other modules call), give it an entity key. Leave the entity empty only for facts that are purely internal to this file.

Produce:

"claims":
  {"kind": "purpose" | "input" | "output" | "requires" | "ensures" | "assumption" | "behavior",
   "entity": entity key or null, "attribute": attribute or null, "value": short normalized value or null,
   "statement": "one self-contained sentence",
   "evidence": [{"line_start": 14, "line_end": 14, "quote": "exact code"}],
   "confidence": "high" | "medium" | "low"}
  - purpose: one claim saying what this chunk is for.
  - input / output: what it reads and what it writes or hands to other modules.
  - requires: something that must already be true of data this code receives (a file it reads, a table, an argument, a value from config) for the code to be correct, and that this code does not itself establish. "Assumes the series is already winsorized." "Expects one row per quarter, sorted." "Treats price as cents." This is the demand side.
  - ensures: something this code makes true of data it produces or passes on (a file it writes, a value it returns to other modules). "Winsorizes at the 1st and 99th percentile before saving." "Drops duplicate order ids." "Writes dates as quarter-end." This is the supply side.
  - assumption: something taken for granted that is not about data handed between files (the environment, a library's behaviour).
  - behavior: a rule or transformation this code applies that is neither of the above but that another file might apply differently.

  Requires and ensures are the most important claims you write. A later step matches them: for every requirement it looks for a producer that ensures it, and reports requirements nobody meets and producers that supply something different. That matching is only as good as these claims. So:
  - Attach each one to the entity key of the data it is about, the same key for the reader and the writer.
  - Be exact about the terms: which percentile, which unit, which key, which date convention. "Cleans the data" cannot be matched to anything.
  - If this code both needs a property and establishes it itself (it deduplicates what it reads before using it), that is an ensures on what it produces, not a requires.
  - A check that raises an error when a property fails is a requires that is at least enforced; say so in the statement.
  Write statements that stand alone: "billing/invoice.py divides table:orders#price by 100, treating it as cents", not "it divides the price". Attach an entity key whenever the claim concerns shared data, and an attribute and value whenever it is an assumption about that data. A comment that states an assumption counts as evidence of the assumption.

"concerns": things in this chunk that look wrong on their own, without needing another file to show it: a branch that can never run, an error swallowed silently, a hard-coded date that has passed, a variable that may be used before it is set, a condition that is always true. Not style, not missing tests, not naming.
  {"title": "...", "detail": "...", "severity": "high" | "medium" | "low", "evidence": [{"line_start": 0, "line_end": 0, "quote": "..."}]}

"questions": at most two, and usually none. A question goes to a busy person, so ask only what no amount of reading the repository could answer and where the answer changes whether the results are right: what an external data source guarantees, which of two behaviours is intended, where a number that drives results came from.
  Do not ask about anything that is defined elsewhere in the repository. You are seeing one file, or one chunk of it; a name that is not defined in your listing is defined in another chunk or another file, and other workers are reading those. Do not ask whether something matches another file: record what this file does, with an entity key, and the comparison step will check.
  {"text": "the question, naming the file and the thing", "why": "what depends on the answer", "entity": entity key or null, "evidence": [{"line_start": 0, "line_end": 0, "quote": "..."}]}

Reply with {"claims": [...], "concerns": [...], "questions": [...]}. A short configuration file may yield only a few claims; a file with nothing suspicious has no concerns. Do not pad.
