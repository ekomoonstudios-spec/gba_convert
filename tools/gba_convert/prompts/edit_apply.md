# Apply a natural-language edit to one C module

Rewrite the source below to carry out the user's request, while staying
within the surgical-splice constraints. Return JSON only.

## User's request

> {instruction}

## Module being edited: `{module_name}`

```c
{source}
```

## variables.md (naming context — use these names)

```markdown
{variables_md}
```

## Hard constraints

1. **Compile-clean** with:

   ```
   arm-none-eabi-gcc -mthumb -mcpu=arm7tdmi -Os -nostdlib \
       -ffreestanding -Wall -fno-builtin -c
   ```

2. **Signatures are immutable.** The splicer matches the edited function
   to its original byte span by label. If you think the signature needs
   to change to satisfy the request, leave a `// TODO:` comment
   explaining why and keep the original signature.

3. **Only `#include "gba.h"`.** No libc, no heap, no floats (unless the
   original source already used soft-float helpers — then keep them as
   `extern`).

4. **Smaller binary = more likely to fit.** The edit has to slot into the
   original byte span. Prefer branches over tables, constants over
   computed values, simple loops over unrolled ones. If you need to
   shrink: fewer locals, reuse registers, early-return paths.

5. **Preserve `// asm:` backref comments** on functions and non-trivial
   blocks — they're how the reviewer checks the translation.

6. **No `main()`.** This module is linked into a full ROM.

7. **Don't touch unrelated functions** in the module. If the edit only
   concerns one function, leave the others byte-for-byte the same.

## Output format

Return a single JSON object, no prose around it, no markdown fences:

```json
{{
  "c_source": "the FULL updated .c file, verbatim",
  "notes": "one paragraph: what changed and why. Flag any concerns (size risk, semantic ambiguity)."
}}
```

{retry_context}
