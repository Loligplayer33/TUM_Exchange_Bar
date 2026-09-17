import urllib.request
import gzip
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

def get_range(url, start, end, timeout=15):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Range": f"bytes={start}-{end}"
        }
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

def get_head_size(url, timeout=10):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        method="HEAD"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(resp.headers["Content-Length"])

def binary_search_offset(cluster_url, total_size, target_surt):
    low = 0
    high = total_size - 1
    best_offset = 0

    for _ in range(32):
        if low >= high:
            break
        mid = (low + high) // 2
        try:
            chunk = get_range(cluster_url, mid, min(mid + 4096, total_size - 1)).decode("latin1", errors="replace")
        except Exception:
            break
        lines = chunk.split("\n")
        if len(lines) < 2:
            break
        key = lines[1].split("\t")[0].split(" ")[0]
        if key < target_surt:
            best_offset = mid
            low = mid + 1
        else:
            high = mid - 1
    return best_offset

def get_blocks_for_prefix(cluster_url, total_size, target_prefix):
    best_offset = binary_search_offset(cluster_url, total_size, target_prefix)
    # Read window
    read_start = max(0, best_offset - 4000)
    read_end = min(total_size - 1, best_offset + 30000)
    try:
        window = get_range(cluster_url, read_start, read_end).decode("latin1", errors="replace")
    except Exception:
        return []

    cluster_lines = [l.strip() for l in window.split("\n") if l.strip()]
    target_blocks = []
    
    # We want any block that starts with target_prefix OR the block immediately preceding the first key >= target_prefix
    for i, line in enumerate(cluster_lines):
        parts = line.split("\t")
        if len(parts) >= 4:
            key = parts[0].split(" ")[0]
            if key >= target_prefix and len(target_blocks) == 0:
                if i > 0:
                    prev_parts = cluster_lines[i - 1].split("\t")
                    target_blocks.append((prev_parts[1], int(prev_parts[2]), int(prev_parts[3])))
            if key.startswith(target_prefix):
                target_blocks.append((parts[1], int(parts[2]), int(parts[3])))
            elif key > target_prefix + "\xff" and len(target_blocks) > 0:
                break

    # De-duplicate
    return list(dict.fromkeys(target_blocks))

def process_collection(coll):
    cluster_url = f"https://data.commoncrawl.org/cc-index/collections/{coll}/indexes/cluster.idx"
    try:
        total_size = get_head_size(cluster_url)
    except Exception as e:
        return coll, f"HEAD error: {e}", []

    prefixes = ["de,tum,international", "de,tum,global"]
    all_blocks = []
    for prefix in prefixes:
        blocks = get_blocks_for_prefix(cluster_url, total_size, prefix)
        all_blocks.extend(blocks)
    all_blocks = list(dict.fromkeys(all_blocks))

    if not all_blocks:
        return coll, "No matching blocks", []

    results = []
    for fn, offset, length in all_blocks:
        cdx_url = f"https://data.commoncrawl.org/cc-index/collections/{coll}/indexes/{fn}"
        try:
            raw = get_range(cdx_url, offset, offset + length - 1)
            data = gzip.decompress(raw).decode("utf-8", errors="replace")
        except Exception as e:
            continue

        for line in data.split("\n"):
            if not line.strip():
                continue
            parts = line.split(" ", 2)
            if len(parts) < 3:
                continue
            surt, ts, js_str = parts
            try:
                rec = json.loads(js_str)
            except Exception:
                continue

            url = rec.get("url", "")
            url_lower = url.lower()
            
            # Keywords of interest
            keywords = ["tumexchange", "auswahl", "chanc", "bewerbung", "partner"]
            matched_kw = [kw for kw in keywords if kw in url_lower]
            
            is_pdf = ".pdf" in url_lower
            is_w00bwe = "fileadmin/w00bwe" in url_lower

            # Keep if:
            # 1. Any PDF under w00bwe with exchange/chancen keywords
            # 2. Any URL with tumexchange
            # 3. Any PDF in TUMexchange directory
            if ("tumexchange" in url_lower) or (is_pdf and matched_kw) or ("studierende/tumexchange" in url_lower):
                rec["collection"] = coll
                rec["timestamp"] = ts
                rec["surt"] = surt
                results.append(rec)

    return coll, f"OK ({len(all_blocks)} blocks, {len(results)} matches)", results

def get_all_collections():
    url = "https://data.commoncrawl.org/cc-index/collections/index.html"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8")
    cols = re.findall(r"CC-MAIN-\d{4}-\d{2}", html)
    seen = set()
    unique = [c for c in cols if not (c in seen or seen.add(c))]
    return [c for c in unique if int(c.split("-")[2]) >= 2018]

def main():
    collections = get_all_collections()
    print(f"Loaded {len(collections)} collections from 2018-2026.")
    
    os.makedirs("data/source/crawled", exist_ok=True)
    all_findings = []
    
    # Use 8 worker threads
    start_time = time.time()
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_to_coll = {executor.submit(process_collection, c): c for c in collections}
        for future in as_completed(future_to_coll):
            coll = future_to_coll[future]
            try:
                c, status, matches = future.result()
                if matches:
                    print(f"[{c}] -> {status}")
                    all_findings.extend(matches)
                else:
                    print(f"[{c}] -> {status}")
            except Exception as e:
                print(f"[{coll}] -> Exception: {e}")

    total_time = time.time() - start_time
    print(f"\nCompleted in {total_time:.1f}s. Total matches collected: {len(all_findings)}")
    
    out_file = "data/source/crawled/all_matches.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(all_findings, f, indent=2)
    print(f"Saved all raw matches to {out_file}")

if __name__ == "__main__":
    main()
