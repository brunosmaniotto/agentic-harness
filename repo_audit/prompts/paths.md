Task: trace one path from its entry point to its outputs.

The crude map says this pipeline turns certain inputs into certain outputs. Your job is the next level of detail: the ordered steps in between. You receive the pipeline's entry files and a card for each file reachable from them: what it defines, what it imports, and what data static analysis saw it read and write. You do not see the code itself; workers in the next wave read each file with your steps as context, so each step should tell them what that file is supposed to be doing at that point.

Produce:

"steps": the path in execution order, as far as the cards let you establish it.
  {"description": "what happens in this step, in one sentence", "files": ["paths that carry out this step"], "reads": ["entity keys"], "writes": ["entity keys"]}
  Follow the data: a step that writes something should come before the step that reads it. If the cards do not show the order of two steps, keep them as separate steps and say so in a question rather than inventing an order. Files that are reachable but play no visible part can be left out.

"questions": usually none. The next wave reads every one of these files line by line, so anything the code itself shows will be found without asking. Ask only about what lies outside the repository and changes the picture: a step that must happen somewhere but that no file here performs, or an input that no file here produces.
  {"text": "the question", "why": "what depends on the answer"}

Reply with {"steps": [...], "questions": [...]}.
