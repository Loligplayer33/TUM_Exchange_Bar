import sqlite3
import re
import unicodedata

conn = sqlite3.connect("data/output/combined/bars.sqlite")
names = [r[0] for r in conn.execute("SELECT DISTINCT university FROM observations ORDER BY university").fetchall()]

def clean(name):
    t = unicodedata.normalize("NFKC", name).strip()
    # strip quotes
    t = re.sub(r"[„“”\"\'‘’]", "", t)
    # strip OCR garbage watermark on Thammasat
    t = re.sub(r"\s*[—–-]\s*[“\"].*$", "", t)
    # strip leading 'The '
    t = re.sub(r"^the\s+", "", t, flags=re.IGNORECASE)
    # strip leading 'M.V. '
    t = re.sub(r"^m\.v\.\s+", "", t, flags=re.IGNORECASE)
    # strip trailing parenthetical with acronyms (including spaces like IIT Mandi, UT Dallas, UDLAP)
    while True:
        m = re.search(r"\s*\(([^()]*)\)\s*$", t)
        if not m:
            break
        q = m.group(1).strip()
        if q.casefold().startswith("planned transfer"):
            t = t[:m.start()].rstrip()
            continue
        # If it is a school/faculty/campus, keep it!
        if any(q.casefold().startswith(p) for p in ["school", "faculty", "graduate school", "department", "college", "campus"]):
            break
        # If it is an acronym/short identifier, strip it
        if re.fullmatch(r"[A-Za-z0-9&.+ -]{2,20}", q):
            t = t[:m.start()].rstrip()
        else:
            break
    return " ".join(t.casefold().split())

cleaned = {}
for n in names:
    c = clean(n)
    cleaned.setdefault(c, []).append(n)

duplicates = {c: origs for c, origs in cleaned.items() if len(origs) > 1}
print(f"Found {len(duplicates)} duplicate groups:")
for c, origs in sorted(duplicates.items()):
    print(f"\nCanonical key: {c!r}")
    for o in origs:
        # Check years for each
        years = [r[0] for r in conn.execute("SELECT year FROM observations WHERE university = ? ORDER BY year", (o,)).fetchall()]
        print(f"  - {o!r} (years: {years})")
