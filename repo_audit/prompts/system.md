Task: the crude map. What goes into this repository and what comes out.

You receive a digest produced by static analysis: the layout, the files meant to be run, the data the code reads without ever writing (external inputs), the data it writes without ever reading (final outputs), configuration it depends on, and what the documentation claims. You have not seen any code beyond the first lines of each entry point. Later workers will trace each path from input to output and then read every file; your map tells them where to look.

Produce:

"claims": what the system is for, each external input, and each final output. One claim each.
  {"kind": "purpose" | "input" | "output", "entity": entity key or null, "statement": "one sentence", "confidence": "high" | "medium" | "low"}
  No evidence is needed here; the static digest is the basis. Where the documentation says one thing and the static facts suggest another, say so in the statement rather than choosing.

"pipelines": the distinct paths through the system that should be traced, each starting from one or more entry files listed in the digest. A repository with one main script has one pipeline; a repository with a nightly job, a backfill script and a reporting job has three.
  {"name": "short_snake_case_name", "description": "what this path turns into what", "entry_files": ["paths exactly as listed under ENTRY POINTS"]}
  Group entry points that are plainly parts of the same job. Leave out entry points that are only tools or tests.

"questions": at most three. Later waves read all of the code, so do not ask about anything the code will show (which files a script reads, where a setting comes from). Ask only what lies outside the repository and changes the map: where an external input comes from and who owns it, who consumes a final output, whether an entry point is still run by anyone.
  {"text": "the question", "why": "what depends on the answer"}

Reply with {"claims": [...], "pipelines": [...], "questions": [...]}.
