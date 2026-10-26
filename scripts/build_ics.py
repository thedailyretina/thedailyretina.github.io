"""Build pipeline.ics (a subscribable calendar) from pipeline.json.

Only upcoming items with an exact date become events; quarter or half-year targets stay on the page only.
Standard library only. Writes the file only when its content changes, so the workflow commits only real updates.
"""
import json
from datetime import date, timedelta

SRC, OUT = "pipeline.json", "pipeline.ics"
KIND = {"fda": "FDA decision", "eu": "EU decision", "readout": "Readout", "start": "Trial start"}


def esc(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    out, b = [], line.encode("utf-8")
    while len(b) > 74:
        cut = 74
        while (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(b[:cut].decode("utf-8"))
        b = b" " + b[cut:]
    out.append(b.decode("utf-8"))
    return "\r\n".join(out)


def main():
    data = json.load(open(SRC))
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//The Daily Retina//Pipeline calendar//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:The Daily Retina — retina pipeline",
        "X-WR-CALDESC:FDA dates\\, EU decisions and trial readouts in retina. thedailyretina.github.io/pipeline.html",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H", "X-PUBLISHED-TTL:PT12H",
    ]
    stamp = data.get("updated", date.today().isoformat()).replace("-", "") + "T000000Z"
    for it in data.get("items", []):
        if it.get("status") != "upcoming" or not it.get("date"):
            continue
        d = date.fromisoformat(it["date"])
        lines += [
            "BEGIN:VEVENT",
            f"UID:{it['id']}@thedailyretina.github.io",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{esc(KIND.get(it.get('kind'), '') + ': ' + it['title'])}",
            f"DESCRIPTION:{esc(it.get('detail', '') + ' Source: ' + it.get('src', '') + ' ' + it.get('url', ''))}",
            f"URL:{it.get('url', '')}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    text = "\r\n".join(fold(l) for l in lines) + "\r\n"
    try:
        old = open(OUT, newline="").read()
    except FileNotFoundError:
        old = None
    if text != old:
        with open(OUT, "w", newline="") as f:
            f.write(text)
        print("pipeline.ics updated")
    else:
        print("pipeline.ics unchanged")


if __name__ == "__main__":
    main()
