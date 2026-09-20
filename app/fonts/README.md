# Fonts for portable document exports

These fonts are distributed with Lumori under the SIL Open Font License 1.1.
No fonts are downloaded at runtime. Keep the TTF files and license files with
`app/content_exports.py` when packaging or deploying the application.

## Provenance

Retrieved from the official Noto repositories on 2026-09-20:

- Noto Sans Regular, Bold, Math Regular and Mono Regular: [notofonts/noto-fonts](https://github.com/notofonts/noto-fonts/tree/ffebf8c1ee449e544955a7e813c54f9b73848eac/hinted/ttf), revision `ffebf8c1ee449e544955a7e813c54f9b73848eac`. Upstream paths are `hinted/ttf/NotoSans/NotoSans-Regular.ttf`, `hinted/ttf/NotoSans/NotoSans-Bold.ttf`, `hinted/ttf/NotoSansMath/NotoSansMath-Regular.ttf` and `hinted/ttf/NotoSansMono/NotoSansMono-Regular.ttf`. These binaries are unmodified. The repository's LICENSE is copied to `noto-fonts-LICENSE.txt`.
- Noto Sans SC: [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk/blob/f8d157532fbfaeda587e826d4cd5b21a49186f7c/Sans/Variable/TTF/Subset/NotoSansSC-VF.ttf), revision `f8d157532fbfaeda587e826d4cd5b21a49186f7c`. Source path `Sans/Variable/TTF/Subset/NotoSansSC-VF.ttf`. `Sans/LICENSE` is copied to `noto-cjk-LICENSE.txt`.

The SC variable font was instantiated at `wght=400` with fontTools 4.65.0.
Its name records 2, 4, 6 and 17 were set to Regular, Noto Sans SC Regular,
NotoSansSC-Regular and Regular respectively, matching the static font weight.
All 30,890 upstream mapped Unicode characters are retained. The upstream path
uses "Subset" for the SC regional distribution; Lumori does not subset it to
QA examples or to a fixed course vocabulary. Final binary checksums and sizes
are recorded in `checksums.json`. fontTools is a packaging tool only, not a
runtime dependency.

## Runtime behavior

PDF exports embed only the glyphs used in each document through ReportLab's
TrueType subsetting. Latin text, Chinese text, mathematics and code select
appropriate fonts per text run. DOCX exports embed the complete required fonts
using the OOXML obfuscated-font mechanism, preserving editability for additional
characters in those fonts. Downloads therefore remain readable without Chinese
fonts installed on the recipient's computer.

This is not universal Unicode or mathematical-typesetting coverage. Characters
absent from all supplied fonts are displayed explicitly as `[U+XXXX]`, preserving
the character identity instead of silently omitting it. Saved material is never
changed. Formulas remain authored text; this exporter does not interpret LaTeX
or generate Word equation objects.
