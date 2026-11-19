Task: for each candidate, decide from the places its name occurs whether it is still used.

Static analysis found code, files and constants that nothing appears to use. For each CANDIDATE you see what it is, where it is defined, why static analysis listed it, and every place its name occurs elsewhere in the repository, each tagged by where it was found:

- [code]: Python code outside tests that names it (these were already ruled out, so you will rarely see one)
- [test]: test code
- [string]: inside a string in Python code (getattr, a registry, a dispatch table, a log message)
- [all]: listed in `__all__`
- [config], [other]: a configuration file, shell script, SQL or other non-Python file
- [notebook]: a Jupyter notebook
- [doc]: documentation

Decide, for each candidate:

- "used": something reaches it. A string that the code turns into a call (getattr(module, "name"), a dict mapping names to functions, a config value naming a function or a module to run), a framework decorator that registers it (a web route, a CLI command, a task, a fixture, a signal handler), a method a base class from a library calls (fit, forward, run, handle, setUp), a notebook or a script that calls it.
- "unused": nothing reaches it. A mention in documentation, a comment, a log message or an unrelated string with the same word is not a use. Code reached only from tests is unused in production: answer "unused" and say "only tests use it".
- "unsure": you cannot tell from what is shown: the name is too common to tell its occurrences apart, it is public in a package other repositories may import, or a string may or may not be resolved.

Answer every candidate. Do not guess: "unsure" sends it to a reviewer who reads the code.

Reply with:
{"verdicts": [{"candidate": "K1", "verdict": "unused" | "used" | "unsure", "reason": "one sentence, naming the occurrence that decided it"}]}
