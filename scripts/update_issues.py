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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

FEED = "https://buttondown.com/thedailyretina/rss"
OUT = "issues.json"

SECTION_KEYS = [
    # Current layout (Oct 27, 2026 onward)
    ("lead", re.compile(r"\bbig one\b", re.I)),
    ("hits", re.compile(r"quick hits", re.I)),
    ("numbers", re.compile(r"by the numbers", re.I)),
    ("clock", re.compile(r"on the clock", re.I)),
    # Original layout
    ("top", re.compile(r"\btop\b", re.I)),
    ("fda", re.compile(r"\bfda\b|regulatory|\bcms\b", re.I)),
    ("industry", re.compile(r"industry|treatments|trials", re.I)),
    ("research", re.compile(r"research|papers", re.I)),
    ("guidelines", re.compile(r"guideline|big news|other news", re.I)),
]
# Topic tag line above each item in the current layout: "Research · Surgery", "FDA & CMS", ...
TOPICS = {"fda & cms": "fda", "fda": "fda", "industry": "industry", "research": "research", "guidelines": "guidelines"}
TAG_LINE = re.compile(r"^(FDA\s*&\s*CMS|FDA|Industry|Research|Guidelines)\s*(?:[·•|–-]\s*(.{1,40}))?$", re.I)
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


MONTH_RX = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?"
# Parentheticals like "(earlier item: Oct 15 opinion)" or "(previously reported)".
EARLIER_PAREN = re.compile(r"\s*\((?:[^()]*\b(?:earlier|previously|past)\b[^()]*)\)", re.I)
# Sentences that only date-stamp an item: "Published Oct 15.", "Covered by Healio Oct 19.", "Published July 9; covered by Healio Oct 19."
DATE_ONLY_SENT = re.compile(r"(?:^|(?<=[.;]))\s*(?:(?:first\s+)?(?:published|posted|indexed|covered|reported)\b[^.;]*?" + MONTH_RX + r"[^.;]*(?:[.;]|$))+", re.I)
# Trailing clauses like "Ophthalmic Image, published Oct 15."
DATE_CLAUSE = re.compile(r",\s*(?:first\s+)?(?:published|posted|indexed|covered|reported)\b[^.;,]*?" + MONTH_RX + r"[^.;,]*", re.I)


def real_url(url):
    """Unwrap Gmail/Google redirect links and drop email tracking parameters."""
    parts = urlsplit(url)
    if parts.netloc.endswith("google.com") and parts.path == "/url":
        q = dict(parse_qsl(parts.query))
        if q.get("q", "").startswith("http"):
            return real_url(q["q"])
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def strip_earlier(text):
    text = EARLIER_PAREN.sub("", text or "")
    text = DATE_ONLY_SENT.sub("", text)
    text = DATE_CLAUSE.sub("", text)
    text = clean(text).strip(" ;,")
    if text and not text.endswith((".", ")", "?")):
        text += "."
    return text


def normalize(issue):
    """House style: no summary or coverage line under the date, no earlier/past-news labels, real source URLs."""
    def fix(e, with_ctx):
        out = dict(e)
        out["h"] = clean(EARLIER_PAREN.sub("", e.get("h", ""))).rstrip(". ")
        out["url"] = real_url(e.get("url", ""))
        if with_ctx:
            out["ctx"] = strip_earlier(e.get("ctx", ""))
        return out
    out = {
        "date": issue["date"],
        "top": [fix(t, False) for t in issue.get("top", [])],
        "items": [fix(x, True) for x in issue.get("items", [])],
    }
    if issue.get("lead"):
        lead = fix(issue["lead"], True)
        if lead.get("why"):
            lead["why"] = strip_earlier(lead["why"])
        out["lead"] = lead
    if issue.get("numbers"):
        out["numbers"] = [{"big": clean(n.get("big")), "label": clean(n.get("label"))} for n in issue["numbers"]]
    return out


def section_of(heading):
    for key, rx in SECTION_KEYS:
        if rx.search(heading):
            return key
    return None


def parse_issue(body_html):
    """Return (lead, top, items, numbers). Handles the current layout and the original five-section one."""
    blocks = Blocks()
    blocks.feed(body_html)
    blocks = blocks.close()
    section, lead, top, items, numbers, pending_tag = None, None, [], [], [], None
    for b in blocks:
        text = clean(b["text"])
        bold = clean(b["bold"])
        # Topic tag line ("Research · Surgery") before an item in the current layout.
        if section in ("lead", "hits") and not b["links"]:
            m = TAG_LINE.match(text)
            if m:
                pending_tag = (TOPICS[re.sub(r"\s+", " ", m.group(1).lower())], clean(m.group(2) or ""))
                continue
        if not b["links"] and re.match(r"^nothing (new|notable)", text, re.I):
            continue
        if b["kind"] == "h" or (not b["links"] and section_of(text) and len(text) < 60 and text == (bold or text)):
            s = section_of(text)
            if s:
                section = s
            continue
        if section == "lead" and re.match(r"^why it matters\s*:?", text, re.I) and not b["links"]:
            if lead is not None:
                lead["why"] = re.sub(r"^why it matters\s*:?\s*", "", text, flags=re.I)
            continue
        if section == "numbers" and not b["links"] and bold:
            label = clean(text.replace(bold, "", 1)).lstrip(" :–—-")
            if label:
                numbers.append({"big": bold.rstrip(" :"), "label": label})
            continue
        if section is None or section == "clock" or not b["links"]:
            continue
        if all("buttondown." in l["url"] or "thedailyretina.github.io" in l["url"] for l in b["links"]):
            continue
        if "free subscription" in text.lower():
            continue
        head = bold or clean(b["links"][0]["text"])
        if not head or "nothing notable" in text.lower():
            continue
        link = b["links"][-1] if bold else b["links"][0]
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
        elif section in ("lead", "hits"):
            topic, area = pending_tag or ("research", "")
            pending_tag = None
            item = {"section": topic, **entry, "ctx": ctx}
            if area:
                item["tag"] = area
            if section == "lead" and lead is None:
                lead = dict(item)
                item["lead"] = True
            items.append(item)
        elif section in ("fda", "industry", "research", "guidelines"):
            items.append({"section": section, **entry, "ctx": ctx})
    if lead is not None:
        # Past issues show three headlines: the lead story, then the first two quick hits.
        rest = [x for x in items if not x.get("lead")]
        top = [{"h": x["h"], "url": x["url"], "src": x["src"]} for x in [lead] + rest[:2]]
    return lead, top[:3], items, numbers


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
    old = {i["date"]: i for i in data.get("issues", [])}
    by_date = {d: normalize(i) for d, i in old.items()}
    added = sum(1 for d in by_date if by_date[d] != old[d])
    for it in root.iter("item"):
        title = it.findtext("title") or ""
        if "daily retina" not in title.lower():
            continue
        body = it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or it.findtext("description") or ""
        lead, top, items, numbers = parse_issue(html.unescape(body) if "&lt;" in body else body)
        if not items and not top:
            print("skipped (no items parsed):", title, file=sys.stderr)
            continue
        d = issue_date(title, it.findtext("pubDate") or "")
        new = normalize({"date": d, "lead": lead, "top": top, "items": items, "numbers": numbers})
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
