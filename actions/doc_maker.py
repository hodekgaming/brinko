# actions/doc_maker.py
#
# create_document: BRINKO writes a real document (report, essay, letter,
# cheat sheet, plan…) and you WATCH it being built — a blueprint appears,
# cursors fly in and mark out the layout, headings type themselves, text
# wipes in — then it's saved as PDF (printed by Chromium, so it looks exactly
# like the animation) and/or a styled Word file, and opened.

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

OUT_DIR = Path.home() / "Documents" / "BRINKO"


def _profile() -> str:
    try:
        from actions.smart_writer import _user_profile
        return _user_profile()
    except Exception:
        return ""


def _language(text: str) -> str:
    """German vs English from the request itself (the model otherwise drifts
    toward German because of German details in the user's profile)."""
    words = set(re.findall(r"[a-zäöüß]+", text.lower()))
    de = {"und", "der", "die", "das", "ein", "eine", "über", "für", "mit", "mein", "meine", "ich", "bitte",
          "schreib", "schreibe", "mach", "mache", "erstelle", "bericht", "zusammenfassung", "dokument", "zu", "von"}
    return "German" if (len(words & de) >= 2 or re.search(r"[äöüß]", text.lower())) else "English"


def generate_doc(request: str, title: str = "") -> dict:
    from or_client import client
    prof = _profile()
    prompt = (
        f"Write the content for a document the user asked for: \"{request}\"\n"
        f"{'Title wanted: ' + title + chr(10) if title else ''}"
        f"Today is {datetime.now():%d %B %Y}.\n"
        f"{'About the user (only use if relevant): ' + chr(10) + prof + chr(10) if prof else ''}\n"
        "Write REAL, complete, genuinely useful content: specific facts, numbers, steps and examples. "
        "Only personal details you can't know (names, addresses, signatures) become [placeholders]. "
        f"Write the WHOLE document in {_language(request)} (the language the request was written in), "
        "unless the request explicitly asks for another language.\n"
        "Length: what the request implies. DEFAULT = 4–6 sections, each with 2–3 full paragraphs of "
        "3–5 sentences (or a paragraph plus a list), about 700 words in total. A 'one-page'/'cheat sheet' "
        "request ≈ 400 words; an essay or report 1000+ words. Short, thin sections are a failure.\n\n"
        "It must read like a capable PERSON wrote it, not an AI:\n"
        "- No em dashes. Use commas, full stops or brackets.\n"
        "- Plain, natural title. Never 'The Ultimate/Complete/Essential Guide', never 'X: Y' subtitle formulas.\n"
        "- No openers like 'In today's world', no 'In conclusion', no 'delve', 'leverage', 'unlock', "
        "'seamless', 'robust', 'game-changer', 'navigate the landscape'.\n"
        "- Vary sentence length. Prefer short, concrete sentences. No emoji.\n"
        "- Bold at most a handful of truly key terms in the whole document (often none).\n"
        "- Use lists or a table only where they genuinely help; most text is normal paragraphs. "
        "At most one quote or note box, and only if it adds something.\n"
        "- Headings are short and plain ('Feeding', 'What it costs'), not clever.\n\n"
        "Return ONLY JSON in exactly this shape:\n"
        '{"title": "...", "subtitle": "optional short line or empty", "author": "", "date": "' + datetime.now().strftime("%d %B %Y") + '", '
        '"accent": "a calm, muted hex colour that suits the topic (e.g. #2f5d8a, #3d6b4f, #7a3b2e)", "language": "en", '
        '"filename": "short-file-name", '
        '"sections": [{"heading": "...", "blocks": ['
        '{"type": "paragraph", "text": "..."}, {"type": "bullets", "items": ["..."]}, '
        '{"type": "numbered", "items": ["..."]}, {"type": "table", "rows": [["Header", "Header"], ["cell", "cell"]]}, '
        '{"type": "quote", "text": "..."}, {"type": "callout", "text": "..."}]}]}'
    )
    # fast model: ~2–4 s instead of ~23 s with the thinking model, and the content holds up
    raw = client.chat(prompt, system="You are an experienced human writer and editor. JSON only.",
                      max_tokens=8000, temperature=0.6)
    raw = raw.replace(" — ", ", ").replace("—", ", ")      # belt and braces: no em dashes
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("the writer didn't return a document")
    doc = json.loads(m.group(0))
    doc.setdefault("title", title or "Document")
    doc.setdefault("sections", [])
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(doc.get("accent", ""))):
        doc["accent"] = "#2f5d8a"
    return doc


def _safe_name(doc: dict) -> str:
    base = doc.get("filename") or doc.get("title") or "document"
    base = re.sub(r"[^\w\- äöüÄÖÜß]", "", str(base)).strip().replace(" ", "-")[:60] or "document"
    return base


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 200):
        p = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not p.exists():
            return p
    return path.with_name(f"{path.stem}-{uuid.uuid4().hex[:6]}{path.suffix}")


