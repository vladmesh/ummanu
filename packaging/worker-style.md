These rules apply in every project; the project's AGENTS.md wins on conflict.

Prose
1. Docstring = the contract or invariant in 1-3 lines. No history, incidents, "previously", "now". Why: git keeps history; every later head pays to read it.
2. Comment only a why the code cannot show. Never restate the code. Why: the next head copies the density it sees.
3. No real work-item ids or dates in code, comments, docstrings or test names; synthetic fixture data is exempt. Why: references rot.
4. No em dashes, bold, or filler openers ("Note that", "This ensures"). Why: noise tokens.

Tests
5. Test observable behaviour through the public seam. Why: shape tests break on refactor and protect nothing.
6. Do not add a test that:
   - asserts a removed name, file, field, flag or wording is absent;
   - reads checked-in docs, prompts, skills, Markdown or product source as text; runtime product output is exempt;
   - compares a constant with its own literal, or repeats an existing test.
   Why: deleting code and its tests and passing the suite proves a removal; a boundary is a lint rule.
7. Delete the tests of code you delete. Why: they either fail or keep testing nothing.

Code
8. Rename or move = update every caller in the same change. No alias, `X as X` re-export, facade module or fallback for the old name unless the card names a released public API. Why: aliases outlive their callers.
9. Search before writing a helper (rg "def name", same verb); reuse or move the existing one. Why: copies drift into bugs.
10. A missing required value or a failure is an error, never an empty default, broad except or getattr(x, "attr", None) probe. Why: silent paths hide defects; fix the fake, not the product.
11. Remove what your change made dead: params, branches, constants, imports. Why: the next audit should not find it.
12. Stay inside the card; name adjacent findings in the report. Why: scope creep is reviewed and paid twice.

Report
13. In report:done state: prose lines added and removed, each new test and the behaviour it proves, any alias kept and the card that may drop it. Why: the reviewer checks these against the diff.

Docs
14. Docs hold the current state only: no history, task progress, experiments, option comparisons or acceptance evidence in any file; reasoning goes into the report. On a behaviour change rewrite its one canonical description in place and delete stale copies elsewhere. Why: a doc reads as the contract; copies drift.
