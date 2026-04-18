/* linker.ld — GBA ROM layout for rebuild.py
 *
 * The Luvdis-generated .s files each declare their own `.text` section
 * starting at offset 0. When linked in module order (mod_0000, mod_0001,
 * ...), they concatenate into a single .text that matches the original
 * ROM's byte layout, because:
 *
 *   - rebuild.py's collect_source_files() globs annotated/mod_*.s and
 *     passes them sorted by filename, which is also address order.
 *   - Each mod_NNNN.s disassembles exactly its original byte span, so
 *     the concatenated sizes match the ROM.
 *
 * If SHA-1 verification fails after a clean build (no splices), check:
 *   (a) module ordering,
 *   (b) .align padding introduced by thumb_func_start macros at module
 *       boundaries (shouldn't happen if original functions were aligned),
 *   (c) rodata emitted by your edited C (the splicer is text-only today).
 */

ENTRY(_08000000)

MEMORY {
    ROM (rx) : ORIGIN = 0x08000000, LENGTH = 32M
}

SECTIONS {
    .text 0x08000000 : {
        *(.text)
        *(.text.*)
        *(.rodata)
        *(.rodata.*)
        *(.data)
        *(.data.*)
    } > ROM

    /DISCARD/ : {
        *(.comment)
        *(.note.*)
        *(.ARM.attributes)
        *(.ARM.exidx*)
        *(.debug_*)
    }
}
