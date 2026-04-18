# Route a natural-language edit to the right module

You are picking which C module (in `output/c_view/`) a user's edit request
is most likely about. Return JSON only, no prose.

## User's request

> {instruction}

## variables.md (index of named functions + globals across the ROM)

```markdown
{variables_md}
```

## Available modules

Each filename encodes `mod_<INDEX>_<START_ADDRESS>.c`.

{module_list}

## How to choose

1. First, scan `variables.md` for function or global names that match the
   user's description (by keyword or semantic intent).
2. Each named entity has an address. Find the module whose range contains
   that address (the filename's `<START_ADDRESS>` is the module's base).
3. If multiple modules plausibly fit, list them most-likely-first.
4. If nothing matches confidently, return an empty `candidates` list and
   explain what you'd need to know.

## Output

```json
{{
  "candidates": ["mod_NNNN_ADDR.c", "..."],
  "reasoning": "one sentence: why these"
}}
```
