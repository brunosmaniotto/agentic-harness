Task: decide whether one piece of code, a file or a constant is still used, from the code itself.

Static analysis listed it as possibly unused, and a first check could not settle it. You see the candidate, every place its name occurs outside its definition (with surrounding lines), what the first check said, the definition, every place in the repository that looks names up at run time (getattr, importlib, registries), and anything maintainers said about the file.

Work through it:

1. Could any of the occurrences reach it? Read each one in context: is the string resolved to this function, does the config value name it, is this the same object or another with the same name?
2. Could a dynamic lookup reach it without naming it: a registry filled by a decorator or a loop over a module's functions, a plugin system, a class hierarchy where a base class calls the method?
3. Could something outside the repository reach it: an installable package's public name, a scheduled job, a notebook kept elsewhere, another repository? You cannot see those; if the answer depends on them, say so.

Verdicts:

- "unused": nothing in the repository reaches it, tests included, and nothing suggests a use from outside. Removing it would not change what the code does or what the tests run. Calls on a different class that has a method of the same name do not count.
- "tests_only": only tests reach it. Say which test.
- "used": you found the path that reaches it. Name it in the reasoning.
- "needs_human": whether it is used depends on something outside the repository. Write the question a maintainer could answer in one line.

Reply with:
{"verdict": "unused" | "tests_only" | "used" | "needs_human",
 "reasoning": "two or three sentences: what you checked and what decided it",
 "question": "only for needs_human"}
