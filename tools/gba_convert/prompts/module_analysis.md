# Module analysis request

You are analysing one module of a GBA disassembly. Follow the rules in
the system prompt (`CLAUDE.md`). Use the existing `variables.md` to keep
names consistent.

## Module metadata

- **File:** `{module_path}`
- **Address range:** `{addr_start}` – `{addr_end}`
- **Kind:** `{kind}`   (code | data | mixed)
- **Lines in this chunk:** `{line_count}`

## Current `variables.md`

```markdown
{variables_md}
```

## Module source

```arm
{module_source}
```

---

## Output format

Respond with **a single JSON object**, nothing else. No prose, no
markdown fences. Schema:

```json
{{
  "annotated_source": "string — the ENTIRE module file with @ comments added. Must still assemble. Preserve every original instruction, label, and directive exactly.",
  "functions": [
    {{
      "address": "0x08XXXXXX",
      "name": "PascalCaseName",
      "mode": "thumb | arm",
      "summary": "one short sentence",
      "args": "r0 = ..., r1 = ...",
      "returns": "r0 = ...",
      "confidence": "high | medium"
    }}
  ],
  "globals": [
    {{
      "address": "0x03XXXXXX",
      "type": "u8 | u16 | u32 | ptr | struct",
      "name": "snake_case_or_leave_empty",
      "purpose": "short description",
      "access": "read | write | rw",
      "confidence": "high | medium"
    }}
  ],
  "io_writes": [
    {{
      "register": "REG_DISPCNT",
      "value_or_source": "#0x0140 | from r2",
      "purpose": "enable mode 0 with BG0+BG1"
    }}
  ],
  "constants": [
    {{
      "value": "0x1E",
      "meaning": "frames per second",
      "context": "sub_08001234 delay counter"
    }}
  ],
  "notes": "one short paragraph of anything that didn't fit above — leave empty string if nothing"
}}
```

## Rules for this response

- Only include `functions` entries with `confidence: "high"` — these will
  be written to `functions.cfg`. Medium-confidence names stay in
  `variables.md` via `notes`.
- If the module is pure data (`kind: data`), `annotated_source` may be
  the original file unchanged, and `functions` will be empty — but still
  fill `globals` / `constants` where the data clearly represents game
  state (a level table, string pool, palette, etc.).
- Do not emit an entry that's already in `variables.md` unchanged.
  Only emit new information or corrections.
- If you cannot analyse the module at all (e.g. it's corrupted or
  entirely unknown bytes), return the object with empty arrays and
  explain in `notes`.
