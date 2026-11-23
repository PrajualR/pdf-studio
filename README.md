# PDF Studio — Streamlit PDF Editor

A free local-first PDF editing project using Python, Streamlit and PyMuPDF.

## Run on Windows

```powershell
cd pdf_editor_pro
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

## Run on macOS / Linux

```bash
cd pdf_editor_pro
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Open the local URL printed by Streamlit (normally http://localhost:8501).

## Main feature: edit existing text

The editor extracts text spans from the selected page. Choose a span, edit its wording, and apply the change. It redacts the original span and writes the replacement in the same bounding area, attempting to reuse the embedded font and preserve font size, colour and placement.

## Included tools

- Upload and preview a PDF
- Select and edit existing selectable text
- Preserve font details where possible
- Add new text with font, size, colour, bold and italic options
- Rotate and delete pages
- Extract page ranges
- Merge additional PDFs
- Download the edited document

## Current limitations

PDF is a fixed-layout format, not a word-processing format. The current text-editing approach is best-effort:
- Exact font matching is possible only when the font can be reused or embedded font data is available.
- Font substitution may occur if the original font is not embedded or extraction is restricted.
- Text replacement can change wrapping, kerning, line spacing or alignment. Longer replacements may not fit the original text area.
- The current editor edits detected text spans, not arbitrary individual glyphs or complex multi-column paragraphs.
- Scanned/image-only pages need OCR; OCR is not included.
- Some PDFs restrict editing or use unusual encodings that PyMuPDF cannot reliably reconstruct.

This is a functional starter project, not a full replacement for Acrobat Pro. Test on copies of important documents and visually inspect every edited page.


## Unicode and currency symbols

The project bundles Noto Sans regular, bold, italic and bold-italic font files under `fonts/`. When the original embedded PDF font lacks a character in replacement text, the editor uses the matching Noto Sans style instead. This supports common Unicode characters including the Indian rupee symbol (`₹`). The same Unicode fallback is used when adding text containing non-Latin-1 characters. The bundled Noto Sans font files are distributed under the SIL Open Font License; see `fonts/OFL-LICENSE.txt`.


## Currency-safe amount editing

When a text span contains a supported currency symbol (₹, $, €, £, ¥, ₩ or ₽),
the editor separates the symbol from adjacent numeric content. The currency glyph
is omitted from the edit dropdown, while the numeric amount is presented as its
own editable element. Applying the edit redacts only the numeric characters'
horizontal bounds, retaining the original line height and leaving the currency
symbol outside the redaction region.

This is character-bounds-based protection, not a guarantee for every unusual PDF
font or overlapping glyph layout. Always inspect the edited page before using it.
