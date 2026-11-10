"""Convert .docx templates to clean email HTML, and detect field placeholders.

Handles: bold/italic, run colors, font sizes, hyperlinks, inline images
(embedded as local: refs), bullet lists, headings, simple tables.
"""
import hashlib
import re
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from docx.table import Table

IMG_DIR = Path(__file__).resolve().parent / "template_images"

A = chr(38)  # ampersand, to build entities without the decoder eating them
BLIP = '{http://schemas.openxmlformats.org/drawingml/2006/main}blip'
EMBED = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed'
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
PHONE_RE = re.compile(r"(?<![\dA-Za-z])(\+?\d(?:[\d\s\-]*\d){8,12})(?![\dA-Za-z])")


def _esc(text):
    out = text.replace(A, A + "amp;").replace("<", A + "lt;").replace(">", A + "gt;")
    out = (out.replace(chr(8217), A + "#8217;").replace(chr(8216), A + "#8216;")
            .replace(chr(8220), A + "#8220;").replace(chr(8221), A + "#8221;"))
    return out


def _run_html(run):
    text = run.text
    if not text:
        return ""
    esc = _esc(text).replace("\r\n", "\n").replace("\n", "<br>")
    if not esc.replace("<br>", "").strip():
        return esc
    bits = []
    bold = run.bold if run.bold is not None else False
    italic = run.italic if run.italic is not None else False
    if bold:
        bits.append("font-weight:bold")
    if italic:
        bits.append("font-style:italic")
    try:
        color = run.font.color
        if color and color.rgb is not None:
            bits.append("color: #" + str(color.rgb))
    except Exception:
        pass
    if run.font.size:
        bits.append("font-size: " + str(int(round(run.font.size.pt * 4 / 3))) + "px")
    if bits:
        return '<span style="' + ";".join(bits) + '">' + esc + "</span>"
    return esc


def _hyperlink_html(rel, text):
    href = rel.target_ref
    if not href.startswith("http") and not href.startswith("mailto:") and not href.startswith("tel:"):
        href = "https://" + href
    return '<a href="' + href.replace(chr(34), A + "quot;") + '" style="color:#1155cc;">' + _esc(text) + "</a>"


def _inline_image_html(doc, rel_id):
    try:
        part = doc.part.related_parts[rel_id]
        data = part.blob
        ext = Path(part.partname).suffix or ".png"
        name = "img_" + hashlib.md5(data).hexdigest()[:10] + ext
        IMG_DIR.mkdir(exist_ok=True)
        if not (IMG_DIR / name).exists():
            (IMG_DIR / name).write_bytes(data)
        return '<img src="local:' + name + '" alt="" style="max-width:100%;">'
    except Exception:
        return ""


def paragraph_html(doc, para):
    style_name = (para.style.name or "").lower() if para.style is not None else ""
    if not para.text.strip():
        # may still contain an image (drawing nested inside a run)
        parts = []
        for drawing in para._p.iter(qn("w:drawing")):
            for blip in drawing.iter(BLIP):
                embed = blip.get(EMBED)
                if embed:
                    parts.append(_inline_image_html(doc, embed))
        if not parts:
            return ""
        return '<p style="margin:12px 0 10px 0;">' + "".join(parts) + "</p>"

    parts = []
    for child in para._p:
        tag = child.tag
        if tag == qn("w:r"):
            parts.append(_run_html(Run(child, para)))
            for drawing in child.iter(qn("w:drawing")):
                for blip in drawing.iter(BLIP):
                    embed = blip.get(EMBED)
                    if embed:
                        parts.append(_inline_image_html(doc, embed))
        elif tag == qn("w:hyperlink"):
            rid = child.get(qn("r:id"))
            link_text = "".join(t.text or "" for t in child.iter(qn("w:t")))
            if rid and rid in doc.part.rels:
                parts.append(_hyperlink_html(doc.part.rels[rid], link_text))
            else:
                parts.append(_esc(link_text))
        elif tag == qn("w:drawing"):
            for blip in child.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}blip"):
                embed = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
                if embed:
                    parts.append(_inline_image_html(doc, embed))
    html = "".join(parts)
    if not html.strip():
        return ""

    pPr = para._p.pPr
    is_bullet = ("list" in style_name) or (pPr is not None and pPr.numPr is not None)
    if is_bullet:
        return '<p class="mf-bullet" style="margin:0;padding:0;">' + html + "</p>"
    m = re.match(r"heading ([1-4])", style_name)
    if m:
        size = {1: 22, 2: 18, 3: 15, 4: 13}[int(m.group(1))]
        return ('<p style="margin:14px 0 6px 0;font-size:' + str(size) + 'px;font-weight:bold;">'
                + html + "</p>")
    return '<p style="margin:0 0 10px 0;">' + html + "</p>"


BULLET_P = re.compile(r'<p class="mf-bullet" style="margin:0;padding:0;">(.*?)</p>', re.S)


