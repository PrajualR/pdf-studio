import unicodedata
from functools import lru_cache
from pathlib import Path

import fitz
import streamlit as st

st.set_page_config(page_title="PDF Studio", page_icon="📄", layout="wide")

st.markdown("""
<style>
.block-container {padding-top: 1.4rem; max-width: 1500px;}
div[data-testid="stTabs"] button {font-weight: 600;}
.small-note {color: #777; font-size: .88rem;}
</style>
""", unsafe_allow_html=True)

st.title("PDF Studio")
st.caption("A free, local-first PDF editor — edit existing text, add content and manage pages.")

FONT_DIR = Path(__file__).parent / "fonts"
RUPEE_CODEPOINT = 0x20B9

# (regular, bold, italic, bold-italic) base-14 names per family
BASE14 = {
    "Helvetica": ("helv", "hebo", "heit", "hebi"),
    "Times": ("tiro", "tibo", "tiit", "tibi"),
    "Courier": ("cour", "cobo", "coit", "cobi"),
}


# --------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------
def open_pdf(data):
    return fitz.open(stream=data, filetype="pdf")


def to_bytes(doc):
    return doc.tobytes(garbage=4, deflate=True)


def commit(doc):
    """Store the edited document, bump the revision counter and close it."""
    st.session_state.pdf_data = to_bytes(doc)
    st.session_state.rev = st.session_state.get("rev", 0) + 1
    doc.close()


def flash(kind, message):
    """Queue a message that survives st.rerun()."""
    st.session_state.setdefault("flash", []).append((kind, message))


def color_tuple(rgb):
    return (((rgb >> 16) & 255) / 255,
            ((rgb >> 8) & 255) / 255,
            (rgb & 255) / 255)


def font_flags(span):
    flags = span.get("flags", 0)
    return bool(flags & 16), bool(flags & 2)  # bold, italic


# --------------------------------------------------------------------------
# Unicode font handling
# --------------------------------------------------------------------------
def noto_path(bold=False, italic=False):
    if bold and italic:
        name = "NotoSans-BoldItalic.ttf"
    elif bold:
        name = "NotoSans-Bold.ttf"
    elif italic:
        name = "NotoSans-Italic.ttf"
    else:
        name = "NotoSans-Regular.ttf"
    path = FONT_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"Required Unicode fallback font is missing: {path}")
    return path


@lru_cache(maxsize=8)
def load_font(path_str):
    return fitz.Font(fontfile=path_str)


def text_width(text, path, size):
    font = load_font(str(path))
    return max((font.text_length(line, fontsize=size) for line in text.split("\n")), default=0)


def missing_glyphs(text, path):
    font = load_font(str(path))
    return sorted({c for c in text if not c.isspace() and not font.has_glyph(ord(c))})


# --------------------------------------------------------------------------
# Protected glyphs (currency symbols etc.)
# --------------------------------------------------------------------------
def is_protected_glyph(ch):
    """Currency symbols (₹ $ € £ ¥ ...), private-use glyphs and unmappable glyphs."""
    if len(ch) != 1:
        return False
    return unicodedata.category(ch) in ("Sc", "Co") or ch == "\ufffd"


