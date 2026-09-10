"""
High-Resolution Document & Scribd Universal Extractor for Copita.
Extracts full authentic documents including multi-page graphics, vector typography,
scaled layout coordinates, and active clickable hyperlinks directly into high-fidelity PDFs.
"""

import os
import re
import ssl
import gzip
import shutil
import certifi
import aiohttp
import asyncio
import json
import base64
import subprocess
from typing import Dict, Any, Optional, Callable, List

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

CHAR_MAP = {
    '\ue000': ' ', '\ue001': 'Th', '\ue002': 'ff', '\ue003': 'fi', '\ue004': 'fl',
    '\ue005': 'ffi', '\ue006': 'ffl', '\ue007': 'ft', '\ue008': 'st', '\ue009': 'tt',
    '\ue00a': 'ti', '\ue00b': ' ', '\ue00c': ' '
}

def find_chromium_binary() -> Optional[str]:
    """Find available Chromium/Brave/Chrome headless binary on the host system."""
    candidates = [
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("brave"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser")
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None

class ScribdDownloader:
    def __init__(
        self,
        url: str,
        output_dir: str,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ):
        self.url = url
        self.output_dir = os.path.abspath(output_dir)
        self.progress_callback = progress_callback
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
        self.doc_id = None
        m = re.search(r'document/(\d+)', url)
        if m:
            self.doc_id = m.group(1)

    async def _fetch_html(self, session: aiohttp.ClientSession) -> str:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        async with session.get(self.url, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Scribd page returned HTTP {resp.status}")
            return await resp.text(errors="ignore")

    async def _fetch_page_jsonp(self, session: aiohttp.ClientSession, prefix: str, page_token: str) -> Optional[str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.scribd.com/"
        }
        url = f"https://html.scribdassets.com/{prefix}/pages/{page_token}.jsonp"
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                if resp.status == 200:
                    raw = await resp.read()
                    try:
                        return gzip.decompress(raw).decode("utf-8")
                    except Exception:
                        return raw.decode("utf-8", errors="ignore")
        except Exception as e:
            print(f"[ScribdDownloader] Error fetching page {page_token}: {e}")
        return None

    def _render_reportlab_fallback(self, title: str, total_pages: int, jsonp_contents: List[str], final_pdf_path: str):
        """Fallback vector card generator if headless browser is unavailable."""
        from reportlab.lib.pagesizes import letter
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

        styles = getSampleStyleSheet()
        header_style = ParagraphStyle('HeaderStyle', fontName='Helvetica-Bold', fontSize=12, leading=15, textColor=colors.white, alignment=1)
        sub_header_style = ParagraphStyle('SubHeaderStyle', parent=header_style, fontSize=7.5, leading=9, textColor=colors.HexColor('#fecaca'))
        num_style = ParagraphStyle('NumStyle', fontName='Helvetica-Bold', fontSize=9, leading=11, textColor=colors.HexColor('#475569'), alignment=1)
        item_title_style = ParagraphStyle('ItemTitleStyle', fontName='Helvetica-Bold', fontSize=9.5, leading=12, textColor=colors.HexColor('#0f172a'))
        link_style = ParagraphStyle('LinkStyle', fontName='Helvetica', fontSize=8, leading=10, textColor=colors.HexColor('#2563eb'))

        story = []
        global_item_counter = 1

        for p_idx, raw in enumerate(jsonp_contents, 1):
            if not raw:
                continue
            m = re.search(r'window\.page\d+_callback\(\[(.*)\]\);?', raw, re.DOTALL)
            if not m:
                continue
            html_snippet = json.loads(m.group(1))

            divs = re.findall(r'<div class=\"ff\d+\" style=\"font-size:(\d+)px\">(.*?)</div>', html_snippet, re.DOTALL)
            title_parts = []
            p_items = []
            for sz_str, content in divs:
                sz = int(sz_str)
                if sz < 70 or sz > 120:
                    continue
                orig_links = re.findall(r'orig=[\"\']([^\"\']+)[\"\']', content)
                real_links = []
                for o in orig_links:
                    try:
                        dec = base64.b64decode(o).decode('utf-8')
                        if 'scribd' not in dec and dec.startswith('http'):
                            real_links.append(dec)
                    except Exception:
                        pass
                if real_links:
                    if title_parts:
                        full_title = ' '.join(title_parts)
                        full_title = re.sub(r'THANKS.*PURCHASE', '', full_title, flags=re.IGNORECASE)
                        full_title = re.sub(r'\s+', ' ', full_title).strip()
                        p_items.append((full_title, real_links[0]))
                        title_parts = []
                else:
                    spans = re.findall(r'<span[^>]*>(.*?)</span>', content)
                    cleaned_spans = []
                    for s in spans:
                        t = re.sub(r'<[^>]+>', '', s)
                        for k, v in CHAR_MAP.items():
                            t = t.replace(k, v)
                        t = re.sub(r'[\ue000-\uf8ff\U000f0000-\U000f00ff]', ' ', t)
                        t = t.replace('\xa0', ' ').strip()
                        if t and t not in cleaned_spans:
                            cleaned_spans.append(t)
                    if cleaned_spans:
                        title_text = max(cleaned_spans, key=len)
                        if len(title_text) > 1 and not title_text.startswith('http') and 'scribd' not in title_text.lower():
                            title_parts.append(title_text)

            header_data = [
                [Paragraph(title.upper(), header_style)],
                [Paragraph(f'Page {p_idx} of {total_pages}', sub_header_style)]
            ]
            header_table = Table(header_data, colWidths=[540])
            header_table.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#dc2626')),
                ('ALIGN', (0,0), (-1,-1), 'CENTER'),
                ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
                ('TOPPADDING', (0,0), (-1,-1), 5),
                ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ]))
            story.append(header_table)
            story.append(Spacer(1, 8))

            table_rows = []
            for item_title, item_link in p_items:
                clean_item_title = item_title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                title_p = Paragraph(f'<b>{clean_item_title}</b>', item_title_style)
                safe_link = item_link.replace('&', '&amp;')
                link_p = Paragraph(f'<a href="{safe_link}" color="#2563eb"><u>&#x27A4; {safe_link}</u></a>', link_style)
                badge_p = Paragraph(f'#{global_item_counter:02d}', num_style)
                global_item_counter += 1

                row_content = Table([[title_p], [link_p]], colWidths=[495])
                row_content.setStyle(TableStyle([
                    ('TOPPADDING', (0,0), (-1,-1), 1),
                    ('BOTTOMPADDING', (0,0), (-1,-1), 1),
                    ('LEFTPADDING', (0,0), (-1,-1), 0),
                    ('RIGHTPADDING', (0,0), (-1,-1), 0),
                ]))
                table_rows.append([badge_p, row_content])

            if table_rows:
                content_table = Table(table_rows, colWidths=[35, 505])
                ts = [
                    ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
                    ('ALIGN', (0,0), (0,-1), 'CENTER'),
                    ('TOPPADDING', (0,0), (-1,-1), 3),
                    ('BOTTOMPADDING', (0,0), (-1,-1), 3),
                    ('LEFTPADDING', (0,0), (-1,-1), 4),
                    ('RIGHTPADDING', (0,0), (-1,-1), 4),
                    ('BOX', (0,0), (-1,-1), 1, colors.HexColor('#e2e8f0')),
                    ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#f1f5f9')),
                ]
                for r_i in range(len(table_rows)):
                    bg = colors.HexColor('#ffffff') if r_i % 2 == 0 else colors.HexColor('#f8fafc')
                    ts.append(('BACKGROUND', (0, r_i), (-1, r_i), bg))
                content_table.setStyle(TableStyle(ts))
                story.append(content_table)

            if p_idx < total_pages:
                story.append(PageBreak())

        doc = SimpleDocTemplate(
            final_pdf_path,
            pagesize=letter,
            leftMargin=36,
            rightMargin=36,
            topMargin=28,
            bottomMargin=28
        )
        doc.build(story)

    async def download(self) -> str:
        if self.progress_callback:
            self.progress_callback({"status": "analyzing", "percent": 5.0, "threads": 1})

        connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            html = await self._fetch_html(session)

            title_m = re.search(r'<title>(.*?)</title>', html)
            title = title_m.group(1).replace('| Scribd', '').replace('| PDF', '').strip() if title_m else f"Scribd Document {self.doc_id}"
            safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)[:80].strip()
            final_pdf_path = os.path.join(self.output_dir, f"{safe_title}.pdf")

            prefix_m = re.search(r'assetPrefix\s*=\s*["\']([^"\']+)["\']', html)
            prefix = prefix_m.group(1) if prefix_m else None

            page_matches = re.findall(r'pages/(\d+-[a-zA-Z0-9]+)\.jsonp', html)
            seen_tokens = set()
            page_tokens = []
            for p in page_matches:
                if p not in seen_tokens:
                    seen_tokens.add(p)
                    page_tokens.append(p)

            def get_page_num(token: str) -> int:
                try:
                    return int(token.split('-')[0])
                except Exception:
                    return 9999
            page_tokens.sort(key=get_page_num)

            total_pages = len(page_tokens)
            if not prefix or total_pages == 0:
                raise ValueError("Could not extract Scribd page tokens or asset prefix.")

            if self.progress_callback:
                self.progress_callback({
                    "status": "downloading",
                    "percent": 30.0,
                    "threads": min(16, total_pages),
                    "total": total_pages
                })

            # Concurrently fetch all JSONP page assets
            tasks = [self._fetch_page_jsonp(session, prefix, token) for token in page_tokens]
            jsonp_contents = await asyncio.gather(*tasks)

            if self.progress_callback:
                self.progress_callback({
                    "status": "rendering",
                    "percent": 70.0,
                    "threads": 1
                })

            os.makedirs(self.output_dir, exist_ok=True)
            chromium_bin = find_chromium_binary()

            if chromium_bin:
                # High-Fidelity Exact Vector + Graphic Reconstruction Pipeline
                page_snippets = []
                page_sizes = []

                for raw in jsonp_contents:
                    if not raw:
                        continue
                    m = re.search(r'window\.page\d+_callback\(\[(.*)\]\);?', raw, re.DOTALL)
                    if not m:
                        continue
                    snippet = json.loads(m.group(1))

                    # Replace ligatures
                    for k, v in CHAR_MAP.items():
                        snippet = snippet.replace(k, v)

                    # Extract dimensions
                    dim_m = re.search(r'style="width:\s*(\d+)px;\s*height:(\d+)px"', snippet)
                    w, h = (int(dim_m.group(1)), int(dim_m.group(2))) if dim_m else (900, 1200)
                    page_sizes.append((w, h))

                    # Map image paths to scribdassets
                    def rep_img(match):
                        img_url = match.group(1)
                        fname = img_url.split('/')[-1]
                        return f'src="https://html.scribdassets.com/{prefix}/images/{fname}"'
                    snippet = re.sub(r'orig=["\']([^"\']*images[^"\']*)["\']', rep_img, snippet)

                    # Decode base64 URLs into clickable links
                    def rep_link(match):
                        o = match.group(1)
                        try:
                            dec = base64.b64decode(o).decode('utf-8')
                            return f'href="{dec}" target="_blank"'
                        except Exception:
                            return ''
                    snippet = re.sub(r'orig=["\']([^"\']+)["\']', rep_link, snippet)

                    # Scale text coordinates by 0.2 to match true 1:1 pixel canvas
                    def repl_styles(match):
                        st = match.group(1)
                        st = re.sub(r'top:\s*(-?\d+)px', lambda m: f'top:{round(int(m.group(1))*0.2, 1)}px', st)
                        st = re.sub(r'left:\s*(-?\d+)px', lambda m: f'left:{round(int(m.group(1))*0.2, 1)}px', st)
                        st = re.sub(r'font-size:\s*(-?\d+)px', lambda m: f'font-size:{round(int(m.group(1))*0.2, 1)}px', st)
                        st = re.sub(r'word-spacing:\s*(-?\d+)px', lambda m: f'word-spacing:{round(int(m.group(1))*0.2, 1)}px', st)
                        st = re.sub(r'letter-spacing:\s*(-?\d+)px', lambda m: f'letter-spacing:{round(int(m.group(1))*0.2, 1)}px', st)
                        return f'style="{st}"'

                    snippet = re.sub(r'style="([^"]+)"', repl_styles, snippet)
                    page_snippets.append(snippet)

                w_px, h_px = page_sizes[0] if page_sizes else (900, 1200)
                w_pt = round(w_px * 72 / 96, 2)
                h_pt = round(h_px * 72 / 96, 2)

                full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: {w_pt}pt {h_pt}pt;
    margin: 0;
}}
*, *:before, *:after {{
    box-sizing: border-box;
}}
body {{
    margin: 0;
    padding: 0;
    background: #ffffff;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
}}
.newpage {{
    position: relative !important;
    width: {w_px}px !important;
    height: {h_px}px !important;
    page-break-after: always;
    page-break-inside: avoid;
    overflow: hidden;
}}
.image_layer {{
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    z-index: 1;
}}
.image_layer img.absimg {{
    position: absolute;
    top: 0;
    left: 0;
    width: {w_px}px;
    height: {h_px}px;
}}
.text_layer {{
    width: 100%;
    height: 100%;
    position: absolute;
    top: 0;
    left: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    z-index: 2;
}}
.text_layer div, .text_layer span {{
    white-space: nowrap;
    padding: 0;
    margin: 0;
    border: none;
    line-height: 1;
}}
.text_layer span.a, .text_layer span.g {{
    position: absolute;
    border: none;
}}
.text_layer a.ll {{
    position: absolute;
    display: block;
    color: inherit;
    text-decoration: none;
}}
.link_layer {{
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    z-index: 10;
}}
.link_layer a.ll {{
    position: absolute;
    display: block;
    cursor: pointer;
    z-index: 10;
}}
</style>
</head>
<body>
{" ".join(page_snippets)}
</body>
</html>"""

                tmp_html = os.path.join(self.output_dir, f".temp_{self.doc_id}.html")
                with open(tmp_html, "w", encoding="utf-8") as f:
                    f.write(full_html)

                cmd = [
                    chromium_bin,
                    "--headless",
                    "--disable-gpu",
                    "--no-pdf-header-footer",
                    f"--print-to-pdf={final_pdf_path}",
                    tmp_html
                ]
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, lambda: subprocess.run(cmd, capture_output=True))
                
                if os.path.exists(tmp_html):
                    try:
                        os.remove(tmp_html)
                    except OSError:
                        pass
            else:
                # Fallback to ReportLab vector card document
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self._render_reportlab_fallback(title, total_pages, jsonp_contents, final_pdf_path)
                )

            if not os.path.exists(final_pdf_path) or os.path.getsize(final_pdf_path) == 0:
                raise RuntimeError("Failed to generate PDF document from Scribd stream.")

            if self.progress_callback:
                self.progress_callback({
                    "status": "completed",
                    "percent": 100.0,
                    "threads": 1
                })

            return final_pdf_path