def group_bullets(html):
    """Group consecutive mf-bullet paragraphs into real <ul><li> lists."""
    out, last, i = [], 0, 0
    while True:
        m = BULLET_P.search(html, i)
        if not m:
            out.append(html[last:])
            break
        j = m.end()
        while BULLET_P.match(html, j):
            j = BULLET_P.match(html, j).end()
        items = BULLET_P.findall(html[m.start():j])
        out.append(html[last:m.start()])
        out.append('<ul style="margin:0 0 10px 0;padding-left:24px;">'
                   + "".join("<li>" + it + "</li>" for it in items) + "</ul>")
        last = i = j
    return "".join(out)


def table_html(doc, table):
    rows = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            inner = "".join(paragraph_html(doc, p) for p in cell.paragraphs)
            cells.append('<td style="padding:4px 8px;border:1px solid #ddd;">' + inner + "</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return group_bullets('<table style="border-collapse:collapse;margin:0 0 10px 0;">' + "".join(rows) + "</table>")


def _autolink_text(text):
    def _email(m):
        return '<a href="mailto:' + m.group(0) + '" style="color:#1155cc;">' + m.group(0) + "</a>"
    text = EMAIL_RE.sub(_email, text)
    def _phone(m):
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        if 9 <= len(digits) <= 14:
            return '<a href="tel:' + digits + '" style="color:#1155cc;">' + raw + "</a>"
        return raw
    return PHONE_RE.sub(_phone, text)


def autolink(html):
    parts = re.split(r"(<[^>]+>)", html)
    return "".join(part if part.startswith("<") else _autolink_text(part) for part in parts)


def docx_to_html(docx_path):
    doc = Document(str(docx_path))
    parts = []
    for child in doc.element.body:
        if child.tag == qn("w:p"):
            h = paragraph_html(doc, Paragraph(child, doc))
            if h:
                parts.append(h)
        elif child.tag == qn("w:tbl"):
            h = table_html(doc, Table(child, doc))
            if h:
                parts.append(h)
    body = ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;'
            'line-height:1.5;color:#000000;">' + "".join(parts) + "</div>")
    return autolink(group_bullets(body))


PLACEHOLDER_RE = re.compile(
    r"\{\{([A-Za-z0-9_ .\-]{1,40})\}\}|\{([A-Za-z0-9_ .\-]{1,40})\}"
    r"|\[((?:[A-Za-z][A-Za-z0-9_ .\-]{0,39}))\]"
    r"|\u00ab([A-Za-z][A-Za-z0-9_ .\-]{0,39})\u00bb",
    re.UNICODE,
)


def detect_fields(html_or_text):
    """Return a deduplicated list of placeholder field names (stable order)."""
    text = re.sub(r"<[^>]+>", "", html_or_text)
    fields = []
    for m in PLACEHOLDER_RE.finditer(text):
        name = next(g for g in m.groups() if g).strip()
        if name and name.lower() not in {f.lower() for f in fields}:
            fields.append(name)
    return fields


def normalize_field(name):
    return re.sub(r"[^a-z0-9]+", "", name.lower())


SYNONYMS = [
    ("recruiter", ("hrname", "company", "hr")),
    ("company", ("company", "organization", "organisation")),
    ("organisation", ("company", "organization", "organisation")),
    ("organization", ("company", "organization", "organisation")),
    ("email", ("hremail", "email", "mail")),
]


def suggest_mapping(fields, column_names):
    """Suggest which dataset column fills each template field."""
    cols = [c.strip() for c in column_names if c and c.strip()]
    out = {}
    for f in fields:
        fn = normalize_field(f)
        best = None
        for c in cols:
            if normalize_field(c) == fn:
                best = c
                break
        if not best:
            for c in cols:
                cn = normalize_field(c)
                if fn and (fn in cn or cn in fn) and len(fn) > 2:
                    best = c
                    break
        if not best:
            for key, aliases in SYNONYMS:
                if key in fn:
                    cand, bestscore = None, 0
                    for c in cols:
                        cn = normalize_field(c)
                        for a in aliases:
                            an = normalize_field(a)
                            if cn == an:
                                sc = 3
                            elif cn.startswith(an):
                                sc = 2
                            elif an in cn:
                                sc = 1
                            else:
                                continue
                            if sc > bestscore:
                                bestscore, cand = sc, c
                    if cand:
                        best = cand
                    break
        out[f] = best or ""
    return out


def html_template_body(html_text):
    """Wrap an uploaded HTML fragment consistently."""
    html_text = html_text.strip()
    if html_text.lower().startswith("<!doctype") or html_text.lower().startswith("<html"):
        m = re.search(r"<body[^>]*>(.*)</body>", html_text, re.S | re.I)
        if m:
            html_text = m.group(1).strip()
    if not html_text.startswith("<div"):
        html_text = ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;'
                     'line-height:1.5;color:#000000;">' + html_text + "</div>")
    return html_text
