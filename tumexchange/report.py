"""Render a self-contained, provenance-first HTML report.

The report deliberately keeps the data model small:

* one current observation is retained for each university/year;
* measured bars and explicit ``no_applications`` assignments contribute to
  means;
* unavailable observations remain visible, but do not contribute; and
* every annual row carries the source release and calibration that produced
  it.

The HTML contains all CSS, data, and JavaScript inline.  It can therefore be
opened directly from disk without a server or a network connection.
"""

from __future__ import annotations

import csv
import html
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .profiles import attach_profiles


CANONICAL_FIELDS = (
    "country",
    "university",
    "year",
    "page",
    "row_index",
    "status",
    "score_percent",
    "raw_width_pt",
    "full_width_pt",
    "calibration_id",
    "bar_bbox",
    "notes",
)

_STATUS_LABELS = {
    "measured": "Recorded",
    "no_applications": "100% (No applicants)",
    "ambiguous": "Ambiguous",
    "missing": "No data",
    "no_cooperation": "No cooperation",
    "no_places": "No places",
    "bar_absent": "No data",
}

_CATEGORY_LABELS = {
    "measured": "Measured",
    "assigned": "100% (No applicants)",
    "ambiguous": "Ambiguous",
    "na": "No active exchange",
}

_UNAVAILABLE_STATUSES = {
    "missing",
    "no_cooperation",
    "no_places",
    "bar_absent",
}

_SOURCE_FIELDS = (
    "source_document_id",
    "source_sha256",
    "source_filename",
    "source_release_year",
    "extraction_version",
)

_CALIBRATION_KEYS = {
    "calibration_id",
    "full_width_pt",
    "full_width_pt_by_year",
    "full_width_by_year",
    "formula",
    "method",
    "units",
    "tolerance_pt",
    "reference_bars",
    "methodology",
    "status_assumptions",
}


def _jsonable(value: Any) -> Any:
    """Return a JSON-compatible copy, tolerating useful metadata objects."""

    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _safe_json(value: Any, *, indent: int | None = None) -> str:
    """Serialize JSON safely for files and inline script data blocks."""

    text = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        indent=indent,
        separators=None if indent is not None else (",", ":"),
    )
    # Escaping ``<`` prevents a value such as ``</script>`` from ending the
    # JSON script element.  The other escapes keep the block parser-friendly.
    return (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        number = _finite_float(value)
        if number is None or not number.is_integer():
            return None
        return int(number)


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value
    return str(value)


def _nullable_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return _text(value)


def _canonical_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _text(value))
    return " ".join(text.casefold().split())


def _university_identity(value: Any) -> str:
    """Match harmless release-to-release suffix changes without fuzzy aliases."""

    text = unicodedata.normalize("NFKC", _text(value)).strip()
    text = re.sub(r"[„“”\"\'‘’]", "", text)
    text = re.sub(r"\s*[—–].*$", "", text)
    text = re.sub(r"^(the|m\.v\.)\s+", "", text, flags=re.IGNORECASE)
    while True:
        match = re.search(r"\s*\(([^()]*)\)\s*$", text)
        if match is None:
            break
        qualifier = match.group(1).strip()
        if qualifier.casefold().startswith("planned transfer"):
            text = text[: match.start()].rstrip()
            continue
        if any(
            qualifier.casefold().startswith(prefix)
            for prefix in [
                "school",
                "faculty",
                "graduate school",
                "department",
                "college",
                "campus",
            ]
        ):
            break
        if re.fullmatch(r"[A-Za-z0-9&.+ -]{2,24}", qualifier):
            text = text[: match.start()].rstrip()
        else:
            break
    text = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    text = re.sub(r"[,/]", " - ", text)
    text = re.sub(r"\s*-\s*", " - ", text)
    text = text.casefold()
    return " ".join(text.split())


def _status_key(value: Any) -> str:
    key = _text(value).strip().lower()
    key = re.sub(r"[\s-]+", "_", key)
    return key.strip("_") or "missing"


def _status_category(value: Any) -> str:
    key = _status_key(value)
    if key == "measured":
        return "measured"
    if key == "no_applications":
        return "assigned"
    if key == "ambiguous":
        return "ambiguous"
    return "na"


def _status_label(value: Any) -> str:
    key = _status_key(value)
    if key in _STATUS_LABELS:
        return _STATUS_LABELS[key]
    words = re.sub(r"[_-]+", " ", key).split()
    return " ".join(word[:1].upper() + word[1:] for word in words) or "N/A"


