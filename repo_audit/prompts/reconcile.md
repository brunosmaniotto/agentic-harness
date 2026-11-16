Task: lay the claims about one subject side by side and find where they do not fit.

You receive every claim on the board about one subject: a piece of data and its fields, a shared parameter, or a single file. When several small subjects are sent together, each has its own SUBJECT block: treat each one separately, never compare claims across blocks, and name the subject in everything you return. The claims were written by workers who each saw only one file or one document, so none of them could notice a disagreement. You can. Each claim shows its source (llm: read from code; static: found by static analysis; docs: stated in documentation; human: told to the audit by a maintainer), where it comes from, and the quoted line it rests on.

Report a finding when claims from different places cannot all be right, or reveal that the same thing is being done in more than one way:

- conflicting_assumption: two places assume incompatible things about the same field. One treats a price as cents and another as dollars; one assumes UTC and another local time; one assumes a key is unique and another handles duplicates of it.
- divergent_use: the same data is used in ways that should probably match and do not. Two reports define "active customers" with different filters; two jobs deduplicate on different keys; one rounds and one truncates.
- redundant_logic: the same rule or transformation is implemented in more than one place.
- doc_drift: documentation says one thing and the code shows another.
- other: a real inconsistency that fits none of the above.

A maintainer's statement (source human) is authoritative about what is intended. Code that contradicts it is a finding; and it can also settle an apparent conflict, in which case there is no finding.

Whether a requirement of one file is met by what another file ensures is checked in a separate step, so do not report "nothing ensures this" here. Report disagreements between statements of the same kind: two consumers that require incompatible things, two producers that do the same job differently, a document against the code.

Before reporting, rule out the innocent explanations. Two claims are not in conflict if they are the same fact in different words, if they concern different fields, or if one describes the data before a conversion that another claim records and the other describes it after. Each finding must cite at least two claims from different origins. Do not report something that only one claim supports.

When you cannot tell whether two claims conflict because a fact is missing:
- if the answer should be in a particular file, ask for a second look at it ("probes"). A worker will read the relevant part of that file with your question. Second looks are costly: ask for at most one per subject, and only when the answer would decide a finding of medium or high severity.
- if only a person could know, ask a question.

Reply with:
{"findings": [
  {"subject": "the SUBJECT line this finding belongs to",
   "type": "conflicting_assumption" | "divergent_use" | "redundant_logic" | "doc_drift" | "other",
   "title": "one line a maintainer would understand without context",
   "detail": "what each place assumes or does, naming the files, and what goes wrong if both stay as they are",
   "claim_ids": ["C-00012", "C-00040"],
   "severity": "high" | "medium" | "low",
   "confidence": "high" | "medium" | "low"}
 ],
 "probes": [{"subject": "...", "file": "repository path", "question": "what to look for in that file, naming the function or variable"}],
 "questions": [{"subject": "...", "text": "...", "why": "..."}]}

A constant typed in several places is one finding per constant, not one per place. Report it as redundant_logic with low severity unless the copies already disagree, in which case it is a conflicting_assumption.

Severity: high if the result is probably wrong numbers or lost data that nobody would notice; medium if it would be under plausible conditions, or is a trap for the next person to change this code; low for duplication and tidiness. Most subjects have no findings. An empty list is the expected answer when the claims are consistent.
