Task: decide from the producer's code whether it meets what its consumers require.

Files that use a shared piece of data, a function or a class wrote down what they require of it. The summaries of the producing file did not say enough to tell whether each requirement holds, so you are given the producer's code itself.

You receive one SUBJECT, a numbered list of REQUIREMENTS (each with the consumer file and the line it rests on), and one or two PRODUCER listings with line numbers. A listing may be an excerpt.

For each requirement decide:

- "met": the code shown makes it true. Nothing more is needed.
- "mismatched": the code addresses it on different terms (another key name, unit, threshold, shape, order, default). Quote the line that does so.
- "unmet": the code that would have to establish it is in the listing and does not: the dict is built there and the key is missing, the function is defined there and lacks the argument, the transformation the consumer relies on is not applied on the path that produces the data. Quote the line where it would have to be, if there is one.
- "not_visible": the part of the producer that would settle it is not in the listing. Use this freely; a guess costs a reviewer time.

A requirement the consumer states too strongly is not a finding. If the consumer would work correctly with what the producer does, answer "met".

Reply with:
{"checks": [
  {"requirement": "R1",
   "status": "met" | "mismatched" | "unmet" | "not_visible",
   "evidence": [{"path": "the producer file", "line_start": 10, "line_end": 12, "quote": "copied exactly from the listing"}],
   "title": "for mismatched or unmet: one line a maintainer would understand, naming both files",
   "explanation": "what the consumer needs, what the producer does, and what goes wrong",
   "severity": "high" | "medium" | "low",
   "confidence": "high" | "medium" | "low"}
]}

Severity: high if wrong numbers would result and nobody would notice; medium under plausible conditions; low if it fails loudly or is cosmetic.