def build_docx(doc: dict, path: Path):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor, Cm

    accent = RGBColor.from_string(doc["accent"].lstrip("#").upper())
    d = Document()
    for s in d.sections:
        s.left_margin = s.right_margin = Cm(2.2)
        s.top_margin, s.bottom_margin = Cm(2.0), Cm(2.0)
    st = d.styles["Normal"]
    st.font.name = "Calibri"; st.font.size = Pt(11)

    def runs(par, text):
        for i, chunk in enumerate(re.split(r"\*\*(.+?)\*\*", str(text))):
            if chunk:
                r = par.add_run(chunk)
                r.bold = i % 2 == 1

    def shade(cell_or_par, hex_fill):
        el = cell_or_par._tc if hasattr(cell_or_par, "_tc") else cell_or_par._p
        props = el.get_or_add_tcPr() if hasattr(cell_or_par, "_tc") else el.get_or_add_pPr()
        shd = OxmlElement("w:shd"); shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto"); shd.set(qn("w:fill"), hex_fill)
        props.append(shd)

    t = d.add_paragraph(); r = t.add_run(doc.get("title", "")); r.bold = True; r.font.size = Pt(22)
    r.font.color.rgb = RGBColor(0x1F, 0x1F, 0x1F)
    if doc.get("subtitle"):
        p = d.add_paragraph(); r = p.add_run(doc["subtitle"]); r.font.size = Pt(12); r.font.color.rgb = RGBColor(0x6B, 0x5D, 0x78)
    meta = ", ".join(x for x in [doc.get("author"), doc.get("date")] if x)
    if meta:
        p = d.add_paragraph(); r = p.add_run(meta); r.font.size = Pt(10); r.font.color.rgb = RGBColor(0x7F, 0x7F, 0x7F)

    for si, sec in enumerate(doc.get("sections", [])):
        if sec.get("heading"):
            h = d.add_paragraph(); h.paragraph_format.space_before = Pt(14)
            r = h.add_run(sec["heading"]); r.bold = True; r.font.size = Pt(14); r.font.color.rgb = accent
        for b in sec.get("blocks", []):
            typ = b.get("type")
            if typ in ("bullets", "numbered"):
                for it in b.get("items", []):
                    runs(d.add_paragraph(style="List Bullet" if typ == "bullets" else "List Number"), it)
            elif typ == "table" and b.get("rows"):
                rows = b["rows"]; cols = max(len(r) for r in rows)
                tb = d.add_table(rows=len(rows), cols=cols); tb.style = "Table Grid"
                for i, row in enumerate(rows):
                    for j in range(cols):
                        cell = tb.cell(i, j); cell.text = ""
                        runs(cell.paragraphs[0], row[j] if j < len(row) else "")
                        if i == 0:
                            shade(cell, "F2F2F2")
                            for rr in cell.paragraphs[0].runs:
                                rr.bold = True
                d.add_paragraph()
            elif typ == "quote":
                p = d.add_paragraph(); p.paragraph_format.left_indent = Cm(0.6)
                r = p.add_run(b.get("text", "")); r.italic = True; r.font.color.rgb = RGBColor(0x4A, 0x3B, 0x56)
            elif typ == "callout":
                p = d.add_paragraph(); shade(p, "F2F2F2"); p.paragraph_format.left_indent = Cm(0.3)
                runs(p, b.get("text", ""))
            else:
                runs(d.add_paragraph(), b.get("text", ""))
    foot = d.sections[0].footer.paragraphs[0]
    foot.alignment = WD_ALIGN_PARAGRAPH.CENTER          # just a page number, like a normal document
    fld = OxmlElement("w:fldSimple"); fld.set(qn("w:instr"), "PAGE")
    rr = OxmlElement("w:r"); tt = OxmlElement("w:t"); tt.text = "1"; rr.append(tt); fld.append(rr)
    foot._p.append(fld)
    d.save(str(path))


def create_document(parameters=None, player=None, speak=None, **_) -> str:
    p = parameters or {}
    request = str(p.get("request") or p.get("description") or p.get("topic") or "").strip()
    fmt = str(p.get("format") or "pdf").lower()
    want_pdf = fmt in ("pdf", "both", "")
    want_docx = fmt in ("docx", "word", "both")
    if not request:
        return "What should the document be about?"

    fx = getattr(player, "fx", None)            # the builder window (absent in tests / no web engine)
    key = uuid.uuid4().hex
    if fx:
        fx.doc_start(key)
    t0 = time.time()
    try:
        doc = generate_doc(request, str(p.get("title") or ""))
    except Exception as e:
        if fx:
            fx.doc_error(key, f"Couldn't write it: {e}")
        return f"Couldn't write the document: {e}"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    name = _safe_name(doc)
    pdf_path = _unique(OUT_DIR / f"{name}.pdf") if want_pdf else None
    docx_path = _unique(OUT_DIR / f"{name}.docx") if want_docx else None
    made = []

    if fx:
        label = "✓ Saved " + " + ".join(x.name for x in (pdf_path, docx_path) if x)
        fx.doc_feed(key, json.dumps(doc, ensure_ascii=False), str(pdf_path) if pdf_path else "", label)
        ok, msg = fx.doc_wait(key, 180)
        if pdf_path:
            if ok and pdf_path.exists():
                made.append(pdf_path)
            else:
                print(f"[Docs] ⚠️ PDF via builder failed: {msg}")
    if docx_path or (pdf_path and pdf_path not in made):
        target = docx_path or pdf_path.with_suffix(".docx")
        build_docx(doc, target)
        made.append(target)

    if made:
        try:
            os.startfile(str(made[0]))
        except Exception:
            pass
    secs = int(time.time() - t0)
    sections = len(doc.get("sections", []))
    return (f"Created '{doc.get('title')}' ({sections} sections) in {secs}s — saved to "
            + " and ".join(str(m) for m in made) + ". It's open now.")