def protected_boxes(page):
    """Bounding boxes of every protected glyph on the page (any span, any font)."""
    boxes = []
    for block in page.get_text("rawdict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    if is_protected_glyph(ch.get("c", "")) and ch.get("bbox"):
                        boxes.append(fitz.Rect(ch["bbox"]))
    return boxes


def clip_away_from_protected(rect, boxes, gap=0.5):
    """
    Shrink the redaction rectangle so it never touches a protected glyph on the
    same line. Redaction removes any character whose box overlaps the rectangle,
    and adjacent glyph boxes share an edge, so a small gap is required.
    """
    rect = fitz.Rect(rect)
    for pb in boxes:
        overlap_y = min(rect.y1, pb.y1) - max(rect.y0, pb.y0)
        if overlap_y < 0.3 * min(rect.height, pb.height):
            continue  # different line
        if pb.x1 <= rect.x0 - gap or pb.x0 >= rect.x1 + gap:
            continue  # not close enough to matter
        if (pb.x0 + pb.x1) / 2 < (rect.x0 + rect.x1) / 2:
            rect.x0 = max(rect.x0, pb.x1 + gap)   # symbol sits to the left
        else:
            rect.x1 = min(rect.x1, pb.x0 - gap)   # symbol sits to the right
    return rect


# --------------------------------------------------------------------------
# Span extraction
# --------------------------------------------------------------------------
def split_span_around_protected(span, chars, base):
    """Split a span into editable groups, leaving protected glyphs out."""
    out = []
    group, kind = [], None
    span_box = fitz.Rect(span["bbox"])

    def flush():
        nonlocal group, kind
        if group:
            value = "".join(c.get("c", "") for c in group)
            boxes = [fitz.Rect(c["bbox"]) for c in group if c.get("bbox")]
            if value.strip() and boxes:
                x0 = min(b.x0 for b in boxes)
                x1 = max(b.x1 for b in boxes)
                out.append({
                    **base,
                    "text": value,
                    # horizontal bounds of the group, full line height of the span
                    "bbox": (x0, span_box.y0, x1, span_box.y1),
                    "origin": tuple(group[0].get("origin", (x0, span_box.y1))),
                    "kind": kind,
                })
        group, kind = [], None

    for ch in chars:
        char = ch.get("c", "")
        if is_protected_glyph(char):
            flush()
            continue
        if char.isspace():
            flush()
            continue
        k = "amount" if (char.isdigit() or char in ".,+-()") else "text"
        if kind != k:
            flush()
            kind = k
        group.append(ch)
    flush()
    return out


def get_spans(page):
    """
    Editable elements for a page. Currency symbols are never exposed as editable;
    numeric characters beside them become separate 'amount' elements.
    """
    result = []
    raw = page.get_text("rawdict")

    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                chars = span.get("chars", [])
                if not chars:
                    continue
                full_text = "".join(ch.get("c", "") for ch in chars)
                if not full_text.strip():
                    continue

                base = {
                    "font": span.get("font", "Unknown"),
                    "size": float(span.get("size", 11)),
                    "color": int(span.get("color", 0)),
                    "flags": int(span.get("flags", 0)),
                }

                if not any(is_protected_glyph(c) for c in full_text):
                    bbox = tuple(span.get("bbox", (0, 0, 0, 0)))
                    result.append({
                        **base, "text": full_text, "bbox": bbox,
                        "origin": tuple(span.get("origin", (bbox[0], bbox[3]))),
                        "kind": "text",
                    })
                else:
                    result.extend(split_span_around_protected(span, chars, base))

    return result


# --------------------------------------------------------------------------
# Text replacement
# --------------------------------------------------------------------------
def replace_span(doc, page_index, span_index, new_text):
    page = doc[page_index]
    spans = get_spans(page)

    if span_index >= len(spans):
        raise ValueError("Text selection is no longer available.")

    span = spans[span_index]

    # In amount-only mode the currency symbol stays on the page, so drop any
    # symbol typed into the replacement to avoid duplicating it.
    if span.get("kind") == "amount":
        cleaned = new_text.lstrip()
        while cleaned and is_protected_glyph(cleaned[0]):
            cleaned = cleaned[1:].lstrip()
        if cleaned != new_text:
            flash("info", "The currency symbol is kept from the original, so it was removed from your replacement.")
        new_text = cleaned

    # Redaction area: the element's bounds, pulled back from any neighbouring
    # currency glyph so the symbol is neither deleted nor painted over.
    rect = fitz.Rect(span["bbox"]) & page.rect
    rect = clip_away_from_protected(rect, protected_boxes(page))
    if rect.is_empty or rect.is_infinite:
        raise ValueError("Could not determine text position.")

    size = span["size"]
    color = color_tuple(span["color"])
    origin = span["origin"]

    bold, italic = font_flags(span)
    font_path = noto_path(bold, italic)
    alias = font_path.stem.replace("-", "")

    if new_text:
        gaps = missing_glyphs(new_text, font_path)
        if gaps:
            flash("warn", "These characters are not in the bundled font and may not render: " + " ".join(gaps))
        if text_width(new_text, font_path, size) > rect.width + 1:
            flash("warn", "The replacement is wider than the original text. The original font size is kept, "
                          "so it may extend beyond its original position.")

    page.add_redact_annot(rect, fill=(1, 1, 1))
    page.apply_redactions(images=0, graphics=0)

    if new_text:
        page.insert_text(
            origin,
            new_text,
            fontname=alias,
            fontfile=str(font_path),
            fontsize=size,
            color=color,
            overlay=True,
        )

    return span


def parse_pages(raw, count):
    selected = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            a, b = int(a.strip()), int(b.strip())
            if a > b:
                a, b = b, a
            selected.update(range(a, b + 1))
        else:
            selected.add(int(token))
    if not selected or min(selected) < 1 or max(selected) > count:
        raise ValueError(f"Enter page numbers from 1 to {count}.")
    return sorted(selected)


# --------------------------------------------------------------------------
# Session state + sidebar
# --------------------------------------------------------------------------
if "pdf_data" not in st.session_state:
    st.session_state.pdf_data = None
if "pdf_filename" not in st.session_state:
    st.session_state.pdf_filename = "edited.pdf"
if "rev" not in st.session_state:
    st.session_state.rev = 0

with st.sidebar:
    st.header("Open a PDF")
    upload = st.file_uploader("Upload PDF", type=["pdf"])
    if upload and st.button("Open PDF", use_container_width=True):
        try:
            d = open_pdf(upload.getvalue())
            if d.is_encrypted and not d.authenticate(""):
                st.error("This PDF is password protected. Unlock it before opening.")
            else:
                st.session_state.pdf_data = to_bytes(d)
                st.session_state.pdf_filename = upload.name.rsplit(".", 1)[0] + "_edited.pdf"
                st.session_state.active_page = 1
                st.session_state.rev += 1
                st.success(f"Opened {d.page_count} pages.")
            d.close()
        except Exception as e:
            st.error(f"Unable to open PDF: {e}")

    if st.session_state.pdf_data:
        st.divider()
        st.download_button("Download edited PDF", st.session_state.pdf_data,
                           file_name=st.session_state.pdf_filename,
                           mime="application/pdf", use_container_width=True)
        if st.button("Close document", use_container_width=True):
            st.session_state.pdf_data = None
            st.rerun()

    # Font health check: confirm the bundled font really contains the rupee glyph.
    try:
        if not load_font(str(noto_path())).has_glyph(RUPEE_CODEPOINT):
            st.error("fonts/NotoSans-Regular.ttf has no ₹ glyph. Replace it with the full "
                     "Noto Sans from Google Fonts.")
    except FileNotFoundError as e:
        st.error(str(e))

if not st.session_state.pdf_data:
    st.info("Upload a PDF from the left panel to begin.")
    st.subheader("PDF editing workspace")
    c1, c2, c3 = st.columns(3)
    c1.markdown("**Edit existing text**\n\nSelect detected text, change its wording and retain its formatting where possible.")
    c2.markdown("**Add new content**\n\nInsert text with font, size, colour and style controls.")
    c3.markdown("**Manage pages**\n\nRotate, delete, extract and merge PDF pages.")
    st.stop()

try:
    doc_view = open_pdf(st.session_state.pdf_data)
except Exception as e:
    st.error(f"Could not load current PDF: {e}")
    st.stop()

page_count = doc_view.page_count
if "active_page" not in st.session_state:
    st.session_state.active_page = 1
st.session_state.active_page = min(max(1, st.session_state.active_page), page_count)

header_l, header_r = st.columns([3, 1])
with header_l:
    st.subheader("Document workspace")
with header_r:
    page_no = st.number_input("Page", min_value=1, max_value=page_count,
                              value=st.session_state.active_page, step=1)
    st.session_state.active_page = int(page_no)

for kind, message in st.session_state.pop("flash", []):
    (st.warning if kind == "warn" else st.info)(message)

page_index = int(page_no) - 1
page = doc_view[page_index]
spans = get_spans(page)

preview_col, edit_col = st.columns([1.15, 1])
with preview_col:
    st.markdown("**Page preview**")
    pix = page.get_pixmap(matrix=fitz.Matrix(1.45, 1.45), alpha=False)
    st.image(pix.tobytes("png"), use_container_width=True)
    st.caption(f"Page {page_no} of {page_count} · {page.rect.width:.0f} × {page.rect.height:.0f} pt")

with edit_col:
    edit_tab, add_tab, organize_tab, merge_tab = st.tabs(
        ["Edit existing text", "Add text", "Pages", "Merge PDFs"]
    )

    # ---------------------------------------------------------------- edit
    with edit_tab:
        st.markdown("**Select text detected on this page**")
        if not spans:
            st.warning("No selectable text was detected. This may be a scanned page or image-only PDF. OCR support is not included in this version.")
        else:
            options = [
                f'{i+1}. {s["text"][:100]!r}  ·  {s["font"]} · {s["size"]:.1f} pt'
                + (" · amount only" if s.get("kind") == "amount" else "")
                for i, s in enumerate(spans)
            ]
            chosen = st.selectbox("Text element", range(len(spans)),
                                  format_func=lambda i: options[i])
            chosen_span = spans[chosen]
            st.text_area("Current text", value=chosen_span["text"], height=90, disabled=True)
            st.caption(f'Font: {chosen_span["font"]} · Size: {chosen_span["size"]:.1f} pt · '
                       f'Colour: #{chosen_span["color"]:06X}')
            if chosen_span.get("kind") == "amount":
                st.info("Amount-only edit: currency symbols are excluded and remain untouched.")
            new_value = st.text_area(
                "Replace with", value=chosen_span["text"], height=100,
                key=f"replacement_{st.session_state.rev}_{page_no}_{chosen}",
            )
            st.caption("The original colour, font size and position are kept. Replacement text is drawn "
                       "in the bundled Noto Sans, so longer text may extend past its original area.")
            if st.button("Apply text edit", type="primary", use_container_width=True):
                try:
                    edit_doc = open_pdf(st.session_state.pdf_data)
                    replace_span(edit_doc, page_index, chosen, new_value)
                    commit(edit_doc)
                except Exception as e:
                    st.error(f"Could not apply edit: {e}")
                else:
                    flash("info", "Text updated. Review the page preview and download when ready.")
                    st.rerun()

            with st.expander("Formatting details"):
                bold, italic = font_flags(chosen_span)
                st.write({
                    "Font family in PDF": chosen_span["font"],
                    "Font size": f'{chosen_span["size"]:.2f} pt',
                    "Colour": f'#{chosen_span["color"]:06X}',
                    "Bold detected": bold,
                    "Italic detected": italic,
                    "Text bounds": [round(v, 1) for v in chosen_span["bbox"]],
                })

    # ----------------------------------------------------------------- add
    with add_tab:
        st.markdown("**Insert new text**")
        with st.form("add_text"):
            add_value = st.text_area("Text")
            a, b = st.columns(2)
            with a:
                ax = st.number_input("X position", min_value=0.0, value=50.0, step=5.0)
                ay = st.number_input("Y position (baseline)", min_value=0.0, value=80.0, step=5.0)
                a_size = st.slider("Font size", 6, 48, 12)
            with b:
                a_font = st.selectbox("Font", list(BASE14))
                a_color = st.color_picker("Text colour", "#202020")
            a_bold = st.checkbox("Bold")
            a_italic = st.checkbox("Italic")
            add_submit = st.form_submit_button("Insert text", use_container_width=True)
        if add_submit:
            if not add_value.strip():
                st.warning("Enter text to insert.")
            else:
                rgb = tuple(int(a_color[i:i+2], 16) / 255 for i in (1, 3, 5))
                try:
                    edit_doc = open_pdf(st.session_state.pdf_data)
                    target_page = edit_doc[page_index]

                    if any(ord(c) > 255 for c in add_value):
                        # Characters such as ₹ need the bundled Unicode font.
                        font_path = noto_path(a_bold, a_italic)
                        fontname, fontfile = font_path.stem.replace("-", ""), str(font_path)
                        gaps = missing_glyphs(add_value, font_path)
                        if gaps:
                            flash("warn", "Not in the bundled font: " + " ".join(gaps))
                    else:
                        fontname = BASE14[a_font][(1 if a_bold else 0) + (2 if a_italic else 0)]
                        fontfile = None

                    target_page.insert_text((ax, ay), add_value, fontsize=a_size,
                                            fontname=fontname, fontfile=fontfile, color=rgb)
                    commit(edit_doc)
                except Exception as e:
                    st.error(f"Could not insert text: {e}")
                else:
                    flash("info", "Text inserted.")
                    st.rerun()

    # --------------------------------------------------------------- pages
    with organize_tab:
        st.markdown("**Rotate page**")
        with st.form("rotate"):
            rot = st.selectbox("Rotation", [90, 180, 270])
            rotate_submit = st.form_submit_button("Rotate current page")
        if rotate_submit:
            d = open_pdf(st.session_state.pdf_data)
            p = d[page_index]
            p.set_rotation((p.rotation + rot) % 360)
            commit(d)
            st.rerun()

        st.divider()
        st.markdown("**Delete current page**")
        if st.button("Delete this page", disabled=page_count <= 1):
            d = open_pdf(st.session_state.pdf_data)
            d.delete_page(page_index)
            commit(d)
            st.session_state.active_page = min(int(page_no), page_count - 1)
            st.rerun()

        st.divider()
        st.markdown("**Extract pages**")
        with st.form("extract"):
            range_text = st.text_input("Page numbers or ranges", placeholder="1-3,5,8-9")
            extract_submit = st.form_submit_button("Prepare extracted PDF")
        if extract_submit:
            try:
                nums = parse_pages(range_text, page_count)
                src = open_pdf(st.session_state.pdf_data)
                out = fitz.open()
                for n in nums:
                    out.insert_pdf(src, from_page=n-1, to_page=n-1)
                st.download_button("Download extracted pages", to_bytes(out),
                                   file_name="extracted_pages.pdf", mime="application/pdf")
                out.close()
                src.close()
            except Exception as e:
                st.error(str(e))

    # --------------------------------------------------------------- merge
    with merge_tab:
        extra_files = st.file_uploader("Choose PDFs to append", type=["pdf"],
                                       accept_multiple_files=True, key="merge_upload")
        if st.button("Merge selected PDFs", disabled=not extra_files):
            try:
                d = open_pdf(st.session_state.pdf_data)
                for f in extra_files:
                    extra = open_pdf(f.getvalue())
                    if extra.is_encrypted and not extra.authenticate(""):
                        raise ValueError(f"{f.name} is password protected.")
                    d.insert_pdf(extra)
                    extra.close()
                commit(d)
            except Exception as e:
                st.error(f"Merge failed: {e}")
            else:
                flash("info", "PDFs merged.")
                st.rerun()

doc_view.close()

st.divider()
with st.expander("Important notes about editing PDF text"):
    st.markdown("""
    - **Existing text editing:** this app detects text spans, removes the selected span using PDF redaction, then writes the replacement into the same area.
    - **Formatting preservation:** original colour, size and position are retained. Replacement text is drawn in the bundled Noto Sans (matching bold/italic), so the typeface can differ slightly from the original.
    - **Currency symbols:** symbols such as ₹ are never edited or redacted in amount-only edits; the redaction area is kept clear of them.
    - **Layout:** exact line spacing, kerning, ligatures and neighbouring content cannot always be preserved. A longer replacement may not fit.
    - **Scanned PDFs:** image-only pages need OCR before text can be selected and edited. OCR is not included in this starter build.
    - **Privacy:** files are processed by the Streamlit process. Run locally for private documents; do not expose the app publicly without adding authentication and deployment security.
    """)