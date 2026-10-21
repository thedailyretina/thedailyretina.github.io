"""Rebuild issues.json from the newsletter's public Buttondown RSS feed.

Only issues that were actually sent appear in the feed, so only those reach the site.
Existing entries in issues.json are kept; feed entries are added or replace the same date.
Standard library only.
"""
import email.utils
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser

FEED = "https://buttondown.com/thedailyretina/rss"
OUT = "issues.json"

SECTION_KEYS = [
    ("top", re.compile(r"\btop\b", re.I)),
    ("fda", re.compile(r"\bfda\b|regulatory|\bcms\b", re.I)),
    ("industry", re.compile(r"industry|treatments|trials", re.I)),
    ("research", re.compile(r"research|papers", re.I)),
    ("guidelines", re.compile(r"guideline|big news|other news", re.I)),
]
MONTHS = "January February March April May June July August September October November December".split()


class Blocks(HTMLParser):
    """Turn email HTML into a flat list of headings and text blocks with their bold text and links."""

    HEAD = {"h1", "h2", "h3", "h4"}
    BLOCK = {"p", "li", "div", "td"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.cur, self.depth_b, self.link = [], None, 0, None

    def _open(self, kind):
        self._close()
        self.cur = {"kind": kind, "text": "", "bold": "", "links": []}

    def _close(self):
        if self.cur and self.cur["text"].strip():
            self.out.append(self.cur)
        self.cur = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.HEAD:
            self._open("h")
        elif tag in self.BLOCK:
            self._open("p")
        elif tag == "br" and self.cur:
            self.cur["text"] += " "
        elif tag in ("strong", "b"):
            self.depth_b += 1
        elif tag == "a" and a.get("href", "").startswith("http"):
            if self.cur is None:
                self._open("p")
            self.link = {"url": a["href"], "text": ""}

    def handle_endtag(self, tag):
        if tag in self.HEAD or tag in self.BLOCK:
            self._close()
        elif tag in ("strong", "b"):
            self.depth_b = max(0, self.depth_b - 1)
        elif tag == "a" and self.link is not None:
            if self.cur is not None:
                self.cur["links"].append(self.link)
            self.link = None

    def handle_data(self, data):
        if self.cur is None:
            if not data.strip():
                return
            self._open("p")
        self.cur["text"] += data
        if self.depth_b:
            self.cur["bold"] += data
        if self.link is not None:
            self.link["text"] += data

    def close(self):
        super().close()
        self._close()
        return self.out


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def section_of(heading):
    for key, rx in SECTION_KEYS:
        if rx.search(heading):
            return key
    return None


def parse_issue(body_html):
    blocks = Blocks()
    blocks.feed(body_html)
    blocks = blocks.close()
    coverage, section, top, items = "", None, [], []
    for b in blocks:
        text = clean(b["text"])
        if b["kind"] == "h" or (not b["links"] and section_of(text) and len(text) < 60 and text == clean(b["bold"] or text)):
            s = section_of(text)
            if s:
                section = s
            continue
        if text.lower().startswith("coverage"):
            coverage = re.sub(r"^coverage:\s*", "", text, flags=re.I).rstrip(".")
            continue
        if section is None or not b["links"]:
            continue
        if all("buttondown." in l["url"] for l in b["links"]) or "free subscription" in text.lower():
            continue
        head = clean(b["bold"]) or clean(b["links"][0]["text"])
        if not head or "nothing notable" in text.lower():
            continue
        link = b["links"][-1] if b["bold"] else b["links"][0]
        url = link["url"]
        src = clean(link["text"]) if clean(link["text"]) != head else ""
        ctx = text.replace(head, "", 1)
        if src:
            ctx = ctx[::-1].replace(src[::-1], "", 1)[::-1]
        ctx = clean(ctx).strip(" .–—-:")
        ctx = (ctx + ".") if ctx and not ctx.endswith((".", ")")) else ctx
        head = head.rstrip(". ")
        entry = {"h": head, "url": url, "src": src or "Source"}
        if section == "top":
            top.append(entry)
        else:
            items.append({"section": section, **entry, "ctx": ctx})
    return coverage, top[:3], items


def issue_date(title, pub):
    m = re.search(r"(" + "|".join(MONTHS) + r")\s+(\d{1,2}),\s*(\d{4})", title or "")
    if m:
        return date(int(m.group(3)), MONTHS.index(m.group(1)) + 1, int(m.group(2))).isoformat()
    dt = email.utils.parsedate_to_datetime(pub).astimezone(timezone(timedelta(hours=-5)))
    return dt.date().isoformat()


def main():
    req = urllib.request.Request(FEED, headers={"User-Agent": "thedailyretina-site-updater"})
    xml = urllib.request.urlopen(req, timeout=30).read()
    root = ET.fromstring(xml)
    try:
        with open(OUT) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"issues": []}
    by_date = {i["date"]: i for i in data.get("issues", [])}
    added = 0
    for it in root.iter("item"):
        title = it.findtext("title") or ""
        if "daily retina" not in title.lower():
            continue
        body = it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or it.findtext("description") or ""
        coverage, top, items = parse_issue(html.unescape(body) if "&lt;" in body else body)
        if not items and not top:
            print("skipped (no items parsed):", title, file=sys.stderr)
            continue
        d = issue_date(title, it.findtext("pubDate") or "")
        new = {"date": d, "coverage": coverage, "top": top, "items": items}
        if by_date.get(d) != new:
            by_date[d] = new
            added += 1
    issues = sorted(by_date.values(), key=lambda i: i["date"], reverse=True)
    if added:
        data = {"updated": datetime.now(timezone.utc).date().isoformat(), "issues": issues}
        with open(OUT, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
    print(f"{added} issue(s) added or updated; {len(issues)} total")


if __name__ == "__main__":
    main()
