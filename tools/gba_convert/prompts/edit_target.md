# Route a natural-language edit to the right module

You are picking which C module (in `output/c_view/`) a user's edit request
is most likely about. Return JSON only, no prose.

## User's request

> {instruction}

## Glossary (canonical name registry — small, stable)

```markdown
{glossary}
```

## modules.md (per-module category + one-line summary)

This is the fastest index for routing — each row has `id`, `category`,
and a short `summary`. Prefer this over scanning `variables.md` for
wide queries like "change the audio engine".

```markdown
{modules_md}
```

## Available modules (filtered set you must pick from)

Each filename encodes `mod_<INDEX>_<START_ADDRESS>.c`. This list may
already be pre-filtered by:

- `--category` / `--character` flags, OR
- an FTS5 keyword search over the request text against per-module
  dossiers (pulls the top ~20 matches by bm25 score).

When the list has been pre-filtered, the first few entries are already
the strongest keyword matches — still verify against the glossary and
`modules.md` rather than picking blindly.

{module_list}

## How to choose

1. Decide which **category** the request belongs to (audio / video /
   input / gameplay / ui / system / bios_wrapper / data) and look up
   those ids in `modules.md`.
2. Within that category, match the `summary` column and the glossary
   for named functions/globals hitting the request's keywords or
   semantic intent.
3. Each named entity has an address — the filename's `<START_ADDRESS>`
   is the module's base, so use that to match.
4. If multiple modules plausibly fit, list them most-likely-first.
5. If nothing in the **available modules** list matches confidently,
   return an empty `candidates` list and explain what you'd need.
   Do NOT pick a module that isn't in the available list.

## Output

```json
{{
  "candidates": ["mod_NNNN_ADDR.c", "..."],
  "reasoning": "one sentence: why these"
}}
```
