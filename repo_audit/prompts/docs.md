Task: record what the documentation says.

You receive part of a document that describes the repository: generated documentation, a README, or design notes. Documentation is the record of what people believe the system does. Later, other workers read the code itself and their notes are compared with yours, so that places where belief and code have drifted apart can be found. For that comparison to work, record only what the document states, not what you think is likely true.

Extract statements that could be checked against code:
- what the system or a component is for
- what it reads and what it produces
- the order in which things happen
- conventions about data: units, currencies, timezones, what one row represents, which values are allowed, how duplicates and missing values are treated
- thresholds, schedules, and rules

Skip installation instructions, contribution guidelines, and anything that could not be contradicted by code.

Reply with:
{"claims": [
  {"about": "system" or the repository path of the file the statement is about,
   "kind": "purpose" | "input" | "output" | "step" | "assumption" | "behavior" | "rule",
   "entity": entity key or null,
   "attribute": attribute or null,
   "value": short normalized value or null,
   "statement": "one self-contained sentence saying what the document claims",
   "evidence": [{"line_start": 12, "line_end": 12, "quote": "exact text from the document"}],
   "confidence": "high" | "medium" | "low"}
]}

Use kind "rule" for a statement of something that must hold everywhere in the code, whichever file does the work: "no forecast uses data from after its origin date", "all amounts are stored in cents", "every model is tuned on the training period only". Rules are rare; most documents have none. Each one will be checked against every file.

Use "about": "system" unless the document clearly ties the statement to one file. Split a sentence that makes two checkable statements into two claims.
