#!/usr/bin/env python3
"""
Investigator script for discovering historical TUMexchange document URLs.
Probes candidate filenames and paths on global.tum.de / international.tum.de.
Downloads any HTTP 200 responses, verifies SHA-256 hash, and inspects content with pdfplumber/pypdf.
"""

import concurrent.futures
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_PATHS = [
    "https://www.global.tum.de/fileadmin/w00bwe/www/Wege_ins_Ausland/Studierende/TUMexchange/",
    "https://www.global.tum.de/fileadmin/w00bwe/www/Wege_ins_Ausland/Studierende/",
    "https://www.global.tum.de/fileadmin/w00bwe/www/Wege_ins_Ausland/",
    "https://www.global.tum.de/fileadmin/w00bwe/www/pdf/",
    "https://www.global.tum.de/fileadmin/w00bwe/www/Downloads/",
    "https://www.global.tum.de/fileadmin/w00bwe/www/",
    "https://www.global.tum.de/fileadmin/w00bwe/",
]

YEARS_4DIGIT = [str(y) for y in range(2012, 2029)]
YEARS_2DIGIT = [str(y)[2:] for y in range(2014, 2029)]
ACADEMIC_YEARS_UNDERSCORE = [f"{y}_{str(y+1)[2:]}" for y in range(2013, 2028)] # e.g. 2023_24
ACADEMIC_YEARS_DASH = [f"{y}-{str(y+1)[2:]}" for y in range(2013, 2028)]       # e.g. 2023-24
ACADEMIC_YEARS_FULL = [f"{y}_{y+1}" for y in range(2014, 2028)]                # e.g. 2023_2024
ACADEMIC_YEARS_FULL_DASH = [f"{y}-{y+1}" for y in range(2014, 2028)]           # e.g. 2023-2024
ACADEMIC_YEARS_SHORT = [f"{str(y)[2:]}_{str(y+1)[2:]}" for y in range(2014, 2028)] # e.g. 23_24
SEMESTER_YEARS = [f"WS{str(y)[2:]}{str(y+1)[2:]}" for y in range(2014, 2028)] # e.g. WS2324
WISE_YEARS = [f"WiSe_{str(y)[2:]}_{str(y+1)[2:]}" for y in range(2014, 2028)] # e.g. WiSe_23_24

ALL_YEARS = (
    YEARS_4DIGIT
    + ACADEMIC_YEARS_UNDERSCORE
    + ACADEMIC_YEARS_DASH
    + ACADEMIC_YEARS_FULL
    + ACADEMIC_YEARS_FULL_DASH
    + ACADEMIC_YEARS_SHORT
    + YEARS_2DIGIT
    + SEMESTER_YEARS
    + WISE_YEARS
)

