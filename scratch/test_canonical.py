import sqlite3
import re
import unicodedata

conn = sqlite3.connect("data/output/combined/bars.sqlite")
names = [r[0] for r in conn.execute("SELECT DISTINCT university FROM observations ORDER BY university").fetchall()]

def canonical(value):
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    # Normalize typography & quotes
    text = re.sub(r"[„“”\"\'‘’]", "", text)
    # Strip OCR trailing garbage after em-dash (e.g. Thammasat watermark)
    text = re.sub(r"\s*[—–]\s*[^a-zA-Z\s].*$", "", text)
    # Fix broken hyphen spacing (e.g. "Urbana- Champaign")
    text = re.sub(r"-\s+", "-", text)
    # Strip leading "The " or "M.V. "
    text = re.sub(r"^(the|m\.v\.)\s+", "", text, flags=re.IGNORECASE)
    while True:
        match = re.search(r"\s*\(([^()]*)\)\s*$", text)
        if match is None:
            break
        qualifier = match.group(1).strip()
        if qualifier.casefold().startswith("planned transfer"):
            text = text[: match.start()].rstrip()
            continue
        # PRESERVE distinct academic units / schools / campuses
        if any(qualifier.casefold().startswith(prefix) for prefix in [
            "school", "faculty", "graduate school", "department", "college", "campus"
        ]):
            break
        # Match acronyms / short codes including spaces ("IIT Mandi", "UT Dallas", "UDLAP", "POSTECH", "NCKU", "AUI", "NTU", "SIIT", "UIUC", "UCSD", "USP", "UFSC", "UDEC")
        if re.fullmatch(r"[A-Za-z0-9&.+ -]{2,24}", qualifier):
            text = text[: match.start()].rstrip()
        else:
            break
    text = text.casefold()
    return " ".join(text.split())

groups = {}
for n in names:
    k = canonical(n)
    groups.setdefault(k, []).append(n)

merged = {k: v for k, v in groups.items() if len(v) > 1}
print(f"Total distinct before: {len(names)}")
print(f"Total distinct after: {len(groups)}")
print(f"Number of merged groups: {len(merged)}")
for k, v in sorted(merged.items()):
    print(f"\n{k!r}")
    for item in v:
        print(f"   - {item!r}")