def _normalize_record(record: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Normalize canonical fields while preserving every extra field."""

    normalized = {str(key): _jsonable(value) for key, value in record.items()}
    normalized["country"] = _text(record.get("country"))
    normalized["university"] = _text(record.get("university"))
    normalized["year"] = _integer(record.get("year"))
    normalized["page"] = _integer(record.get("page"))
    normalized["row_index"] = _integer(record.get("row_index"))
    if normalized["row_index"] is None:
        normalized["row_index"] = index + 1
    normalized["status"] = _status_key(record.get("status"))

    score = _finite_float(record.get("score_percent"))
    fraction = _finite_float(record.get("bar_fraction"))
    if score is None and fraction is not None:
        score = fraction * 100.0
    if normalized["status"] == "no_applications":
        score = 100.0
        fraction = 1.0
        if not normalized.get("score_basis"):
            normalized["score_basis"] = "no_applications_assumed_full"
    elif _status_category(normalized["status"]) not in {"measured"}:
        # A stale numeric value on an unavailable/ambiguous row must not leak
        # into the report as if it were a usable score.
        score = None
        fraction = None
    normalized["score_percent"] = score
    normalized["bar_fraction"] = fraction

    normalized["raw_width_pt"] = _finite_float(record.get("raw_width_pt"))
    normalized["full_width_pt"] = _finite_float(record.get("full_width_pt"))
    normalized["calibration_id"] = _nullable_text(record.get("calibration_id"))
    normalized["bar_bbox"] = _jsonable(record.get("bar_bbox"))
    normalized["notes"] = _text(record.get("notes"))

    for field in _SOURCE_FIELDS:
        if field == "source_release_year":
            normalized[field] = _integer(record.get(field))
        else:
            normalized[field] = _nullable_text(record.get(field))
    normalized["row_id"] = _nullable_text(record.get("row_id"))
    normalized["score_basis"] = _nullable_text(record.get("score_basis"))
    return normalized


def _normalized_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [_normalize_record(record, index) for index, record in enumerate(records)]


def _score_value(record: Mapping[str, Any]) -> float | None:
    """Return the only statuses allowed to contribute to a mean."""

    if _status_category(record.get("status")) not in {"measured", "assigned"}:
        return None
    score = _finite_float(record.get("score_percent"))
    if score is None:
        return None
    # Extracted records are validated before reaching this module.  Clamping
    # here keeps hand-authored or legacy report inputs safe as well.
    return max(0.0, min(100.0, score))


def _score_for_width(score: float | None) -> float:
    if score is None:
        return 0.0
    return max(0.0, min(100.0, score))


def _display(value: Any, fallback: str = "—") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def _escaped(value: Any, fallback: str = "—") -> str:
    return html.escape(_display(value, fallback), quote=True)


def _compact_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (list, tuple, dict)):
        return _safe_json(value)
    return str(value)


_PROFILE_LEVELS = ("bachelor", "master")
_PROFILE_LEVEL_LABELS = {
    "bachelor": "Bachelor",
    "master": "Master",
    "unknown": "Unknown level",
}


def _safe_profile_url(value: Any) -> str | None:
    """Allow only absolute HTTP(S) profile links in generated markup."""

    if value is None:
        return None
    url = _text(value).strip()
    if not url or any(char in url for char in "\r\n\t"):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _profile_levels(profile: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    raw = profile.get("levels")
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, Iterable) and not isinstance(raw, (bytes, bytearray, Mapping)):
        for value in raw:
            key = _canonical_text(value)
            if key in _PROFILE_LEVELS and key not in values:
                values.append(key)
    return [level for level in _PROFILE_LEVELS if level in values]


def _profile_subjects(profile: Mapping[str, Any]) -> list[str]:
    raw = profile.get("subject_areas")
    if isinstance(raw, str):
        raw = [raw]
    values: list[str] = []
    if isinstance(raw, Iterable) and not isinstance(raw, (bytes, bytearray, Mapping)):
        for value in raw:
            text = _text(value).strip()
            if text and text not in values:
                values.append(text)
    return values


def _profile_links(value: Any) -> list[dict[str, str]]:
    """Normalize field links without allowing unsafe schemes into the UI."""

    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, bytearray, Mapping)):
        return []
    links: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        url = _safe_profile_url(item.get("url") or item.get("href"))
        if url is None:
            continue
        label = _text(item.get("label") or item.get("text") or url).strip() or url
        links.append({"label": label, "url": url})
    return links


def _profile_fields(profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = profile.get("fields")
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes, bytearray, Mapping)):
        return []
    fields: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        label = _text(item.get("label")).strip()
        text = _text(item.get("text")).strip()
        links = _profile_links(item.get("links"))
        if not label and not text and not links:
            continue
        fields.append({"label": label or "Additional information", "text": text, "links": links})
    return fields


def _profile_payload(profile: Mapping[str, Any], catalog_meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the small, escaped-at-render-time profile contract embedded in HTML."""

    levels = _profile_levels(profile)
    subjects = _profile_subjects(profile)
    source_url = _safe_profile_url(profile.get("source_url"))
    details_url = _safe_profile_url(profile.get("details_url"))
    retrieved_at = _text(profile.get("retrieved_at") or (catalog_meta or {}).get("retrieved_at")).strip()
    return {
        "institution_id": _text(profile.get("institution_id")),
        "relation_id": _text(profile.get("relation_id")),
        "name": _text(profile.get("name")),
        "country": _text(profile.get("country")),
        "city": _text(profile.get("city")).strip(),
        "levels": levels,
        "subject_areas": subjects,
        "fields": _profile_fields(profile),
        "source_url": source_url,
        "details_url": details_url,
        "retrieved_at": retrieved_at or None,
    }


def _profile_catalog_meta(catalog: Any) -> Mapping[str, Any]:
    return catalog if isinstance(catalog, Mapping) else {}


def _profile_records(catalog: Any) -> list[Mapping[str, Any]]:
    if isinstance(catalog, Mapping):
        catalog = catalog.get("profiles")
    if not isinstance(catalog, Iterable) or isinstance(catalog, (str, bytes, bytearray, Mapping)):
        return []
    return [item for item in catalog if isinstance(item, Mapping)]


def _profile_group_defaults(group: Mapping[str, Any], catalog_meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Normalize helper output and make old/no-profile groups explicit."""

    result = dict(group)
    profiles = [
        _profile_payload(profile, catalog_meta)
        for profile in (group.get("profiles") or [])
        if isinstance(profile, Mapping)
    ]
    result["profiles"] = profiles
    levels: list[str] = []
    subjects: list[str] = []
    cities: list[str] = []
    for profile in profiles:
        for level in profile.get("levels") or []:
            if level not in levels:
                levels.append(level)
        for subject in profile.get("subject_areas") or []:
            if subject not in subjects:
                subjects.append(subject)
        city = _text(profile.get("city")).strip()
        if city and city not in cities:
            cities.append(city)
    # The helper's unions are authoritative when supplied, but sanitizing them
    # here keeps hand-authored catalogs and older helper versions compatible.
    result["profile_levels"] = [
        level for level in _PROFILE_LEVELS
        if level in set(result.get("profile_levels") or levels)
    ]
    result["profile_subjects"] = [
        _text(value).strip() for value in (result.get("profile_subjects") or subjects)
        if _text(value).strip()
    ]
    result["profile_cities"] = [
        _text(value).strip() for value in (result.get("profile_cities") or cities)
        if _text(value).strip()
    ]
    search_parts = [_text(result.get("search_text"))]
    for profile in profiles:
        search_parts.extend(
            [
                profile.get("name", ""),
                profile.get("country", ""),
                profile.get("city", ""),
                " ".join(profile.get("levels") or []),
                " ".join(profile.get("subject_areas") or []),
            ]
        )
        for field in profile.get("fields") or []:
            search_parts.extend([field.get("label", ""), field.get("text", "")])
            search_parts.extend(link.get("label", "") for link in field.get("links") or [])
    result["search_text"] = " ".join(_text(value) for value in search_parts).casefold()
    return result


def _attach_profile_catalog(groups: Sequence[Mapping[str, Any]], catalog: Any) -> list[dict[str, Any]]:
    """Attach profile relations while keeping the renderer tolerant of no catalog."""

    if catalog is None:
        return [_profile_group_defaults(group) for group in groups]
    attached = attach_profiles(groups, catalog)
    meta = _profile_catalog_meta(catalog)
    return [_profile_group_defaults(group, meta) for group in attached]


def _score_class(record: Mapping[str, Any], score: float | None) -> str:
    category = _status_category(record.get("status"))
    if category == "assigned":
        return "status-assigned"
    if category == "ambiguous":
        return "status-ambiguous"
    if score is None:
        return "status-na"
    return "status-measured"


def _infer_release_year(filename: Any) -> int | None:
    """Infer a release label only from an explicitly year-like filename."""

    text = _text(filename)
    matches = re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text)
    return int(matches[-1]) if matches else None


def _release_year(record: Mapping[str, Any]) -> int | None:
    explicit = _integer(record.get("source_release_year"))
    return explicit if explicit is not None else _infer_release_year(record.get("source_filename"))


def _source_document(record: Mapping[str, Any]) -> str:
    return (
        _text(record.get("source_filename"))
        or _text(record.get("source_document_id"))
        or _text(record.get("source_sha256"))
        or "Source document not recorded"
    )


def _source_label(record: Mapping[str, Any]) -> str:
    release = _release_year(record)
    document = _source_document(record)
    return f"Release {release} · {document}" if release is not None else document


def _source_cell(record: Mapping[str, Any]) -> str:
    release = _release_year(record)
    document = _source_document(record)
    title = _source_label(record)
    release_html = (
        f'<span class="source-release">Release {release}</span>'
        if release is not None
        else '<span class="source-release">Release not recorded</span>'
    )
    return (
        f'<span class="source-cell-inner" title="{html.escape(title, quote=True)}">'
        f"{release_html}<span class=\"source-document\">"
        f"{html.escape(document, quote=True)}</span></span>"
    )


def _calibration_container_source(calibration: Any) -> Mapping[str, Any]:
    if isinstance(calibration, Mapping):
        source = calibration.get("source")
        if isinstance(source, Mapping):
            return source
        return calibration
    return {}


def _calibration_entries(calibration: Any) -> list[dict[str, Any]]:
    """Accept one calibration, a list, or a multi-calibration envelope."""

    if calibration is None:
        return []
    if isinstance(calibration, Mapping):
        if not calibration:
            return []
        raw_entries: Any
        if "calibrations" in calibration:
            raw_entries = calibration.get("calibrations")
        elif isinstance(calibration.get("calibration"), Mapping):
            raw_entries = [calibration["calibration"]]
        else:
            looks_like_catalog = (
                not (_CALIBRATION_KEYS | set(_SOURCE_FIELDS) | {"source", "calibrations"})
                & set(calibration)
                and calibration
                and all(isinstance(value, Mapping) for value in calibration.values())
            )
            if looks_like_catalog:
                raw_entries = []
                for key, value in calibration.items():
                    item = dict(value)
                    item.setdefault("calibration_id", key)
                    raw_entries.append(item)
            else:
                raw_entries = [calibration]
    elif isinstance(calibration, Sequence) and not isinstance(calibration, (str, bytes)):
        raw_entries = calibration
    else:
        raw_entries = []
    if raw_entries is None:
        raw_entries = []
    if isinstance(raw_entries, Mapping):
        raw_entries = [
            {**dict(value), **({"calibration_id": key} if "calibration_id" not in value else {})}
            for key, value in raw_entries.items()
            if isinstance(value, Mapping)
        ]
    elif isinstance(raw_entries, (str, bytes)) or not isinstance(raw_entries, Iterable):
        raw_entries = []
    return [dict(item) for item in raw_entries if isinstance(item, Mapping)]


def _calibration_meta(
    entry: Mapping[str, Any],
    fallback_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    fallback_source = fallback_source or {}
    nested = entry.get("source")
    nested_source = nested if isinstance(nested, Mapping) else {}
    result: dict[str, Any] = {}
    for field in _SOURCE_FIELDS:
        value = entry.get(field)
        if value in (None, ""):
            value = nested_source.get(field)
        if value in (None, ""):
            value = fallback_source.get(field)
        if field == "source_release_year":
            value = _integer(value)
        else:
            value = _nullable_text(value)
        if value is not None:
            result[field] = value
    release = entry.get("release_year")
    if "source_release_year" not in result and release is not None:
        result["source_release_year"] = _integer(release)
    if "source_release_year" not in result:
        inferred = _infer_release_year(result.get("source_filename"))
        if inferred is not None:
            result["source_release_year"] = inferred
    calibration_id = (
        entry.get("calibration_id")
        or entry.get("id")
        or nested_source.get("calibration_id")
        or fallback_source.get("calibration_id")
    )
    if calibration_id not in (None, ""):
        result["calibration_id"] = _text(calibration_id)
    return result


def _calibration_for_record(
    record: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
    fallback_source: Mapping[str, Any] | None = None,
) -> Mapping[str, Any] | None:
    record_calibration = _text(record.get("calibration_id"))
    record_document = _text(record.get("source_document_id"))
    record_hash = _text(record.get("source_sha256"))
    for entry in entries:
        meta = _calibration_meta(entry, fallback_source)
        if record_calibration and meta.get("calibration_id") == record_calibration:
            return entry
    for entry in entries:
        meta = _calibration_meta(entry, fallback_source)
        if record_document and meta.get("source_document_id") == record_document:
            return entry
        if record_hash and meta.get("source_sha256") == record_hash:
            return entry
    return entries[0] if len(entries) == 1 else None


def _width_for_year(entry: Mapping[str, Any], year: Any) -> float | None:
    maps = entry.get("full_width_pt_by_year") or entry.get("full_width_by_year")
    if isinstance(maps, Mapping):
        for key in (year, _text(year)):
            if key in maps:
                return _finite_float(maps[key])
    return _finite_float(entry.get("full_width_pt"))


def _prepare_records(
    records: Iterable[Mapping[str, Any]],
    calibration: Any,
) -> list[dict[str, Any]]:
    """Normalize records and fill only missing provenance from calibration."""

    normalized = _normalized_records(records)
    entries = _calibration_entries(calibration)
    fallback = _calibration_container_source(calibration)
    prepared: list[dict[str, Any]] = []
    for record in normalized:
        result = dict(record)
        entry = _calibration_for_record(result, entries, fallback)
        meta = _calibration_meta(entry, fallback) if entry is not None else dict(
            _calibration_meta(fallback, fallback)
        )
        for field in _SOURCE_FIELDS:
            if result.get(field) in (None, "") and meta.get(field) is not None:
                result[field] = meta[field]
        if result.get("source_release_year") is None:
            inferred = _infer_release_year(result.get("source_filename"))
            if inferred is not None:
                result["source_release_year"] = inferred
        if (
            result.get("calibration_id") is None
            and _score_value(result) is not None
            and meta.get("calibration_id") is not None
        ):
            result["calibration_id"] = meta["calibration_id"]
        if result.get("full_width_pt") is None and entry is not None:
            width = _width_for_year(entry, result.get("year"))
            if width is not None:
                result["full_width_pt"] = width
        prepared.append(result)
    return prepared


def _record_group_key(record: Mapping[str, Any], index: int) -> tuple[str, Any]:
    university = _university_identity(record.get("university"))
    if not university:
        university = f"__unnamed_{index}"
    year = record.get("year")
    if year is None:
        return university, _text(record.get("row_id")) or f"row-{index}"
    return university, year


def _record_precedence(record: Mapping[str, Any]) -> tuple[int, int]:
    release = _release_year(record)
    # A later PDF release is the current observation.  ``is_current`` is
    # accepted for callers that already queried a current-observation view.
    return (release if release is not None else -1, int(bool(record.get("is_current"))))


def _deduplicate_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep one current observation per university/year.

    Storage uses the same release-year precedence.  Keeping this guard in the
    renderer means a report is still correct when callers concatenate exports
    from multiple source PDFs directly.
    """

    normalized = _normalized_records(records)
    chosen: dict[tuple[str, Any], dict[str, Any]] = {}
    for index, record in enumerate(normalized):
        key = _record_group_key(record, index)
        existing = chosen.get(key)
        if existing is None or _record_precedence(record) > _record_precedence(existing):
            chosen[key] = record
    return list(chosen.values())


def _source_descriptor(record: Mapping[str, Any]) -> dict[str, Any]:
    release = _release_year(record)
    document_id = _text(record.get("source_document_id"))
    filename = _source_document(record)
    source_hash = _text(record.get("source_sha256"))
    key = "|".join((str(release or ""), document_id, filename, source_hash))
    return {
        "key": key,
        "release_year": release,
        "document_id": document_id or None,
        "filename": filename,
        "sha256": source_hash or None,
    }


def _group_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Group current annual entries into exactly one summary per university."""

    current = _deduplicate_records(records)
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in current:
        key = _university_identity(record.get("university"))
        if not key:
            key = f"__unnamed_{len(order)}"
        if key not in groups:
            groups[key] = {
                "country": _text(record.get("country")),
                "university": _text(record.get("university")),
                "records": [],
            }
            order.append(key)
        group = groups[key]
        group["records"].append(record)
        if not group["country"] and _text(record.get("country")):
            group["country"] = _text(record.get("country"))

    result: list[dict[str, Any]] = []
    for key in order:
        group = groups[key]
        annual = sorted(
            group["records"],
            key=lambda item: (
                item.get("year") is None,
                -(item.get("year") or 0),
                item.get("row_index") or 0,
            ),
        )
        preferred = max(
            annual,
            key=lambda item: (
                _release_year(item) if _release_year(item) is not None else -1,
                item.get("year") if item.get("year") is not None else -1,
            ),
        )
        group["university"] = _text(preferred.get("university")) or group["university"]
        group["country"] = _text(preferred.get("country")) or group["country"]
        scores = [
            score for score in (_score_value(item) for item in annual) if score is not None
        ]
        categories: list[str] = []
        source_documents: list[dict[str, Any]] = []
        source_keys: set[str] = set()
        calibration_ids: list[str] = []
        calibration_seen: set[str] = set()
        search_parts = [group["country"], group["university"]]
        for item in annual:
            category = _status_category(item.get("status"))
            if category not in categories:
                categories.append(category)
            descriptor = _source_descriptor(item)
            if descriptor["key"] not in source_keys:
                source_keys.add(descriptor["key"])
                source_documents.append(descriptor)
            calibration_id = _text(item.get("calibration_id"))
            if calibration_id and calibration_id not in calibration_seen:
                calibration_seen.add(calibration_id)
                calibration_ids.append(calibration_id)
            search_parts.extend(
                [
                    _text(item.get("year")),
                    _text(item.get("status")),
                    _text(item.get("notes")),
                    _text(item.get("source_filename")),
                    _text(item.get("source_document_id")),
                    _text(_release_year(item)),
                ]
            )
        source_documents.sort(
            key=lambda item: (
                item["release_year"] is None,
                -(item["release_year"] or 0),
                _canonical_text(item["filename"]),
            )
        )
        group["records"] = annual
        group["categories"] = categories
        group["scored_count"] = len(scores)
        group["total_count"] = len(annual)
        group["mean_score"] = sum(scores) / len(scores) if scores else None
        group["source_documents"] = source_documents
        group["source_count"] = len(source_documents)
        group["calibration_ids"] = calibration_ids
        group["search_text"] = " ".join(search_parts).casefold()
        roster_flags = [
            bool(item.get("is_latest_roster"))
            for item in annual
            if item.get("is_latest_roster") is not None
        ]
        group["is_latest_roster"] = any(roster_flags) if roster_flags else True
        group["latest_release_year"] = max(
            (
                release
                for release in (_release_year(item) for item in current)
                if release is not None
            ),
            default=None,
        )
        result.append(group)
    return result


def _render_details(
    record: Mapping[str, Any],
    display_index: int,
    calibration: Any = None,
) -> str:
    """Render the third-level annual measurement/provenance disclosure."""

    raw_record = {
        key: value for key, value in record.items() if not str(key).startswith("_")
    }
    raw = html.escape(_safe_json(raw_record, indent=2), quote=False)
    status_key = _status_key(record.get("status"))
    if status_key == "no_applications":
        formula = (
            "Assigned 100% under the explicit no-applications rule; the status "
            "box is not a numeric bar measurement."
        )
    elif status_key == "measured":
        formula = (
            "Normalized historical selection-ratio indicator = raw bar width ÷ "
            "this observation's calibrated full width × 100, under a linear "
            "bar-width model."
        )
    else:
        formula = "No numeric score is assigned to this unavailable or unresolved observation."
    full_width = record.get("full_width_pt")
    full_width_text = f"{full_width} pt" if full_width is not None else "—"
    raw_width = record.get("raw_width_pt")
    raw_width_text = f"{raw_width} pt" if raw_width is not None else "—"
    release = _release_year(record)
    release_text = str(release) if release is not None else "Not recorded"
    source_document = _source_document(record)
    return f"""
      <section class="detail-panel" aria-label="Measurement, calibration, and source provenance">
        <div class="detail-heading">
          <div>
            <span class="detail-kicker">Measurement / calibration / source provenance</span>
            <h3>Annual observation {display_index + 1}</h3>
          </div>
          <a href="calibration.json">Open calibration JSON</a>
        </div>
        <dl class="detail-grid">
          <div><dt>Observation year</dt><dd>{_escaped(record.get("year"))}</dd></div>
          <div><dt>Source release</dt><dd>{html.escape(release_text, quote=True)}</dd></div>
          <div><dt>Source document</dt><dd class="breakable">{html.escape(source_document, quote=True)}</dd></div>
          <div><dt>Source document ID</dt><dd class="breakable">{_escaped(record.get("source_document_id"), "Not recorded")}</dd></div>
          <div><dt>Source SHA-256</dt><dd class="breakable">{_escaped(record.get("source_sha256"), "Not recorded")}</dd></div>
          <div><dt>PDF page / row</dt><dd>{_escaped(record.get("page"))} / {_escaped(record.get("row_index"))}</dd></div>
          <div><dt>Raw status</dt><dd>{html.escape(_status_label(status_key), quote=True)} <span class="technical-value">({html.escape(status_key, quote=True)})</span></dd></div>
          <div><dt>Raw bar width</dt><dd>{html.escape(raw_width_text, quote=True)}</dd></div>
          <div><dt>Calibrated full width</dt><dd>{html.escape(full_width_text, quote=True)}</dd></div>
          <div><dt>Calibration ID</dt><dd class="breakable">{_escaped(record.get("calibration_id"), "Not recorded")}</dd></div>
          <div><dt>Score basis</dt><dd class="breakable">{_escaped(record.get("score_basis"), "Not recorded")}</dd></div>
          <div><dt>Bar bounding box</dt><dd class="breakable">{html.escape(_compact_value(record.get("bar_bbox")), quote=True)}</dd></div>
          <div><dt>Status-box geometry</dt><dd class="breakable">{html.escape(_compact_value(record.get("status_box_bbox")), quote=True)}</dd></div>
        </dl>
        <p class="detail-note"><strong>Notes:</strong> {_escaped(record.get("notes"), "No notes")}</p>
        <p class="detail-formula">{html.escape(formula, quote=True)}</p>
        <details class="raw-record-details">
          <summary>Show complete row data</summary>
          <pre class="raw-record">{raw}</pre>
        </details>
      </section>
    """


def _render_row(record: Mapping[str, Any], index: int) -> str:
    """Render a legacy flat row for callers that still use this helper."""

    normalized = _normalize_record(record, index)
    score = _score_value(normalized)
    score_label = f"{score:.1f}%" if score is not None else "N/A"
    category = _status_category(normalized.get("status"))
    detail_id = f"detail-{index}"
    track_label = (
        f"{score_label} of a full reference bar"
        if score is not None
        else "N/A; bar not measured"
    )
    return f"""
      <tr class="data-row legacy-data-row" data-record-index="{index}">
        <td class="university-cell"><span class="university-name">{_escaped(normalized.get("university"), "Unnamed university")}</span></td>
        <td>{_escaped(normalized.get("country"))}</td>
        <td class="numeric">{_escaped(normalized.get("year"))}</td>
        <td><span class="status-pill {_score_class(normalized, score)}">{html.escape(_CATEGORY_LABELS[category], quote=True)}</span></td>
        <td class="score-cell"><div class="score-stack"><div class="score-line"><span class="score-value">{score_label}</span></div><div class="score-track{' is-empty' if score is None else ''}" aria-label="{html.escape(track_label, quote=True)}"><span class="score-fill" style="width: {_score_for_width(score):.4f}%"></span></div></div></td>
        <td class="source-cell">{_source_cell(normalized)}</td>
        <td class="details-cell"><button class="detail-toggle" type="button" aria-expanded="false" aria-controls="{detail_id}">View details</button></td>
      </tr>
      <tr id="{detail_id}" class="detail-row" hidden><td colspan="7">{_render_details(normalized, index)}</td></tr>
    """


def _render_status_pills(categories: Iterable[str]) -> str:
    return "".join(
        f'<span class="status-pill status-{html.escape(category, quote=True)}">'
        f"{html.escape(_CATEGORY_LABELS.get(category, category.title()), quote=True)}</span>"
        for category in categories
    )


def _render_score_display(
    normalized: Mapping[str, Any], score: float | None, track_text: str
) -> str:
    score_basis = normalized.get("score_basis")
    tier = normalized.get("tier")
    category = _status_category(normalized.get("status"))

    if category == "assigned":
        return (
            f'<div class="score-stack">'
            f'<div class="score-line"><span class="score-value">100%</span>'
            f'<span class="basis-badge basis-assigned" tabindex="0" role="note" title="No applicants (100% placement): TUM recorded that places exceeded demand; all applicants secured placement.">No applicants'
            f'<span class="basis-tooltip" role="tooltip"><strong class="basis-tooltip-title">No applicants (100% placement)</strong><span class="basis-tooltip-body">TUM recorded that places exceeded applicant demand; all applicants secured placement.</span></span></span></div>'
            f'<div class="score-track" aria-label="{html.escape(track_text, quote=True)}">'
            f'<span class="score-fill" style="width: 100%"></span></div></div>'
        )

    if score is None or category == "na":
        return (
            f'<div class="score-stack">'
            f'<div class="score-line"><span class="score-na">—</span></div>'
            f'<div class="score-track is-empty" aria-label="{html.escape(track_text, quote=True)}"></div></div>'
        )

    # 1. 4-tier Traffic Light Quartile
    if score_basis == "traffic_light" or tier is not None:
        if tier is None:
            if score <= 25.0:
                tier = 1
            elif score <= 50.0:
                tier = 2
            elif score <= 75.0:
                tier = 3
            else:
                tier = 4
        range_label = normalized.get("score_range") or (
            "≤ 25%" if tier == 1 else "26–50%" if tier == 2 else "51–75%" if tier == 3 else "≥ 76%"
        )
        tier_title = f"Quartile tier {tier}: {range_label}"
        b1 = " filled" if tier >= 1 else ""
        b2 = " filled" if tier >= 2 else ""
        b3 = " filled" if tier >= 3 else ""
        b4 = " filled" if tier >= 4 else ""
        return (
            f'<div class="score-stack">'
            f'<div class="score-line">'
            f'<span class="traffic-light-pill tier-{tier}" title="{html.escape(tier_title, quote=True)}">'
            f'<span class="tier-blocks" aria-hidden="true">'
            f'<span class="tier-block{b1}"></span><span class="tier-block{b2}"></span>'
            f'<span class="tier-block{b3}"></span><span class="tier-block{b4}"></span></span>'
            f'<span class="tier-label">{html.escape(range_label, quote=True)}</span></span>'
            f'<span class="basis-badge basis-tier" tabindex="0" role="note" title="Quartile Tier (Releases 2018–2022): 4-tier placement rating published in historical TUM tables (≤25%, 26–50%, 51–75%, ≥76%).">Quartile'
            f'<span class="basis-tooltip" role="tooltip"><strong class="basis-tooltip-title">Quartile Tier (Releases 2018–2022)</strong><span class="basis-tooltip-body">4-tier placement rating from historical TUM tables: ≤25% (Red), 26–50% (Yellow), 51–75% (Light Green), or ≥76% (Dark Green).</span></span></span></div>'
            f'<div class="score-track is-tier" aria-label="{html.escape(track_text, quote=True)}">'
            f'<span class="score-fill tier-{tier}-fill" style="width: {_score_for_width(score):.4f}%"></span></div></div>'
        )

    # 2. Exact Decimal Percentage
    if score_basis == "exact_percentage":
        return (
            f'<div class="score-stack">'
            f'<div class="score-line"><span class="score-value">{score:.1f}%</span>'
            f'<span class="basis-badge basis-exact" tabindex="0" role="note" title="Exact % (Release 2017): Exact decimal admission percentage officially published in TUM\'s 2016/17 exchange statistics release.">Exact %'
            f'<span class="basis-tooltip" role="tooltip"><strong class="basis-tooltip-title">Exact Published % (Release 2017)</strong><span class="basis-tooltip-body">Exact decimal admission percentage officially published in TUM’s 2016/17 exchange statistics release.</span></span></span></div>'
            f'<div class="score-track is-exact" aria-label="{html.escape(track_text, quote=True)}">'
            f'<span class="score-fill is-exact-fill" style="width: {_score_for_width(score):.4f}%"></span></div></div>'
        )

    # 3. Continuous Vector / Raster Bar
    return (
        f'<div class="score-stack">'
        f'<div class="score-line"><span class="score-value">{score:.1f}%</span>'
        f'<span class="basis-badge basis-bar" tabindex="0" role="note" title="Visual Bar (Releases 2025–2027): Continuous placement rate measured geometrically from TUM\'s vector/raster bar charts against calibrated anchors.">Visual Bar'
        f'<span class="basis-tooltip" role="tooltip"><strong class="basis-tooltip-title">Visual Bar (Releases 2025–2027)</strong><span class="basis-tooltip-body">Continuous placement rate measured geometrically from TUM’s vector/raster bar charts against calibrated anchors.</span></span></span></div>'
        f'<div class="score-track" aria-label="{html.escape(track_text, quote=True)}">'
        f'<span class="score-fill" style="width: {_score_for_width(score):.4f}%"></span></div></div>'
    )


def _render_annual_row(
    record: Mapping[str, Any], group_index: int, annual_index: int
) -> str:
    normalized = _normalize_record(record, annual_index)
    score = _score_value(normalized)
    score_label = f"{score:.1f}%" if score is not None else "N/A"
    category = _status_category(normalized.get("status"))
    status_key = _status_key(normalized.get("status"))
    year = normalized.get("year")
    if category == "assigned":
        status_html = '<span class="status-pill status-assigned" title="No applications submitted this year">100% (No applicants)</span>'
    elif category == "measured":
        status_html = '<span class="status-pill status-measured">Recorded</span>'
    elif category == "ambiguous":
        status_html = '<span class="status-pill status-ambiguous">Ambiguous</span>'
    else:
        status_label = _STATUS_LABELS.get(status_key, "No active exchange")
        status_html = f'<span class="status-pill status-na">{html.escape(status_label, quote=True)}</span>'

    track_text = (
        f"{score_label} acceptance rate"
        if score is not None
        else "N/A; no active exchange"
    )
    release = _release_year(normalized)
    release_text = f"Release {release}" if release is not None else "—"
    score_content = _render_score_display(normalized, score, track_text)
    return f"""
          <tr class="annual-data-row" data-annual-index="{annual_index}" data-year="{year}">
            <td class="numeric" data-label="Year">{_escaped(year)}</td>
            <td class="score-cell" data-label="Acceptance rate">{score_content}</td>
            <td data-label="Status">{status_html}</td>
            <td class="source-cell" data-label="Source"><span class="source-release">{html.escape(release_text, quote=True)}</span></td>
          </tr>
    """


def _source_summary(group: Mapping[str, Any]) -> str:
    source_documents = group.get("source_documents") or []
    count = len(source_documents)
    if count == 0:
        return '<span class="source-release">Source not recorded</span>'
    label = f"{count} source release{'s' if count != 1 else ''}"
    documents = "; ".join(
        _source_label(
            {
                "source_release_year": item.get("release_year"),
                "source_filename": item.get("filename"),
                "source_document_id": item.get("document_id"),
            }
        )
        for item in source_documents
    )
    return (
        f'<span class="source-release">{html.escape(label, quote=True)}</span>'
        f'<span class="source-document" title="{html.escape(documents, quote=True)}">'
        f"See annual rows</span>"
    )


def _render_profile_badges(
    levels: Iterable[str], *, compact: bool = False
) -> str:
    values = list(levels)
    if not values:
        return '<span class="profile-badge profile-badge-unknown">Unknown level</span>'
    return "".join(
        f'<span class="profile-badge profile-badge-{html.escape(level, quote=True)}">'
        f'{html.escape(_PROFILE_LEVEL_LABELS.get(level, level.title()), quote=True)}</span>'
        for level in values
    )


def _render_profile_summary(group: Mapping[str, Any]) -> str:
    profiles = group.get("profiles") or []
    levels = group.get("profile_levels") or []
    cities = group.get("profile_cities") or []
    pieces: list[str] = []
    if profiles:
        pieces.append(_render_profile_badges(levels, compact=True))
        if cities:
            pieces.append(
                f'<span class="profile-city">{html.escape(", ".join(map(str, cities)), quote=True)}</span>'
            )
        if group.get("profile_subjects"):
            pieces.append(
                f'<span class="profile-subject-count">{len(group["profile_subjects"])} subject areas</span>'
            )
    else:
        pieces.append('<span class="profile-missing">No profile data</span>')
    return '<span class="profile-summary">' + "".join(pieces) + "</span>"


def _render_profile_text(value: Any) -> str:
    text = _text(value).strip()
    if not text:
        return '<span class="profile-empty">No text supplied</span>'
    return html.escape(text.replace("||", "\n"), quote=True).replace("\n", "<br>")


def _render_profile_links(links: Iterable[Mapping[str, Any]]) -> str:
    rendered: list[str] = []
    for link in links:
        url = _safe_profile_url(link.get("url")) if isinstance(link, Mapping) else None
        if url is None:
            continue
        label = _text(link.get("label") or url).strip() or url
        rendered.append(
            f'<a href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer">{html.escape(label, quote=True)}</a>'
        )
    if not rendered:
        return ""
    return '<div class="profile-links" aria-label="Related links">' + "".join(rendered) + "</div>"


def _render_profile_card(profile: Mapping[str, Any], group_index: int, profile_index: int) -> str:
    name = _text(profile.get("name")).strip() or "MoveON relation"
    city = _text(profile.get("city")).strip()
    country = _text(profile.get("country")).strip()
    location = " · ".join(value for value in (city, country) if value)
    levels = profile.get("levels") or []
    subjects = profile.get("subject_areas") or []
    relation_id = _text(profile.get("relation_id")).strip()
    relation_meta = []
    if location:
        relation_meta.append(html.escape(location, quote=True))
    summary_meta = " · ".join(relation_meta)
    fields = profile.get("fields") or []
    available_labels = {field.get("label") for field in fields if isinstance(field, Mapping)}
    preview_topics = [short for label, short in (
        ("Language Requirements", "languages"),
        ("Application Deadlines at Host University", "deadlines"),
        ("Semester Dates", "semester dates"),
        ("Subject Area", "eligible subjects"),
    ) if label in available_labels][:3]
    preview = ", ".join(preview_topics)
    expand_hint = f"Show {len(fields)} information sections"
    if preview:
        expand_hint += f": {preview} and more"
    field_markup: list[str] = []
    for field in fields:
        if not isinstance(field, Mapping):
            continue
        label = _text(field.get("label")).strip() or "Additional information"
        links = _render_profile_links(field.get("links") or [])
        field_markup.append(
            f'<div class="profile-field"><dt>{html.escape(label, quote=True)}</dt>'
            f'<dd>{_render_profile_text(field.get("text"))}{links}</dd></div>'
        )
    if not field_markup:
        field_markup.append(
            '<div class="profile-field"><dt>Profile fields</dt>'
            '<dd><span class="profile-empty">No structured details supplied</span></dd></div>'
        )
    direct_links: list[dict[str, str]] = []
    source_url = _safe_profile_url(profile.get("source_url"))
    details_url = _safe_profile_url(profile.get("details_url"))
    if source_url:
        direct_links.append({"label": "Official TUM directory", "url": source_url})
    if details_url and details_url != source_url:
        direct_links.append({"label": "Student experience reports", "url": details_url})
    direct_markup = _render_profile_links(direct_links)
    retrieved = _text(profile.get("retrieved_at")).strip()
    retrieved_markup = (
        f'<span class="profile-retrieved">Checked {html.escape(retrieved[:10], quote=True)}</span>'
        if retrieved
        else ""
    )
    subject_markup = (
        '<span class="profile-subjects">'
        + f"{len(subjects)} eligible subject areas, listed below"
        + "</span>"
        if subjects
        else '<span class="profile-subjects profile-empty">Subject area not recorded</span>'
    )
    return f'''
        <details class="profile-relation" id="profile-{group_index}-{profile_index}">
          <summary>
            <span class="profile-relation-title"><strong>{html.escape(name, quote=True)}</strong>
            <span class="profile-relation-meta">{summary_meta}</span><span class="profile-open-hint"><span class="profile-hint-closed">{html.escape(expand_hint, quote=True)}</span><span class="profile-hint-open">Hide detailed information</span></span></span>
            <span class="profile-relation-badges">{_render_profile_badges(levels)}</span>
          </summary>
          <div class="profile-relation-body">
            <div class="profile-relation-context">{subject_markup}{retrieved_markup}</div>
            <dl class="profile-fields">{"".join(field_markup)}</dl>
            {direct_markup}
          </div>
        </details>
    '''


def _render_profile_panel(group: Mapping[str, Any], group_index: int) -> str:
    profiles = group.get("profiles") or []
    if not profiles:
        return '''
          <section class="profile-panel profile-panel-empty" aria-label="University profile">
            <div class="profile-panel-heading">
              <div><span class="panel-eyebrow">PARTNER PROFILE &amp; REQUIREMENTS</span><h3>No profile data available</h3></div>
            </div>
            <p class="profile-empty-copy">This institution is retained in the historical comparison, but no structured MoveON profile is available in the current catalogue.</p>
          </section>
        '''
    relation_label = "relation" if len(profiles) == 1 else "relations"
    subject_count = len(group.get("profile_subjects") or [])
    subject_note = f" · {subject_count} subject areas" if subject_count else ""
    cards = "".join(
        _render_profile_card(profile, group_index, profile_index)
        for profile_index, profile in enumerate(profiles)
    )
    return f'''
        <section class="profile-panel" aria-label="University profile">
          <div class="profile-panel-heading">
            <div><span class="panel-eyebrow">PARTNER PROFILE &amp; REQUIREMENTS</span><h3>TUM partner details</h3></div>
            <span class="profile-panel-meta">{len(profiles)} {relation_label}{subject_note}</span>
          </div>
          <div class="profile-relations">{cards}</div>
        </section>
    '''


def _render_university_row(group: Mapping[str, Any], group_index: int) -> str:
    score = group.get("mean_score")
    score_label = f"{score:.1f}%" if score is not None else "N/A"
    score_unit = "avg acceptance" if score is not None else "no recorded data"
    detail_id = f"university-detail-{group_index}"
    scored = int(group.get("scored_count") or 0)
    total = int(group.get("total_count") or 0)
    records = list(group.get("records") or [])
    modern_records = [r for r in records if (r.get("year") or 0) >= 2022]
    historical_records = [r for r in records if (r.get("year") or 0) < 2022]

    if modern_records:
        modern_rows = "".join(
            _render_annual_row(record, group_index, annual_index)
            for annual_index, record in enumerate(modern_records)
        )
    else:
        modern_rows = ""

    if historical_records:
        hist_offset = len(modern_records)
        historical_rows = "".join(
            _render_annual_row(record, group_index, hist_offset + idx)
            for idx, record in enumerate(historical_records)
        )
        archive_html = f"""
            <tr class="historical-archive-row">
              <td colspan="4">
                <details class="historical-archive-disclosure">
                  <summary class="archive-summary-button">
                    <span class="archive-badge">Archive</span>
                    <span class="archive-title">Older historical records (2017–2021, {len(historical_records)} years)</span>
                    <span class="archive-chevron" aria-hidden="true">▾</span>
                  </summary>
                  <table class="annual-table archive-table">
                    <tbody>
                      {historical_rows}
                    </tbody>
                  </table>
                </details>
              </td>
            </tr>
        """
    else:
        archive_html = ""

    if records:
        annual_content = (
            '<table class="annual-table"><thead><tr><th scope="col">Year</th>'
            '<th scope="col">Acceptance rate</th><th scope="col">Status</th>'
            '<th scope="col">Source</th></tr></thead><tbody>'
            + modern_rows
            + archive_html
            + "</tbody></table>"
        )
    else:
        annual_content = (
            '<div class="historical-empty"><strong>No historical data</strong>'
            '<p>This institution is present in the current partner catalogue, but no historical application records are available.</p></div>'
        )
    roster_note = ""
    is_current = bool(group.get("is_latest_roster", True))
    if not is_current:
        latest_release = group.get("latest_release_year")
        label = (
            f"Not listed in release {latest_release}"
            if latest_release is not None
            else "Not listed in the latest release"
        )
        roster_note = f'<span class="roster-note">{html.escape(label, quote=True)}</span>'
    hidden_attr = "" if is_current else " hidden"
    return f"""
      <tr class="data-row university-row" data-group-index="{group_index}"{hidden_attr}>
        <td class="university-cell" data-label="University"><button class="university-toggle" type="button" aria-expanded="false" aria-controls="{detail_id}"><span><span class="university-name">{_escaped(group.get("university"), "Unnamed university")}</span>{roster_note}{_render_profile_summary(group)}</span><span class="expand-label">View profile &amp; history</span></button></td>
        <td data-label="Country">{_escaped(group.get("country"))}</td>
        <td class="numeric years-cell" data-label="Years recorded"><strong>{scored} of {total}</strong><span>years recorded</span></td>
        <td class="score-cell" data-label="Average acceptance"><div class="score-stack"><div class="score-line"><span class="score-value">{score_label}</span><span class="score-unit">{score_unit}</span></div><div class="score-track{' is-empty' if score is None else ''}" aria-label="{html.escape((score_label + ' average acceptance') if score is not None else 'N/A; no recorded data', quote=True)}"><span class="score-fill" style="width: {_score_for_width(score):.4f}%"></span></div></div></td>
        <td class="details-cell" data-label="Profile and history"><button class="detail-toggle university-detail-toggle" type="button" aria-expanded="false" aria-controls="{detail_id}">View profile &amp; history</button></td>
      </tr>
      <tr id="{detail_id}" class="university-detail-row" data-group-detail="{group_index}" hidden><td colspan="5"><div class="detail-card-shell">{_render_profile_panel(group, group_index)}<section class="annual-panel" aria-label="Historical annual data"><div class="annual-heading"><div><span class="panel-eyebrow">HISTORICAL ADMISSION DATA</span><h3>Historical acceptance rates by year</h3></div><span class="annual-count-badge">{scored} of {total} years with active exchange data</span></div>{annual_content}</section></div></td></tr>
    """


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return _safe_json(value)
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return str(value)


def _write_csv(records: list[dict[str, Any]], path: Path) -> None:
    extras = sorted(
        {key for record in records for key in record if key not in CANONICAL_FIELDS}
    )
    fields = [*CANONICAL_FIELDS, *extras]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for record in records:
            writer.writerow({field: _csv_value(record.get(field)) for field in fields})


def _status_categories(records: Iterable[Mapping[str, Any]]) -> list[str]:
    present = {_status_category(record.get("status")) for record in records}
    order = ("measured", "assigned", "ambiguous", "na")
    return [category for category in order if category in present]


def _select_options(
    records: Iterable[Mapping[str, Any]], field: str, all_label: str
) -> str:
    records = list(records)
    if field == "status":
        values = _status_categories(records)
        option_text = [(value, _CATEGORY_LABELS[value]) for value in values]
    else:
        values = {_text(record.get(field)).strip() for record in records}
        values.discard("")
        if field == "year":
            values = sorted(
                values,
                key=lambda value: int(value) if value.isdigit() else -1,
                reverse=True,
            )
        else:
            values = sorted(values, key=str.casefold)
        option_text = [(value, value) for value in values]
    options = [f'<option value="">{html.escape(all_label, quote=True)}</option>']
    options.extend(
        f'<option value="{html.escape(value, quote=True)}">{html.escape(label, quote=True)}</option>'
        for value, label in option_text
    )
    return "\n".join(options)


def _profile_subject_options(groups: Iterable[Mapping[str, Any]]) -> str:
    subjects = {
        _text(subject).strip()
        for group in groups
        for subject in (group.get("profile_subjects") or [])
        if _text(subject).strip()
    }
    options = ['<option value="">All subject areas</option>']
    options.extend(
        f'<option value="{html.escape(subject, quote=True)}">{html.escape(subject, quote=True)}</option>'
        for subject in sorted(subjects, key=str.casefold)
    )
    return "\n".join(options)


def _width_label(entry: Mapping[str, Any]) -> str:
    maps = entry.get("full_width_pt_by_year") or entry.get("full_width_by_year")
    if isinstance(maps, Mapping):
        values: list[str] = []
        for year, value in sorted(maps.items(), key=lambda item: str(item[0])):
            width = _finite_float(value)
            if width is not None:
                values.append(f"{year}: {width:.5f} pt")
        if values:
            return "; ".join(values)
    width = _finite_float(entry.get("full_width_pt"))
    return f"{width:.5f} pt" if width is not None else "Full width not recorded"


def _calibration_catalog(
    records: Iterable[Mapping[str, Any]], calibration: Any
) -> list[dict[str, Any]]:
    entries = _calibration_entries(calibration)
    fallback = _calibration_container_source(calibration)
    catalog: dict[str, dict[str, Any]] = {}

    def add_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
        item = dict(entry)
        meta = _calibration_meta(item, fallback)
        for field, value in meta.items():
            item.setdefault(field, value)
        calibration_id = _text(item.get("calibration_id"))
        document_id = _text(item.get("source_document_id"))
        source_hash = _text(item.get("source_sha256"))
        if calibration_id:
            key = calibration_id
        elif document_id:
            key = f"source:{document_id}"
        elif source_hash:
            key = f"hash:{source_hash}"
        else:
            key = f"anonymous:{len(catalog)}"
        if key not in catalog:
            catalog[key] = item
        else:
            for field, value in item.items():
                if catalog[key].get(field) in (None, "") and value not in (None, ""):
                    catalog[key][field] = value
        return catalog[key]

    for entry in entries:
        add_entry(entry)
    for record in records:
        if not _text(record.get("calibration_id")) and not _text(
            record.get("source_document_id")
        ):
            continue
        matched = _calibration_for_record(record, entries, fallback)
        item = add_entry(matched or record)
        for field in _SOURCE_FIELDS + ("calibration_id", "full_width_pt"):
            if item.get(field) in (None, "") and record.get(field) not in (None, ""):
                item[field] = record[field]
    result = list(catalog.values())
    result.sort(
        key=lambda item: (
            _release_year(item) is None,
            -(_release_year(item) or 0),
            _canonical_text(item.get("calibration_id") or item.get("source_filename")),
        )
    )
    return result


def _render_calibration_catalog(catalog: Sequence[Mapping[str, Any]]) -> str:
    if not catalog:
        return (
            '<p class="calibration-empty">No calibration metadata was supplied; '
            "annual provenance remains available where present.</p>"
        )
    items: list[str] = []
    for entry in catalog:
        meta = _calibration_meta(entry)
        calibration_id = _text(meta.get("calibration_id")) or "Unlabelled calibration"
        filename = _text(meta.get("source_filename")) or "Source document not recorded"
        release = _integer(meta.get("source_release_year"))
        release_text = f"Release {release}" if release is not None else "Release not recorded"
        items.append(
            f'<li class="calibration-entry"><strong>{html.escape(calibration_id, quote=True)}</strong>'
            f'<span>{html.escape(release_text, quote=True)} · {html.escape(filename, quote=True)}</span>'
            f'<span>Full-width map: {html.escape(_width_label(entry), quote=True)}</span></li>'
        )
    return '<ul class="calibration-list">' + "".join(items) + "</ul>"


def _calibration_label(catalog: Sequence[Mapping[str, Any]]) -> str:
    if not catalog:
        return "Not supplied"
    if len(catalog) == 1:
        identifier = _text(_calibration_meta(catalog[0]).get("calibration_id"))
        return identifier or "1 calibration"
    return f"{len(catalog)} calibrations"


def _group_payload(group: Mapping[str, Any], index: int) -> dict[str, Any]:
    return {
        "index": index,
        "country": group.get("country"),
        "university": group.get("university"),
        "mean_score": group.get("mean_score"),
        "scored_count": group.get("scored_count"),
        "total_count": group.get("total_count"),
        "is_latest_roster": bool(group.get("is_latest_roster", True)),
        "categories": list(group.get("categories") or []),
        "source_only": bool(group.get("source_only")),
        "profile_levels": list(group.get("profile_levels") or []),
        "profile_subjects": list(group.get("profile_subjects") or []),
        "profile_cities": list(group.get("profile_cities") or []),
        "profiles": [
            _profile_payload(profile)
            for profile in (group.get("profiles") or [])
            if isinstance(profile, Mapping)
        ],
        "search_text": group.get("search_text", ""),
        "records": [
            {
                key: value
                for key, value in record.items()
                if not str(key).startswith("_")
            }
            for record in group.get("records") or []
        ],
    }


def _build_html(
    records: Iterable[Mapping[str, Any]], calibration: Any, profiles: Any = None
) -> str:
    prepared = _prepare_records(records, calibration)
    groups = _attach_profile_catalog(_group_records(prepared), profiles)
    current_records = [
        record for group in groups for record in (group.get("records") or [])
    ]
    initial_rows = "".join(
        _render_university_row(group, index) for index, group in enumerate(groups)
    )
    if not initial_rows:
        initial_rows = (
            '<tr id="empty-state-row" class="empty-row">'
            '<td colspan="5"><div class="empty-state"><strong>No extracted rows yet</strong>'
            "<p>Run the extractor to populate this report.</p></div></td></tr>"
        )
    catalog = _calibration_catalog(current_records, calibration)
    years = sorted(
        {
            record.get("year")
            for record in current_records
            if record.get("year") is not None
        }
    )
    year_span = f"{min(years)}–{max(years)}" if years else "—"
    countries = {
        group.get("country")
        for group in groups
        if group.get("country")
    }
    data_payload = {
        "groups": [_group_payload(group, index) for index, group in enumerate(groups)]
    }
    profile_subject_options = _profile_subject_options(groups)
    template = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light">
  <title>TUMexchange Acceptance Chances</title>
  <style>
    :root {
      --ink: #182d3d;
      --muted: #607687;
      --line: #d8e3eb;
      --paper: #ffffff;
      --wash: #f4f8fb;
      --blue: #2367a6;
      --blue-deep: #16466f;
      --blue-soft: #e7f0f8;
      --green: #286244;
      --green-soft: #e7f2ec;
      --amber: #9b6200;
      --amber-soft: #fff4d6;
      --shadow: 0 16px 50px rgba(22, 58, 88, .08);
      font-family: "Avenir Next", "Segoe UI", system-ui, sans-serif;
      color: var(--ink);
      background: var(--wash);
    }
    * { box-sizing: border-box; }
    html { overflow-x: clip; }
    body {
      margin: 0; min-width: 320px; background: var(--wash); line-height: 1.45;
      padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
    }
    a { color: var(--blue-deep); text-underline-offset: .18em; }
    button, input, select { font: inherit; }
    button, select { cursor: pointer; }
    button, a, input, select { touch-action: manipulation; }
    :focus-visible { outline: 3px solid #6eaee0; outline-offset: 2px; }
    [hidden] { display: none !important; }
    .shell { width: min(1440px, calc(100% - 48px)); margin: 0 auto; }
    .utility {
      display: flex; align-items: center; justify-content: space-between; gap: 24px;
      min-height: 58px; border-bottom: 1px solid var(--line);
      color: var(--muted); font-size: .82rem;
    }
    .brand { display: inline-flex; align-items: center; gap: 10px; color: var(--blue-deep); font-weight: 700; }
    .brand-mark {
      display: grid; place-items: center; width: 27px; height: 27px;
      border: 1px solid var(--blue); border-radius: 50%;
      font-family: Georgia, serif; font-size: 1.05rem;
    }
    .downloads { display: flex; flex-wrap: wrap; gap: 16px; }
    .downloads a { color: var(--muted); }
    .hero {
      display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(300px, 1fr);
      gap: 34px; align-items: center; padding: 48px 0 30px;
    }
    .eyebrow, .detail-kicker {
      margin: 0 0 10px; color: var(--blue); font-size: .68rem;
      font-weight: 700; letter-spacing: .1em; text-transform: uppercase;
    }
    h1, h2, h3, p { margin-top: 0; }
    h1 {
      max-width: 760px; margin-bottom: 12px; font-family: Georgia, "Times New Roman", serif;
      font-size: clamp(1.85rem, 7vw, 3.7rem); font-weight: 400;
      letter-spacing: -.04em; line-height: 1.05;
    }
    .filter-heading { margin-bottom: 10px; font-size: 1.05rem; font-weight: 650; }
    .lede { max-width: 760px; margin-bottom: 0; color: var(--muted); font-size: .98rem; }
    .method-card {
      padding: 20px 22px; background: var(--ink); color: #fff;
      border-left: 4px solid #79b4df; box-shadow: var(--shadow);
    }
    .method-card h2 { margin-bottom: 10px; font-size: 1.05rem; font-weight: 650; }
    .method-card p { margin-bottom: 8px; color: #d5e2eb; font-size: .86rem; }
    .method-card p:last-child { margin-bottom: 0; }
    .method-card strong { color: #fff; }
    .method-note { margin-top: 10px; color: #a9bac5; font-size: .78rem; font-style: italic; }
    .summary {
      display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1px;
      margin: 0 0 24px; background: var(--line); border: 1px solid var(--line);
    }
    .summary div { min-width: 0; padding: 12px 15px; background: var(--paper); }
    .summary dt {
      color: var(--muted); font-size: .65rem; letter-spacing: .07em;
      text-transform: uppercase; white-space: nowrap;
    }
    .summary dd {
      overflow: hidden; margin: 3px 0 0; color: var(--blue-deep);
      font-size: 1.15rem; font-weight: 700; line-height: 1.1;
      text-overflow: ellipsis; white-space: nowrap;
    }
    .summary dd .stat-sub {
      font-size: .8rem; font-weight: 500; color: var(--muted); margin-left: 4px;
    }
    .controls {
      display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px; align-items: end; margin-bottom: 15px;
    }
    .field { min-width: 0; }
    .field label { display: block; margin: 0 0 7px; color: var(--muted); font-size: .77rem; font-weight: 650; }
    .field input, .field select {
      width: 100%; min-height: 44px; padding: 9px 11px; font-size: 16px;
      border: 1px solid #c6d5df; border-radius: 2px; background: var(--paper); color: var(--ink);
    }
    .search-field input { min-height: 46px; padding: 10px 12px; }
    .field input::placeholder { color: #8799a5; }
    .label-with-help { display: flex; align-items: center; gap: 7px; min-height: 20px; }
    .label-with-help label { margin-bottom: 0; }
    .help { position: relative; display: inline-flex; }
    .help-trigger {
      position: relative; display: grid; place-items: center; width: 18px; height: 18px;
      padding: 0; border: 1px solid #91adc0; border-radius: 50%;
      background: var(--paper); color: var(--blue-deep); font-size: .68rem; font-weight: 700;
    }
    .help-trigger::before { position: absolute; inset: -13px; content: ""; }
    .help-tooltip {
      position: absolute; z-index: 10; top: 40px; right: 0; width: min(280px, calc(100vw - 40px));
      margin: 0; padding: 10px 11px; border: 1px solid var(--line);
      background: var(--paper); color: var(--muted); box-shadow: var(--shadow);
      font-size: .77rem; font-weight: 400; opacity: 0; pointer-events: none;
      visibility: hidden;
    }
    .help-trigger:hover + .help-tooltip,
    .help-trigger:focus-visible + .help-tooltip,
    .help:focus-within .help-tooltip { opacity: 1; pointer-events: auto; visibility: visible; }
    .reset-button {
      min-height: 44px; padding: 8px 13px; border: 1px solid var(--blue);
      border-radius: 2px; background: var(--paper); color: var(--blue-deep); font-weight: 650;
    }
    .mobile-sort { display: none; }
    .year-filter-section {
      grid-column: 1 / -1;
      margin: 4px 0 16px;
      padding: 16px 18px;
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 4px;
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
    }
    .year-filter-header {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 12px;
    }
    .year-filter-label {
      font-size: .82rem;
      font-weight: 700;
      color: var(--blue-deep);
      text-transform: uppercase;
      letter-spacing: .06em;
    }
    .year-filter-presets {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .preset-btn {
      min-height: 32px;
      padding: 4px 12px;
      font-size: .78rem;
      font-weight: 600;
      color: var(--blue-deep);
      background: var(--blue-soft);
      border: 1px solid #cce2f2;
      border-radius: 3px;
      cursor: pointer;
      transition: background 0.15s ease, border-color 0.15s ease;
    }
    .preset-btn:hover {
      background: #c3ddf0;
      border-color: #a5cced;
    }
    .preset-btn:focus-visible {
      outline: 2px solid var(--blue);
      outline-offset: 2px;
    }
    .year-pills {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .year-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 40px;
      padding: 6px 14px;
      font-size: .88rem;
      font-weight: 600;
      color: var(--muted);
      background: #f1f5f9;
      border: 1px solid #cbd5e1;
      border-radius: 20px;
      cursor: pointer;
      user-select: none;
      transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease, transform 0.1s ease;
    }
    .year-pill:hover {
      background: #e2e8f0;
      color: var(--ink);
    }
    .year-pill:focus-visible {
      outline: 2px solid var(--blue);
      outline-offset: 2px;
    }
    .year-pill[aria-pressed="true"] {
      background: var(--blue-deep);
      border-color: var(--blue-deep);
      color: #ffffff;
      box-shadow: 0 1px 3px rgba(0, 82, 147, 0.3);
    }
    .year-check {
      display: none;
      font-size: .8rem;
      font-weight: 700;
    }
    .year-pill[aria-pressed="true"] .year-check {
      display: inline;
    }
    .year-era-tag {
      font-size: .68rem;
      font-weight: 600;
      padding: 1px 6px;
      border-radius: 8px;
      background: #e2e8f0;
      color: #475569;
    }
    .year-pill[aria-pressed="true"] .year-era-tag {
      background: rgba(255, 255, 255, 0.25);
      color: #ffffff;
    }
    .traffic-light-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 2px 7px;
      border-radius: 3px;
      font-size: .8rem;
      font-weight: 650;
      font-variant-numeric: tabular-nums;
    }
    .tier-blocks {
      display: inline-flex;
      gap: 2px;
      align-items: center;
    }
    .tier-block {
      width: 6px;
      height: 11px;
      border-radius: 1px;
      background: #cbd5e1;
    }
    .traffic-light-pill.tier-1 {
      background: #fef2f2;
      color: #991b1b;
      border: 1px solid #fecaca;
    }
    .traffic-light-pill.tier-1 .tier-block.filled { background: #dc2626; }
    .traffic-light-pill.tier-2 {
      background: #fffbeb;
      color: #92400e;
      border: 1px solid #fde68a;
    }
    .traffic-light-pill.tier-2 .tier-block.filled { background: #d97706; }
    .traffic-light-pill.tier-3 {
      background: #f7fee7;
      color: #3f6212;
      border: 1px solid #d9f99d;
    }
    .traffic-light-pill.tier-3 .tier-block.filled { background: #65a30d; }
    .traffic-light-pill.tier-4 {
      background: #f0fdf4;
      color: #166534;
      border: 1px solid #bbf7d0;
    }
    .traffic-light-pill.tier-4 .tier-block.filled { background: #16a34a; }
    .tier-1-fill { background: #dc2626 !important; }
    .tier-2-fill { background: #d97706 !important; }
    .tier-3-fill { background: #65a30d !important; }
    .tier-4-fill { background: #16a34a !important; }
    .basis-badge {
      position: relative;
      display: inline-flex;
      align-items: center;
      margin-left: 6px;
      padding: 2px 7px;
      font-size: .68rem;
      font-weight: 700;
      border-radius: 3px;
      letter-spacing: .04em;
      text-transform: uppercase;
      vertical-align: middle;
      cursor: help;
      text-decoration: underline dotted 1px rgba(0, 0, 0, 0.3);
      text-underline-offset: 2px;
      transition: background 0.15s ease, color 0.15s ease, box-shadow 0.15s ease;
    }
    .basis-badge:hover, .basis-badge:focus-visible {
      text-decoration-color: currentColor;
      z-index: 40;
    }
    .basis-bar { background: var(--blue-soft); color: var(--blue-deep); }
    .basis-exact { background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }
    .basis-tier { background: #f1f5f9; color: #475569; }
    .basis-assigned { background: #ecfdf5; color: #047857; }
    .score-track.is-exact .score-fill { background: #1d4ed8; }
    .basis-tooltip {
      position: absolute;
      bottom: calc(100% + 7px);
      left: 50%;
      transform: translateX(-50%);
      width: max-content;
      max-width: 255px;
      padding: 8px 11px;
      border-radius: 5px;
      background: #0b2540;
      color: #f1f5f9;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
      font-size: .74rem;
      font-weight: 400;
      line-height: 1.35;
      text-transform: none;
      letter-spacing: normal;
      pointer-events: none;
      opacity: 0;
      visibility: hidden;
      transition: opacity 0.15s ease, transform 0.15s ease;
      z-index: 100;
      white-space: normal;
      text-align: left;
    }
    .basis-tooltip::after {
      content: "";
      position: absolute;
      top: 100%;
      left: 50%;
      transform: translateX(-50%);
      border-width: 5px;
      border-style: solid;
      border-color: #0b2540 transparent transparent transparent;
    }
    .basis-tooltip .basis-tooltip-title {
      display: block;
      color: #93c5fd;
      font-size: .76rem;
      font-weight: 700;
      margin-bottom: 3px;
    }
    .basis-tooltip .basis-tooltip-body {
      display: block;
      color: #e2e8f0;
    }
    .basis-badge:hover .basis-tooltip,
    .basis-badge:focus-visible .basis-tooltip,
    .basis-badge:focus .basis-tooltip {
      opacity: 1;
      visibility: visible;
      transform: translateX(-50%) translateY(-2px);
    }
    .historical-archive-row td {
      padding: 0 !important;
      background: #f8fafc;
      border-top: 1px dashed #cbd5e1;
    }
    .historical-archive-disclosure { border: none; }
    .archive-summary-button {
      display: flex;
      align-items: center;
      gap: 10px;
      min-height: 44px;
      padding: 10px 16px;
      cursor: pointer;
      color: var(--blue);
      font-weight: 650;
      font-size: .84rem;
      background: #f8fafc;
      list-style: none;
      user-select: none;
      transition: background 0.15s ease, color 0.15s ease;
    }
    .archive-summary-button::-webkit-details-marker { display: none; }
    .archive-summary-button:hover {
      background: #f1f5f9;
      color: var(--blue-deep);
    }
    .archive-summary-button:focus-visible {
      outline: 2px solid var(--blue);
      outline-offset: -2px;
    }
    .archive-badge {
      font-size: .68rem;
      font-weight: 700;
      letter-spacing: .06em;
      text-transform: uppercase;
      padding: 2px 6px;
      background: #e2e8f0;
      color: #475569;
      border-radius: 2px;
    }
    .archive-chevron {
      margin-left: auto;
      font-size: .9rem;
      transition: transform 0.2s ease;
    }
    .historical-archive-disclosure[open] .archive-chevron {
      transform: rotate(180deg);
    }
    .archive-table {
      margin: 0;
      border-top: 1px solid #e2e8f0;
      background: #fafbfc;
    }
    tr.annual-data-row {
      position: relative;
    }
    tr.annual-data-row:hover,
    tr.annual-data-row:focus-within {
      z-index: 25;
    }
    tr.annual-data-row.is-year-inactive {
      opacity: 0.55;
      transition: opacity 0.15s ease;
    }
    tr.annual-data-row.is-year-inactive:hover,
    tr.annual-data-row.is-year-inactive:focus-within {
      opacity: 1;
    }
    .result-line {
      display: flex; justify-content: space-between; gap: 16px; margin-bottom: 12px;
      color: var(--muted); font-size: .82rem;
    }
    .result-line strong { color: var(--ink); }
    .table-frame { overflow-x: auto; background: var(--paper); border: 1px solid var(--line); box-shadow: var(--shadow); }
    table { width: 100%; min-width: 860px; border-collapse: collapse; text-align: left; }
    th {
      padding: 14px 16px; border-bottom: 1px solid var(--line); background: #f7fafc;
      color: var(--muted); font-size: .72rem; font-weight: 700;
      letter-spacing: .08em; text-transform: uppercase; white-space: nowrap;
    }
    th.sortable { padding: 0; }
    .sort-button {
      display: inline-flex; align-items: center; gap: 8px; width: 100%;
      min-height: 46px; padding: 14px 16px; border: 0; background: transparent;
      color: inherit; font: inherit; letter-spacing: inherit; text-transform: inherit;
      text-align: left;
    }
    .sort-button:hover { color: var(--blue-deep); }
    th[aria-sort="ascending"] .sort-button,
    th[aria-sort="descending"] .sort-button { color: var(--blue-deep); }
    .sort-indicator {
      display: inline-block; min-width: .7em; font-size: .78em; line-height: 1;
      color: var(--blue-deep);
    }
    .sort-indicator::before { content: ""; }
    th[aria-sort="ascending"] .sort-indicator::before { content: "▲"; }
    th[aria-sort="descending"] .sort-indicator::before { content: "▼"; }
    td { padding: 12px 16px; border-bottom: 1px solid #e7eef3; color: #365264; font-size: .9rem; vertical-align: middle; }
    tbody tr.data-row:hover, tbody tr.annual-data-row:hover { background: #fbfdfe; }
    tr.university-row { cursor: pointer; }
    .university-cell { width: 38%; color: var(--ink); }
    .university-name { display: block; color: var(--ink); font-weight: 680; }
    .roster-note { display: block; margin-top: 3px; color: var(--amber); font-size: .7rem; font-weight: 650; }
    .profile-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 5px 7px; margin-top: 7px; color: var(--muted); font-size: .72rem; }
    .profile-badge { display: inline-flex; align-items: center; min-height: 22px; padding: 2px 7px; border-radius: 999px; background: var(--blue-soft); color: var(--blue-deep); font-size: .68rem; font-weight: 700; white-space: nowrap; }
    .profile-badge-master { background: var(--green-soft); color: var(--green); }
    .profile-badge-unknown { background: #edf1f3; color: #60717b; }
    .profile-city, .profile-subject-count { color: var(--muted); }
    .profile-missing { color: var(--muted); font-style: italic; }
    .university-toggle {
      display: flex; align-items: baseline; gap: 10px; width: 100%; padding: 0;
      border: 0; background: transparent; color: inherit; text-align: left;
    }
    .university-toggle:hover .university-name,
    .university-toggle:focus-visible .university-name { color: var(--blue); }
    .expand-label { color: var(--muted); font-size: .72rem; font-weight: 500; white-space: nowrap; }
    .years-cell { width: 18%; }
    .years-cell span { display: block; color: var(--muted); font-size: .72rem; }
    .numeric { font-variant-numeric: tabular-nums; }
    .source-cell { color: var(--muted); }
    .source-release { color: var(--blue-deep); font-size: .74rem; font-weight: 700; white-space: nowrap; }
    .status-list { display: flex; flex-wrap: wrap; gap: 5px; }
    .status-pill {
      display: inline-flex; align-items: center; min-height: 26px; padding: 3px 8px;
      border-radius: 2px; font-size: .73rem; font-weight: 700; white-space: nowrap;
    }
    .status-measured { background: var(--blue-soft); color: var(--blue-deep); }
    .status-assigned { background: var(--green-soft); color: var(--green); }
    .status-na { background: #edf1f3; color: #60717b; }
    .status-ambiguous { background: var(--amber-soft); color: var(--amber); }
    .score-cell { width: 28%; min-width: 180px; }
    .score-line { display: flex; align-items: baseline; gap: 7px; margin-bottom: 7px; }
    .score-value { color: var(--ink); font-variant-numeric: tabular-nums; font-weight: 720; }
    .score-unit { color: var(--muted); font-size: .73rem; }
    .score-track {
      position: relative; width: 100%; max-width: 210px; height: 9px; overflow: hidden;
      border-radius: 1px; background: #dbe7ee;
    }
    .score-track::after {
      position: absolute; top: 0; right: 0; bottom: 0; width: 1px;
      background: var(--blue-deep); opacity: .75; content: "";
    }
    .score-track.is-empty { background: repeating-linear-gradient(135deg, #e8eef1 0, #e8eef1 4px, #d5e0e5 4px, #d5e0e5 8px); }
    .score-track.is-empty::after { display: none; }
    .score-fill { display: block; height: 100%; background: var(--blue); }
    .details-cell { width: 16%; text-align: right; }
    .detail-toggle {
      display: inline-flex; align-items: center; justify-content: center;
      min-height: 36px; padding: 6px 14px; border: 1px solid #b8d2e5;
      border-radius: 4px; background: #f4f8fb; color: var(--blue-deep);
      font-size: .8rem; font-weight: 650; text-decoration: none;
      transition: all 0.15s ease;
    }
    .detail-toggle:hover {
      background: var(--blue); color: #ffffff; border-color: var(--blue);
      box-shadow: 0 1px 4px rgba(0, 82, 147, 0.25);
    }
    tr.university-row {
      cursor: pointer;
      transition: background 0.15s ease, box-shadow 0.15s ease;
    }
    tr.university-row.is-expanded {
      background: #eaf3fa !important;
      border-top: 2px solid var(--blue);
      box-shadow: inset 5px 0 0 var(--blue);
    }
    tr.university-row.is-expanded td {
      background: #eaf3fa !important;
      border-bottom: 1px solid #d4e4f0;
    }
    tr.university-row.is-expanded .university-name {
      color: var(--blue-deep);
      font-weight: 750;
    }
    tr.university-row.is-expanded .expand-label {
      color: var(--blue);
      font-weight: 650;
    }
    tr.university-row.is-expanded .university-detail-toggle {
      background: var(--blue);
      color: #ffffff;
      border-color: var(--blue);
      box-shadow: 0 1px 4px rgba(0, 82, 147, 0.3);
    }
    tr.university-row.is-expanded .university-detail-toggle:hover {
      background: var(--blue-deep);
      border-color: var(--blue-deep);
    }
    tr.university-detail-row:not([hidden]) {
      box-shadow: inset 5px 0 0 var(--blue), 0 10px 24px -6px rgba(0, 51, 89, 0.12);
      border-bottom: 3px solid var(--blue) !important;
    }
    tr.university-detail-row > td {
      padding: 18px 24px 26px;
      background: #eef5fa;
    }
    .detail-card-shell {
      display: grid;
      gap: 18px;
    }
    .panel-eyebrow {
      display: block;
      margin-bottom: 3px;
      color: var(--blue);
      font-size: .65rem;
      font-weight: 700;
      letter-spacing: .08em;
      text-transform: uppercase;
    }
    .profile-panel {
      padding: 20px 22px 18px;
      border: 1px solid #c9dceb;
      border-radius: 6px;
      background: var(--paper);
      box-shadow: 0 2px 6px rgba(0, 51, 89, 0.04);
    }
    .profile-panel-empty {
      background: #fbfdfe;
      border-style: dashed;
    }
    .profile-panel-heading {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 14px;
    }
    .profile-panel-heading h3 {
      margin: 0;
      font-family: Georgia, serif;
      font-size: 1.22rem;
      font-weight: 400;
      color: var(--ink);
    }
    .profile-panel-meta, .profile-retrieved { color: var(--muted); font-size: .78rem; }
    .profile-empty-copy { max-width: 720px; margin: 0; color: var(--muted); font-size: .84rem; }
    .profile-relations { display: grid; gap: 8px; }
    .profile-relation { border: 1px solid #d4e3ef; border-radius: 4px; background: #fbfdfe; }
    .profile-relation > summary { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 12px 14px; cursor: pointer; list-style-position: inside; color: var(--ink); border-radius: 4px; }
    .profile-relation > summary::marker { color: var(--blue); }
    .profile-relation > summary { position: relative; padding-right: 48px; }
    .profile-relation > summary::after { content: "+"; position: absolute; right: 16px; top: 10px; color: var(--blue); font-size: 1.4rem; font-weight: 600; }
    .profile-relation[open] > summary::after { content: "−"; }
    .profile-open-hint { flex-basis: 100%; color: var(--blue); font-size: .75rem; }
    .profile-hint-open, .profile-relation[open] .profile-hint-closed { display: none; }
    .profile-relation[open] .profile-hint-open { display: inline; }
    .profile-relation > summary:hover { background: #f0f7fc; }
    .profile-relation > summary:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
    .profile-relation-title { display: inline-flex; flex-wrap: wrap; align-items: baseline; gap: 5px 10px; min-width: 0; }
    .profile-relation-meta { color: var(--muted); font-size: .76rem; font-weight: 400; }
    .profile-relation-badges { display: inline-flex; flex-wrap: wrap; gap: 5px; justify-content: flex-end; }
    .profile-relation-body { padding: 2px 16px 16px; }
    .profile-relation-context { display: flex; flex-wrap: wrap; gap: 5px 14px; margin: 0 0 12px; color: var(--muted); font-size: .78rem; }
    .profile-subjects { color: var(--blue-deep); }
    .profile-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 18px; margin: 0; }
    .profile-field { min-width: 0; padding-top: 8px; border-top: 1px solid var(--line); }
    .profile-field dt { margin-bottom: 3px; color: var(--muted); font-size: .72rem; font-weight: 700; }
    .profile-field dd { margin: 0; color: #365264; font-size: .83rem; overflow-wrap: anywhere; }
    .profile-links { display: flex; flex-wrap: wrap; gap: 8px 15px; margin-top: 8px; font-size: .78rem; font-weight: 650; }
    .profile-empty { color: var(--muted); font-style: italic; }
    .historical-empty { padding: 23px 18px; border: 1px dashed var(--line); background: var(--paper); color: var(--muted); border-radius: 4px; }
    .historical-empty strong { display: block; margin-bottom: 3px; color: var(--ink); }
    .historical-empty p { margin: 0; font-size: .83rem; }
    .annual-panel {
      padding: 20px 22px 18px;
      border: 1px solid #c9dceb;
      border-radius: 6px;
      background: var(--paper);
      box-shadow: 0 2px 6px rgba(0, 51, 89, 0.04);
    }
    .annual-heading {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      margin: 0 0 14px;
      color: var(--muted);
      font-size: .8rem;
    }
    .annual-heading h3 {
      margin: 0;
      font-family: Georgia, serif;
      font-size: 1.22rem;
      font-weight: 400;
      color: var(--ink);
    }
    .annual-count-badge {
      display: inline-flex;
      align-items: center;
      padding: 3px 10px;
      border-radius: 4px;
      background: #edf5fa;
      color: var(--blue-deep);
      font-size: .76rem;
      font-weight: 650;
      border: 1px solid #d3e4f0;
    }
    .annual-table {
      width: 100%;
      min-width: 540px;
      border: 1px solid #cfe0ec;
      border-radius: 4px;
      overflow: visible;
      box-shadow: none;
    }
    .annual-table th {
      padding: 10px 12px;
      font-size: .68rem;
      font-weight: 700;
      letter-spacing: .06em;
      text-transform: uppercase;
      background: #f8fafc;
      border-bottom: 1px solid #cfe0ec;
      color: #475569;
    }
    .annual-table td {
      padding: 10px 12px;
      font-size: .84rem;
      border-bottom: 1px solid #eef3f7;
    }
    .annual-table tbody tr:last-child td {
      border-bottom: none;
    }
    .empty-row td { border-bottom: 0; }
    .empty-state { padding: 50px 24px; text-align: center; }
    .empty-state strong { display: block; color: var(--ink); font-family: Georgia, serif; font-size: 1.35rem; font-weight: 400; }
    .empty-state p { margin: 8px 0 17px; color: var(--muted); }
    footer { display: flex; justify-content: space-between; gap: 24px; padding: 30px 0 48px; color: var(--muted); font-size: .78rem; }
    footer p { margin: 0; }
    .visually-hidden {
      position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
      overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
    }
    @media (max-width: 1120px) {
      .controls { grid-template-columns: repeat(3, minmax(145px, 1fr)); }
      .search-field { grid-column: 1 / -1; }
    }
    @media (max-width: 960px) {
      .shell { width: min(100% - 28px, 1440px); }
      .hero { grid-template-columns: 1fr; gap: 22px; padding: 28px 0 18px; }
      .utility { align-items: flex-start; flex-direction: column; gap: 10px; padding: 14px 0; }
      .downloads { gap: 12px; }
      .summary { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .summary dd { white-space: normal; }
      .controls { grid-template-columns: 1fr 1fr; }
      .search-field, .reset-button { grid-column: 1 / -1; }
      .result-line, footer, .annual-heading { align-items: flex-start; flex-direction: column; gap: 6px; }
      .mobile-sort {
        display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
        margin: 0 0 12px; color: var(--muted); font-size: .77rem;
      }
      .mobile-sort-label { margin-right: 2px; font-weight: 650; }
      .mobile-sort-button {
        display: inline-flex; align-items: center; gap: 6px; min-height: 44px; padding: 8px 12px;
        border: 1px solid #c6d5df; border-radius: 2px; background: var(--paper);
        color: var(--blue-deep); font-size: .78rem; font-weight: 650;
      }
      .mobile-sort-button.is-active { border-color: var(--blue); background: var(--blue-soft); }
      .mobile-sort-button .sort-indicator { min-width: .7em; color: var(--blue-deep); }
      .table-frame { overflow: visible; background: transparent; border: 0; box-shadow: none; }
      #report-table, .annual-table { min-width: 0; }
      #report-table > thead { display: none; }
      #report-table, #report-table > tbody, .annual-table, .annual-table > tbody { display: block; width: 100%; }
      #report-table > tbody { display: grid; gap: 10px; }
      tr.university-row, tr.annual-data-row {
        display: grid; grid-template-columns: 1fr; gap: 8px 12px;
        padding: 14px; border: 1px solid var(--line); background: var(--paper);
      }
      tr.university-row { box-shadow: var(--shadow); }
      tr.university-detail-row, tr.empty-row { display: block; }
      tr.university-detail-row > td, tr.empty-row > td {
        display: block; width: 100%;
      }
      tr.university-row > td, tr.annual-data-row > td {
        display: grid; grid-template-columns: minmax(7.2rem, 32%) minmax(0, 1fr);
        align-items: start; gap: 8px; width: 100%; min-width: 0; padding: 0; border: 0;
      }
      tr.university-row > td::before, tr.annual-data-row > td::before {
        color: var(--muted); font-size: .68rem; font-weight: 700; letter-spacing: .06em;
        text-transform: uppercase; content: attr(data-label);
      }
      tr.university-row > .university-cell { grid-template-columns: 1fr; }
      tr.university-row > .university-cell::before { display: none; }
      tr.university-row > .details-cell { display: none; }
      .university-toggle { align-items: flex-start; justify-content: space-between; min-height: 44px; gap: 12px; }
      .expand-label { flex: 0 0 auto; padding-top: 2px; color: var(--blue-deep); font-weight: 650; }
      .university-cell, .score-cell, .details-cell { width: auto; min-width: 0; }
      .years-cell span { display: none; }
      .score-cell, .score-track { max-width: none; }
      .profile-panel { padding: 16px 14px 15px; }
      .profile-panel-heading { align-items: flex-start; flex-direction: column; gap: 5px; }
      .profile-relation > summary { align-items: flex-start; flex-direction: column; gap: 8px; }
      .profile-relation-badges { justify-content: flex-start; }
      .profile-fields { grid-template-columns: 1fr; gap: 8px; }
      .annual-panel { padding: 12px 4px 4px; }
      .annual-table { border: 0; }
      .annual-table > thead { display: none; }
      .annual-table > tbody { display: grid; gap: 8px; }
      tr.annual-data-row { background: #fbfdfe; }
      .details-cell { text-align: left; }
    }
    @media (max-width: 840px) {
      .hero { grid-template-columns: 1fr; gap: 24px; }
    }
    @media (max-width: 620px) {
      .summary { grid-template-columns: 1fr; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; animation-duration: .01ms !important; }
    }
    @media print {
      body { background: #fff; }
      .utility, .controls, .mobile-sort, .detail-toggle, footer { display: none; }
      .shell { width: 100%; }
      .hero { padding: 20px 0; }
      .table-frame { overflow: visible; box-shadow: none; }
      table { min-width: 0; }
      .university-detail-row[hidden] { display: table-row !important; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <nav class="utility" aria-label="Report navigation">
      <div class="brand"><span class="brand-mark" aria-hidden="true">T</span><span>TUMexchange Chances</span></div>
      <div class="downloads"><a href="rows.csv" download>Download CSV data</a></div>
    </nav>
    <header class="hero">
      <div>
        <h1>TUMexchange Acceptance Chances</h1>
        <p class="lede">Explore historical admission chances and check partner university requirements for TUMexchange. Filter by country, degree level, or subject area.</p>
      </div>
      <aside class="method-card" aria-labelledby="method-heading">
        <p class="eyebrow">How to read the bars</p>
        <h2 id="method-heading">What the bars mean</h2>
        <p><strong>Full bar (100%):</strong> All applicants were accepted (places exceeded applicants, or no one applied that year).</p>
        <p><strong>Partial bar:</strong> Shows the percentage of applicants accepted (for example, 50% means half of applicants got a place).</p>
        <p class="method-note">Competition fluctuates each semester based on applicant volume and student grades; past rates serve as a helpful guide, not an admission guarantee.</p>
      </aside>
    </header>
    <dl class="summary" aria-label="Report summary">
      <div><dt>Partner universities</dt><dd id="stat-universities">@@ACTIVE_UNIVERSITIES@@ <span class="stat-sub">active (@@UNIVERSITIES@@ total)</span></dd></div>
      <div><dt>Countries</dt><dd id="stat-countries">@@COUNTRIES@@</dd></div>
      <div><dt>Years covered</dt><dd id="stat-year-span">@@YEAR_SPAN@@</dd></div>
    </dl>
    <section aria-labelledby="filters-heading">
      <h2 id="filters-heading" class="filter-heading">Find a university</h2>
      <div class="controls">
        <div class="field search-field"><label for="search">Search university, country, city, or subject</label><input id="search" type="search" placeholder="UFRJ, São Paulo, Brazil, 2024" autocomplete="off" spellcheck="false"></div>
        <div class="field"><label for="country-filter">Country</label><select id="country-filter">@@COUNTRY_OPTIONS@@</select></div>
        <div class="field">
          <span class="label-with-help">
            <label for="status-filter">Status</label>
            <span class="help">
              <button class="help-trigger" type="button" aria-label="Explain 100% acceptance for unapplied years" aria-describedby="assigned-help">i</button>
              <span class="help-tooltip" id="assigned-help" role="tooltip">Years with no applications are counted as 100% acceptance because any applicant would have secured a spot.</span>
            </span>
          </span>
          <select id="status-filter">@@STATUS_OPTIONS@@</select>
        </div>
        <div class="field"><label for="roster-filter">Application availability</label><select id="roster-filter"><option value="current" selected>Current exchange partners (Release 2027)</option><option value="all">All partners (including past agreements)</option></select></div>
        <div class="field"><label for="degree-filter">Degree level</label><select id="degree-filter"><option value="">All degree levels</option><option value="bachelor">Bachelor</option><option value="master">Master</option><option value="unknown">Unknown</option></select></div>
        <div class="field"><label for="subject-filter">Subject area</label><select id="subject-filter">@@SUBJECT_OPTIONS@@</select></div>
        <button class="reset-button" type="button" id="reset-filters">Reset filters</button>
        <div class="field year-filter-section" role="group" aria-labelledby="year-filter-heading">
          <div class="year-filter-header">
            <span id="year-filter-heading" class="year-filter-label">Years included in average calculation</span>
            <div class="year-filter-presets">
              <button type="button" class="preset-btn" id="preset-core" title="Reset to standard 5-year core window (2022–2026)">Core (2022–2026)</button>
              <button type="button" class="preset-btn" id="preset-all" title="Include all 10 historical years (2017–2026)">All Years (2017–2026)</button>
              <button type="button" class="preset-btn" id="preset-recent" title="Focus strictly on latest 3 observation years (2024–2026)">Recent 3y (2024–2026)</button>
            </div>
          </div>
          <div class="year-pills" id="year-pills" aria-label="Toggle observation years in average calculation">
            <button type="button" class="year-pill" data-year="2026" aria-pressed="true"><span class="year-check">✓</span> 2026</button>
            <button type="button" class="year-pill" data-year="2025" aria-pressed="true"><span class="year-check">✓</span> 2025</button>
            <button type="button" class="year-pill" data-year="2024" aria-pressed="true"><span class="year-check">✓</span> 2024</button>
            <button type="button" class="year-pill" data-year="2023" aria-pressed="true"><span class="year-check">✓</span> 2023</button>
            <button type="button" class="year-pill" data-year="2022" aria-pressed="true"><span class="year-check">✓</span> 2022</button>
            <button type="button" class="year-pill" data-year="2021" aria-pressed="false"><span class="year-check">✓</span> 2021 <span class="year-era-tag">Archive</span></button>
            <button type="button" class="year-pill" data-year="2020" aria-pressed="false"><span class="year-check">✓</span> 2020 <span class="year-era-tag">Archive</span></button>
            <button type="button" class="year-pill" data-year="2019" aria-pressed="false"><span class="year-check">✓</span> 2019 <span class="year-era-tag">Archive</span></button>
            <button type="button" class="year-pill" data-year="2018" aria-pressed="false"><span class="year-check">✓</span> 2018 <span class="year-era-tag">Archive</span></button>
            <button type="button" class="year-pill" data-year="2017" aria-pressed="false"><span class="year-check">✓</span> 2017 <span class="year-era-tag">Archive</span></button>
          </div>
        </div>
      </div>
      <div class="result-line" aria-live="polite"><span id="result-count"><strong>@@ACTIVE_UNIVERSITIES@@</strong> active partner universities (Release 2027)</span><span>Averages reflect years with active exchange data</span></div>
      <div class="mobile-sort" role="group" aria-label="Sort this list">
        <span class="mobile-sort-label">Sort</span>
        <button type="button" class="mobile-sort-button" data-sort-column="university" aria-pressed="false">University<span class="sort-indicator" aria-hidden="true"></span></button>
        <button type="button" class="mobile-sort-button" data-sort-column="country" aria-pressed="false">Country<span class="sort-indicator" aria-hidden="true"></span></button>
        <button type="button" class="mobile-sort-button" data-sort-column="scored" aria-pressed="false">Years recorded<span class="sort-indicator" aria-hidden="true"></span></button>
        <button type="button" class="mobile-sort-button" data-sort-column="score" aria-pressed="false">Average acceptance<span class="sort-indicator" aria-hidden="true"></span></button>
      </div>
    </section>
    <div class="table-frame">
      <table id="report-table">
        <caption class="visually-hidden">TUMexchange universities with historical acceptance rates and partner requirements</caption>
        <thead><tr>
          <th scope="col" class="sortable" data-sort-column="university" aria-sort="none"><button type="button" class="sort-button">University<span class="sort-indicator" aria-hidden="true"></span></button></th>
          <th scope="col" class="sortable" data-sort-column="country" aria-sort="none"><button type="button" class="sort-button">Country<span class="sort-indicator" aria-hidden="true"></span></button></th>
          <th scope="col" class="sortable" data-sort-column="scored" aria-sort="none"><button type="button" class="sort-button">Years recorded<span class="sort-indicator" aria-hidden="true"></span></button></th>
          <th scope="col" class="sortable" data-sort-column="score" aria-sort="none"><button type="button" class="sort-button">Average acceptance<span class="sort-indicator" aria-hidden="true"></span></button></th>
          <th scope="col"><span class="visually-hidden">Details</span></th>
        </tr></thead>
        <tbody id="rows-body">@@INITIAL_ROWS@@</tbody>
      </table>
    </div>
    <footer><p>TUMexchange acceptance data compiled from official TUM publication releases.</p><p><a href="rows.csv" download>Download complete CSV dataset</a></p></footer>
  </div>
  <script id="report-data" type="application/json">@@REPORT_DATA@@</script>
  <script>
    (() => {
      "use strict";
      const dataElement = document.getElementById("report-data");
      const data = JSON.parse(dataElement.textContent || "{\"groups\":[]}");
      const groups = Array.isArray(data.groups) ? data.groups : [];
      const body = document.getElementById("rows-body");
      const search = document.getElementById("search");
      const countryFilter = document.getElementById("country-filter");
      const statusFilter = document.getElementById("status-filter");
      const rosterFilter = document.getElementById("roster-filter");
      const degreeFilter = document.getElementById("degree-filter");
      const subjectFilter = document.getElementById("subject-filter");
      const resetButton = document.getElementById("reset-filters");
      const resultCount = document.getElementById("result-count");
      const sortHeaders = Array.from(document.querySelectorAll("#report-table thead th[data-sort-column]"));
      const mobileSortButtons = Array.from(document.querySelectorAll(".mobile-sort [data-sort-column]"));
      const sortColumns = {
        university: { asc: "university-asc", desc: "university-desc", first: "asc" },
        country: { asc: "country-asc", desc: "country-desc", first: "asc" },
        scored: { asc: "scored-asc", desc: "scored-desc", first: "desc" },
        score: { asc: "score-asc", desc: "score-desc", first: "desc" }
      };
      const allAvailableYears = new Set();
      groups.forEach((g) => (g.records || []).forEach((r) => { if (r.year !== null && r.year !== undefined) allAvailableYears.add(Number(r.year)); }));
      const defaultCore = [2026, 2025, 2024, 2023, 2022];
      const hasCoreYears = defaultCore.some((y) => allAvailableYears.has(y));
      const initialYears = hasCoreYears ? new Set(defaultCore) : new Set(allAvailableYears);
      const state = { search: "", country: "", status: "", roster: "current", degree: "", subject: "", sort: "source", years: initialYears };
      const text = (value) => value === null || value === undefined ? "" : String(value);
      const numeric = (value) => {
        if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : null;
      };
      const summaryRows = new Map(Array.from(body.querySelectorAll("tr.university-row")).map((row) => [Number(row.dataset.groupIndex), row]));
      const detailRows = new Map(Array.from(body.querySelectorAll("tr.university-detail-row")).map((row) => [Number(row.dataset.groupDetail), row]));
      const foldSearch = (value) => text(value).normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
      const searchTokens = () => foldSearch(state.search).split(/\s+/).filter(Boolean);
      const searchMatches = (group) => {
        const tokens = searchTokens();
        if (!tokens.length) return true;
        const haystack = foldSearch(group.search_text);
        return tokens.every((token) => haystack.includes(token));
      };
      const profileMatches = (group) => {
        if (!state.degree && !state.subject) return true;
        const profiles = group.profiles || [];
        if (!profiles.length) {
          const degreeOk = !state.degree || (group.profile_levels || []).includes(state.degree);
          const subjectOk = !state.subject || (group.profile_subjects || []).includes(state.subject);
          return degreeOk && subjectOk;
        }
        return profiles.some((profile) => {
          const degreeMatches = !state.degree || (profile.levels || []).includes(state.degree);
          const subjectMatches = !state.subject || (profile.subject_areas || []).includes(state.subject);
          return degreeMatches && subjectMatches;
        });
      };
      const getActiveRecords = (group) => {
        return (group.records || []).filter((r) => r.year !== null && r.year !== undefined && state.years.has(Number(r.year)));
      };
      const computeGroupMetrics = (group) => {
        const active = getActiveRecords(group);
        const scores = [];
        const categories = new Set();
        for (const r of active) {
          const s = text(r.status).toLowerCase().replace(/[\s-]+/g, "_");
          if (s === "measured") {
            categories.add("measured");
            const val = numeric(r.score_percent);
            if (val !== null) scores.push(Math.max(0, Math.min(100, val)));
          } else if (s === "no_applications") {
            categories.add("assigned");
            const val = numeric(r.score_percent);
            if (val !== null) scores.push(Math.max(0, Math.min(100, val)));
          } else if (s === "ambiguous") {
            categories.add("ambiguous");
          } else {
            categories.add("na");
          }
        }
        const mean = scores.length > 0 ? scores.reduce((a, b) => a + b, 0) / scores.length : null;
        return {
          score: mean,
          scored_count: scores.length,
          total_count: active.length,
          categories: categories,
        };
      };
      const updateGroupScores = () => {
        groups.forEach((group) => {
          const metrics = computeGroupMetrics(group);
          group.current_score = metrics.score;
          group.current_scored_count = metrics.scored_count;
          group.current_total_count = metrics.total_count;
          group.current_categories = metrics.categories;

          const index = Number(group.index);
          const summary = summaryRows.get(index);
          const detail = detailRows.get(index);

          if (summary) {
            const yearsEl = summary.querySelector(".years-cell");
            if (yearsEl) {
              yearsEl.innerHTML = "<strong>" + metrics.scored_count + " of " + metrics.total_count + "</strong><span>years recorded</span>";
            }
            const scoreValEl = summary.querySelector(".score-value");
            const scoreUnitEl = summary.querySelector(".score-unit");
            const scoreTrack = summary.querySelector(".score-track");
            const scoreFill = summary.querySelector(".score-fill");
            if (metrics.score !== null) {
              const label = metrics.score.toFixed(1) + "%";
              if (scoreValEl) scoreValEl.textContent = label;
              if (scoreUnitEl) scoreUnitEl.textContent = "avg acceptance";
              if (scoreTrack) {
                scoreTrack.classList.remove("is-empty");
                scoreTrack.setAttribute("aria-label", label + " average acceptance");
              }
              if (scoreFill) scoreFill.style.width = metrics.score.toFixed(4) + "%";
            } else {
              if (scoreValEl) scoreValEl.textContent = "N/A";
              if (scoreUnitEl) scoreUnitEl.textContent = "no recorded data";
              if (scoreTrack) {
                scoreTrack.classList.add("is-empty");
                scoreTrack.setAttribute("aria-label", "N/A; no recorded data");
              }
              if (scoreFill) scoreFill.style.width = "0%";
            }
          }

          if (detail) {
            const headingSpan = detail.querySelector(".annual-count-badge, .annual-heading span");
            if (headingSpan) {
              headingSpan.textContent = metrics.scored_count + " of " + metrics.total_count + " years with active exchange data";
            }
            detail.querySelectorAll("tr.annual-data-row").forEach((row) => {
              const rowYear = Number(row.dataset.year);
              const isActive = state.years.has(rowYear);
              row.classList.toggle("is-year-inactive", !isActive);
            });
          }
        });
      };
      const matches = (group) => {
        if (state.roster === "current" && !group.is_latest_roster) return false;
        if (state.country && text(group.country) !== state.country) return false;
        if (state.status) {
          const cats = group.current_categories || group.categories || [];
          const hasCat = cats instanceof Set ? cats.has(state.status) : cats.includes(state.status);
          if (!hasCat) return false;
        }
        return searchMatches(group) && profileMatches(group);
      };
      const compareNullable = (left, right, descending) => {
        if (left === null && right !== null) return 1;
        if (left !== null && right === null) return -1;
        if (left !== null && right !== null && left !== right) {
          return descending ? (left < right ? 1 : -1) : (left > right ? 1 : -1);
        }
        return 0;
      };
      const compareGroups = (left, right, sortKey) => {
        const sourceOrder = Number(left.index) - Number(right.index);
        if (!sortKey || sortKey === "source") return sourceOrder;
        if (sortKey === "university-asc") return text(left.university).localeCompare(text(right.university)) || sourceOrder;
        if (sortKey === "university-desc") return text(right.university).localeCompare(text(left.university)) || sourceOrder;
        if (sortKey === "country-asc") return text(left.country).localeCompare(text(right.country)) || text(left.university).localeCompare(text(right.university)) || sourceOrder;
        if (sortKey === "country-desc") return text(right.country).localeCompare(text(left.country)) || text(left.university).localeCompare(text(right.university)) || sourceOrder;
        const leftScored = left.current_scored_count !== undefined ? left.current_scored_count : left.scored_count;
        const rightScored = right.current_scored_count !== undefined ? right.current_scored_count : right.scored_count;
        if (sortKey === "scored-desc") return compareNullable(numeric(leftScored), numeric(rightScored), true) || text(left.university).localeCompare(text(right.university)) || sourceOrder;
        if (sortKey === "scored-asc") return compareNullable(numeric(leftScored), numeric(rightScored), false) || text(left.university).localeCompare(text(right.university)) || sourceOrder;
        const leftScore = left.current_score !== undefined ? left.current_score : left.mean_score;
        const rightScore = right.current_score !== undefined ? right.current_score : right.mean_score;
        if (sortKey === "score-desc") return compareNullable(numeric(leftScore), numeric(rightScore), true) || text(left.university).localeCompare(text(right.university)) || sourceOrder;
        if (sortKey === "score-asc") return compareNullable(numeric(leftScore), numeric(rightScore), false) || text(left.university).localeCompare(text(right.university)) || sourceOrder;
        return sourceOrder;
      };
      const orderedGroups = () => {
        const copy = groups.slice();
        copy.sort((left, right) => compareGroups(left, right, state.sort));
        return copy;
      };
      const syncSortHeaders = () => {
        sortHeaders.forEach((th) => {
          const column = th.dataset.sortColumn;
          const config = sortColumns[column];
          if (!config) return;
          if (state.sort === config.asc) {
            th.setAttribute("aria-sort", "ascending");
          } else if (state.sort === config.desc) {
            th.setAttribute("aria-sort", "descending");
          } else {
            th.setAttribute("aria-sort", "none");
          }
        });
        mobileSortButtons.forEach((button) => {
          const column = button.dataset.sortColumn;
          const config = sortColumns[column];
          const indicator = button.querySelector(".sort-indicator");
          const active = Boolean(config && (state.sort === config.asc || state.sort === config.desc));
          button.classList.toggle("is-active", active);
          button.setAttribute("aria-pressed", String(active));
          if (!indicator) return;
          if (state.sort === config.asc) {
            indicator.textContent = "▲";
          } else if (state.sort === config.desc) {
            indicator.textContent = "▼";
          } else {
            indicator.textContent = "";
          }
        });
      };
      const cycleSort = (column) => {
        const config = sortColumns[column];
        if (!config) return;
        const first = config[config.first];
        const second = config.first === "asc" ? config.desc : config.asc;
        state.sort = state.sort === first ? second : first;
        render();
      };
      const setExpanded = (button, detail, expanded) => {
        detail.hidden = !expanded;
        const detailId = detail.id;
        const controls = detailId
          ? document.querySelectorAll('button[aria-controls="' + detailId + '"]')
          : [button];
        controls.forEach((item) => {
          item.setAttribute("aria-expanded", String(expanded));
          if (item.classList.contains("university-detail-toggle")) {
            item.textContent = expanded ? "Hide profile & history" : "View profile & history";
          } else if (item.classList.contains("university-toggle")) {
            const label = item.querySelector(".expand-label");
            if (label) label.textContent = expanded ? "Hide profile & history" : "View profile & history";
          } else {
            item.textContent = expanded ? "Hide details" : "View details";
          }
        });
        const groupIndex = detail.dataset.groupDetail;
        const summary = groupIndex !== undefined ? summaryRows.get(Number(groupIndex)) : null;
        if (summary) {
          summary.classList.toggle("is-expanded", expanded);
        }
        detail.classList.toggle("is-expanded", expanded);
      };
      const toggleButton = (button) => {
        const detailId = button.getAttribute("aria-controls");
        const detail = detailId ? document.getElementById(detailId) : null;
        if (!detail) return;
        setExpanded(button, detail, button.getAttribute("aria-expanded") !== "true");
      };
      const ensureEmptyRow = (show) => {
        let row = document.getElementById("empty-state-row");
        if (!show) {
          if (row) row.remove();
          return;
        }
        if (!row) {
          row = document.createElement("tr");
          row.id = "empty-state-row";
          row.className = "empty-row";
          row.innerHTML = "<td colspan=\"5\"><div class=\"empty-state\"><strong></strong><p></p><button class=\"reset-button\" type=\"button\" data-empty-reset>Reset filters</button></div></td>";
          body.appendChild(row);
        }
        const title = row.querySelector("strong");
        const hint = row.querySelector("p");
        const noData = groups.length === 0;
        if (title) title.textContent = noData ? "No extracted rows yet" : "No universities match these filters";
        if (hint) hint.textContent = noData ? "Run the extractor to populate this report." : "Clear a filter or reset the search to show all universities.";
      };
      const render = () => {
        const visible = groups.filter(matches);
        const visibleIndexes = new Set(visible.map((group) => Number(group.index)));
        const fragment = document.createDocumentFragment();
        orderedGroups().forEach((group) => {
          const index = Number(group.index);
          const summary = summaryRows.get(index);
          const detail = detailRows.get(index);
          if (!summary || !detail) return;
          const shown = visibleIndexes.has(index);
          summary.hidden = !shown;
          if (!shown) {
            detail.hidden = true;
            summary.classList.remove("is-expanded");
            detail.classList.remove("is-expanded");
          }
          fragment.append(summary, detail);
        });
        body.appendChild(fragment);
        ensureEmptyRow(visible.length === 0);
        if (state.roster === "current") {
          resultCount.innerHTML = "<strong>" + visible.length + "</strong> active partner universit" + (visible.length === 1 ? "y" : "ies") + " (Release 2027)";
        } else {
          resultCount.innerHTML = "<strong>" + visible.length + "</strong> partner universit" + (visible.length === 1 ? "y" : "ies") + " shown";
        }
        syncSortHeaders();
      };
      const yearPills = Array.from(document.querySelectorAll(".year-pill"));
      const presetCore = document.getElementById("preset-core");
      const presetAll = document.getElementById("preset-all");
      const presetRecent = document.getElementById("preset-recent");
      const updateYearPillsUI = () => {
        yearPills.forEach((pill) => {
          const y = Number(pill.dataset.year);
          const active = state.years.has(y);
          pill.setAttribute("aria-pressed", String(active));
        });
        const hasArchiveYear = Array.from(state.years).some((y) => y < 2022);
        if (hasArchiveYear) {
          document.querySelectorAll(".historical-archive-disclosure").forEach((details) => {
            details.open = true;
          });
        }
      };
      const setYears = (newYears) => {
        if (!newYears.size) return;
        state.years = newYears;
        updateYearPillsUI();
        updateGroupScores();
        render();
      };
      yearPills.forEach((pill) => {
        pill.addEventListener("click", () => {
          const y = Number(pill.dataset.year);
          const nextYears = new Set(state.years);
          if (nextYears.has(y)) {
            if (nextYears.size > 1) nextYears.delete(y);
          } else {
            nextYears.add(y);
          }
          setYears(nextYears);
        });
      });
      if (presetCore) {
        presetCore.addEventListener("click", () => {
          setYears(new Set([2026, 2025, 2024, 2023, 2022]));
        });
      }
      if (presetAll) {
        presetAll.addEventListener("click", () => {
          setYears(new Set([2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017]));
        });
      }
      if (presetRecent) {
        presetRecent.addEventListener("click", () => {
          setYears(new Set([2026, 2025, 2024]));
        });
      }
      const reset = () => {
        state.search = "";
        state.country = "";
        state.status = "";
        state.roster = "current";
        state.degree = "";
        state.subject = "";
        state.sort = "source";
        state.years = hasCoreYears ? new Set(defaultCore) : new Set(allAvailableYears);
        search.value = "";
        countryFilter.value = "";
        statusFilter.value = "";
        if (rosterFilter) rosterFilter.value = "current";
        degreeFilter.value = "";
        subjectFilter.value = "";
        updateYearPillsUI();
        updateGroupScores();
        render();
        search.focus();
      };
      search.addEventListener("input", () => { state.search = search.value; render(); });
      countryFilter.addEventListener("change", () => { state.country = countryFilter.value; render(); });
      statusFilter.addEventListener("change", () => { state.status = statusFilter.value; render(); });
      if (rosterFilter) rosterFilter.addEventListener("change", () => { state.roster = rosterFilter.value; render(); });
      degreeFilter.addEventListener("change", () => { state.degree = degreeFilter.value; render(); });
      subjectFilter.addEventListener("change", () => { state.subject = subjectFilter.value; render(); });
      document.querySelector("#report-table thead").addEventListener("mousedown", (event) => {
        if (event.target.closest(".sort-button")) event.preventDefault();
      });
      document.querySelector("#report-table thead").addEventListener("click", (event) => {
        const header = event.target.closest("th[data-sort-column]");
        if (header) cycleSort(header.dataset.sortColumn);
      });
      document.querySelector(".mobile-sort").addEventListener("click", (event) => {
        const button = event.target.closest("[data-sort-column]");
        if (button) cycleSort(button.dataset.sortColumn);
      });
      resetButton.addEventListener("click", reset);
      const tableFrame = document.querySelector(".table-frame");
      const toggleFromRow = (row, selector) => {
        const button = row.querySelector(selector);
        if (button) toggleButton(button);
      };
      tableFrame.addEventListener("click", (event) => {
        const emptyReset = event.target.closest("[data-empty-reset]");
        if (emptyReset) { reset(); return; }
        if (event.target.closest("a, summary, .help-trigger, .sort-button")) return;
        const universityRow = event.target.closest("tr.university-row");
        if (universityRow) toggleFromRow(universityRow, ".university-toggle, .university-detail-toggle");
      });
      document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        if (state.search) {
          event.preventDefault();
          search.value = "";
          state.search = "";
          render();
          search.focus();
          return;
        }
        body.querySelectorAll("button[aria-expanded=\"true\"]").forEach((button) => {
          const detailId = button.getAttribute("aria-controls");
          const detail = detailId ? document.getElementById(detailId) : null;
          if (detail) setExpanded(button, detail, false);
        });
      });
      updateYearPillsUI();
      updateGroupScores();
      render();
    })();
  </script>
</body>
</html>
"""
    active_groups_count = sum(1 for g in groups if g.get("is_latest_roster"))
    replacements = {
        "@@ACTIVE_UNIVERSITIES@@": str(active_groups_count),
        "@@UNIVERSITIES@@": str(len(groups)),
        "@@COUNTRIES@@": str(len(countries)),
        "@@YEAR_SPAN@@": year_span,
        "@@COUNTRY_OPTIONS@@": _select_options(groups, "country", "All countries"),
        "@@STATUS_OPTIONS@@": _select_options(current_records, "status", "All statuses"),
        "@@SUBJECT_OPTIONS@@": profile_subject_options,
        "@@INITIAL_ROWS@@": initial_rows,
        "@@REPORT_DATA@@": _safe_json(data_payload),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return "\n".join(line.rstrip() for line in template.split("\n"))


def write_report(
    records: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    calibration: Any,
    output_path: Path,
    *,
    profiles: Any = None,
) -> None:
    """Write only the self-contained HTML report.

    CSV, JSON, and SQLite exports are owned by the storage/CLI layer.  This
    function intentionally does not mutate records or replace adjacent files.
    """

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(records, Mapping):
        payload_records = records.get("records", [])
        if not isinstance(payload_records, Sequence) or isinstance(
            payload_records, (str, bytes)
        ):
            payload_records = []
        if calibration is None and isinstance(records.get("calibration"), Mapping):
            calibration = records["calibration"]
        records = payload_records
    output_path.write_text(_build_html(records, calibration, profiles), encoding="utf-8")


__all__ = ["write_report"]