def generate_candidate_batches():
    tier1 = set() # Ultra-high probability (direct core patterns)
    tier2 = set() # Medium probability (presentations, spreadsheets, extra suffixes)
    tier3 = set() # Broad patterns

    years_4d = [str(y) for y in range(2013, 2029)]
    years_acad_under = [f"{y}_{str(y+1)[2:]}" for y in range(2013, 2028)] # 2023_24
    years_acad_dash = [f"{y}-{str(y+1)[2:]}" for y in range(2013, 2028)]  # 2023-24
    years_acad_full = [f"{y}_{y+1}" for y in range(2015, 2028)]           # 2023_2024
    years_acad_short = [f"{str(y)[2:]}_{str(y+1)[2:]}" for y in range(2015, 2028)] # 23_24
    years_ws = [f"WS{str(y)[2:]}{str(y+1)[2:]}" for y in range(2015, 2028)] # WS2324
    years_2d = [str(y)[2:] for y in range(2015, 2029)] # 15..28

    primary_years = years_4d + years_acad_under + years_acad_dash
    secondary_years = years_acad_full + years_acad_short + years_ws + years_2d

    # Core templates:
    # 1. TUMexchange_Chances / TUMex_Chances
    core_prefixes = ["TUMexchange", "TUMex", "TUM_Exchange"]
    core_keywords = ["Chances", "Chancen", "Auswahlchancen"]
    core_suffixes = ["", "_DE", "_EN", "_v1", "_v2", "_v01", "_v02", "_final", "_neu"]

    for p in core_prefixes:
        for kw in core_keywords:
            for y in primary_years:
                for s in core_suffixes:
                    tier1.add(f"{p}_{kw}_{y}{s}.pdf")
            for y in secondary_years:
                for s in ["", "_DE", "_EN", "_v1", "_v2"]:
                    tier2.add(f"{p}_{kw}_{y}{s}.pdf")

    # 2. Auswahlquote / Auswahlquoten / neu_Auswahlquote
    aq_patterns = [
        "Auswahlquote_TUMexchange",
        "Auswahlquoten_TUMexchange",
        "neu_Auswahlquote_TUMexchange",
        "neu_Auswahlquoten_TUMexchange",
        "Auswahlquote_TUMex",
        "neu_Auswahlquote_TUMex",
        "Auswahlquote",
        "Auswahlquoten",
        "neu_Auswahlquote",
        "neu_Auswahlquoten",
        "Bewerberstatistik_TUMexchange",
        "Bewerberstatistik",
        "Bewerbungschancen_TUMexchange",
        "Bewerbungschancen",
    ]
    for aq in aq_patterns:
        for y in primary_years:
            for s in core_suffixes:
                tier1.add(f"{aq}_{y}{s}.pdf")
        for y in secondary_years:
            for s in ["", "_DE", "_EN"]:
                tier2.add(f"{aq}_{y}{s}.pdf")

    # 3. Direct prefix_year
    for p in ["TUMexchange", "TUMex", "Chances", "Chancen", "Bewerbungschancen"]:
        for y in primary_years:
            for s in ["", "_DE", "_EN", "_final", "_v1", "_v2"]:
                tier1.add(f"{p}_{y}{s}.pdf")

    # 4. Presentations / Infoveranstaltung
    info_templates = [
        "Infoveranstaltung_TUMexchange",
        "Infoveranstaltung",
        "Presentation_TUMexchange",
        "Praesentation_TUMexchange",
        "Wege_ins_Ausland",
        "Wege_ins_Ausland_TUMexchange",
        "TUMexchange_Infoveranstaltung",
        "TUMexchange_Presentation",
        "TUMexchange_Info",
        "TUMexchange_Overview",
        "TUMexchange_Uebersicht",
    ]
    for t in info_templates:
        for y in years_4d + years_acad_under:
            for s in ["", "_DE", "_EN", "_final", "_v1", "_v2"]:
                tier2.add(f"{t}_{y}{s}.pdf")

    # 5. Spreadsheets (.xlsx, .xls)
    for p in ["TUMexchange", "TUMex"]:
        for kw in ["Chances", "Chancen", "Auswahlquote", "Auswahlquoten", "Bewerberstatistik", "Statistik"]:
            for y in years_4d + years_acad_under:
                for s in ["", "_DE", "_EN"]:
                    for ext in [".xlsx", ".xls"]:
                        tier2.add(f"{p}_{kw}_{y}{s}{ext}")

    # 6. Broader permutations (tier 3)
    broader_prefixes = ["TUM_exchange", "TUM-Exchange", "tumexchange", "Auswahl", "Statistik", "Selection"]
    for p in broader_prefixes:
        for kw in ["Chances", "Chancen", "Selection", "Auswahl", "Bewerber", "Statistik"]:
            for y in years_4d:
                for s in ["", "_DE", "_EN"]:
                    tier3.add(f"{p}_{kw}_{y}{s}.pdf")
                    tier3.add(f"{p}_{y}{s}.pdf")

    return sorted(tier1), sorted(tier2), sorted(tier3)

def generate_filenames():
    t1, t2, t3 = generate_candidate_batches()
    return sorted(list(set(t1 + t2 + t3)))

def check_url(url, timeout=4.0):
    """
    Check if URL exists using HEAD request, fallback to GET if HEAD rejected.
    Returns (url, status_code, content_length, last_modified, content_type, error)
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }
    for method in ["HEAD", "GET"]:
        try:
            req = urllib.request.Request(url, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                cl = resp.headers.get("Content-Length")
                lm = resp.headers.get("Last-Modified")
                ct = resp.headers.get("Content-Type", "")
                return url, status, cl, lm, ct, None
        except urllib.error.HTTPError as e:
            if e.code == 405 and method == "HEAD":
                continue # try GET
            return url, e.code, None, None, None, None
        except urllib.error.URLError as e:
            return url, None, None, None, None, str(e.reason)
        except Exception as e:
            return url, None, None, None, None, str(e)
    return url, None, None, None, None, "Unknown error"

def download_file(url, target_path):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp, open(target_path, "wb") as f:
        content = resp.read()
        f.write(content)
    return content

def inspect_pdf(filepath):
    """Inspect metadata and text summary using pypdf and pdfplumber."""
    info = {"pages": 0, "metadata": {}, "text_sample": ""}
    try:
        import pypdf
        reader = pypdf.PdfReader(filepath)
        info["pages"] = len(reader.pages)
        if reader.metadata:
            info["metadata"] = {k: str(v) for k, v in reader.metadata.items()}
        text = ""
        for i in range(min(5, len(reader.pages))):
            page_text = reader.pages[i].extract_text() or ""
            if page_text.strip():
                text += f"\n--- Page {i+1} ---\n" + page_text[:1000]
        info["text_sample"] = text[:2500]
    except Exception as e:
        info["pdf_error"] = str(e)

    # Check for vector geometry / tables
    try:
        import pdfplumber
        with pdfplumber.open(filepath) as pdf:
            curves_count = sum(len(p.curves) for p in pdf.pages[:3])
            rects_count = sum(len(p.rects) for p in pdf.pages[:3])
            info["vector_curves"] = curves_count
            info["vector_rects"] = rects_count
    except Exception as e:
        info["plumber_error"] = str(e)

    return info

def main():
    out_dir = Path("data/discovered")
    out_dir.mkdir(parents=True, exist_ok=True)
    results_file = out_dir / "discovered_results.json"

    print("Generating candidate filenames...")
    filenames = generate_filenames()
    print(f"Generated {len(filenames)} unique candidate filenames.")

    # Phase 1: Test primary TUMexchange path first
    primary_path = BASE_PATHS[0]
    primary_urls = [f"{primary_path}{fn}" for fn in filenames]
    print(f"Phase 1: Testing {len(primary_urls)} candidate URLs on primary base path: {primary_path}")

    discovered = []
    active_errors = 0

    max_workers = 8
    tested_count = 0
    start_time = time.time()

    def run_probe_batch(urls, batch_name):
        nonlocal tested_count, active_errors
        found_batch = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_url = {executor.submit(check_url, u): u for u in urls}
            for future in concurrent.futures.as_completed(future_to_url):
                tested_count += 1
                url, status, cl, lm, ct, err = future.result()
                if status == 200:
                    print(f"\n🎯 [FOUND 200 OK] {url} (Length: {cl}, Last-Modified: {lm}, Type: {ct})")
                    found_batch.append((url, cl, lm, ct))
                elif status is not None and status != 404:
                    print(f"\nℹ️ [STATUS {status}] {url}")
                elif err and "connection refused" in str(err).lower():
                    active_errors += 1
                    if active_errors % 20 == 0:
                        print(f"⚠️ Warning: Connection refused count {active_errors}. Backing off slightly...")
                        time.sleep(2)

                if tested_count % 250 == 0:
                    elapsed = time.time() - start_time
                    rate = tested_count / elapsed if elapsed > 0 else 0
                    print(f"[{batch_name}] Tested {tested_count}/{len(urls)} ({rate:.1f} req/s). Found so far: {len(discovered) + len(found_batch)}")
        return found_batch

    found_primary = run_probe_batch(primary_urls, "Phase 1 - TUMexchange Base")
    discovered.extend(found_primary)

    # If any hits found, see if successful patterns should be tested on other base paths
    hit_patterns = [Path(urllib.parse.urlparse(u).path).name for u, _, _, _ in found_primary]
    print(f"\nPhase 1 complete. Found {len(found_primary)} files on primary path.")
    print("Hit filenames on primary path:", hit_patterns)

    # Phase 2: Probe secondary base paths with the top candidate patterns
    # (including known patterns and variants of what worked)
    secondary_filenames = set(hit_patterns)
    # Add top high-value filenames to test across all other base paths
    for p in ["TUMexchange", "TUMex"]:
        for kw in ["Chances", "Chancen", "Bewerbungschancen", "Auswahlquote", "Auswahlquoten", "Bewerberstatistik"]:
            for y in YEARS_4DIGIT + ACADEMIC_YEARS_UNDERSCORE:
                for s in ["", "_DE", "_EN", "_v1", "_v2", "_final", "_neu"]:
                    secondary_filenames.add(f"{p}_{kw}_{y}{s}.pdf")
                    secondary_filenames.add(f"neu_{kw}_{p}_{y}{s}.pdf")
                    secondary_filenames.add(f"{kw}_{p}_{y}{s}.pdf")

    # Add presentation templates across other paths
    for t in ["Infoveranstaltung_TUMexchange", "Presentation_TUMexchange", "Wege_ins_Ausland_TUMexchange", "Infoveranstaltung"]:
        for y in ["2023", "2024", "2025", "2026", "2027", "2023_24", "2024_25"]:
            for s in ["", "_DE", "_EN"]:
                secondary_filenames.add(f"{t}_{y}{s}.pdf")

    secondary_urls = []
    for bp in BASE_PATHS[1:]:
        for fn in secondary_filenames:
            secondary_urls.append(f"{bp}{fn}")

    print(f"\nPhase 2: Testing {len(secondary_urls)} candidate URLs across {len(BASE_PATHS)-1} secondary base paths...")
    tested_count = 0
    found_secondary = run_probe_batch(secondary_urls, "Phase 2 - Secondary Paths")
    discovered.extend(found_secondary)

    print(f"\n🎉 Total Discovered Files: {len(discovered)}")
    detailed_results = []

    for item in discovered:
        url, cl, lm, ct = item
        filename = Path(urllib.parse.urlparse(url).path).name
        target_path = out_dir / filename
        print(f"\nDownloading {url} -> {target_path} ...")
        try:
            content = download_file(url, target_path)
            sha256 = hashlib.sha256(content).hexdigest()
            print(f"Saved {filename} ({len(content)} bytes, SHA-256: {sha256})")

            # Inspect if PDF
            file_info = {
                "url": url,
                "filename": filename,
                "content_length": cl,
                "last_modified": lm,
                "content_type": ct,
                "sha256": sha256,
                "size_bytes": len(content),
                "local_path": str(target_path.resolve()),
            }

            if filename.lower().endswith(".pdf"):
                pdf_info = inspect_pdf(target_path)
                file_info.update(pdf_info)

            detailed_results.append(file_info)
        except Exception as e:
            print(f"Error downloading/inspecting {url}: {e}")

    with open(results_file, "w") as f:
        json.dump(detailed_results, f, indent=2)

    print(f"\nSaved summary report to {results_file}")

if __name__ == "__main__":
    main()
