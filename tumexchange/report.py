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


def _mark_offers_places(
    groups: Sequence[Mapping[str, Any]], *, from_moveon: bool
) -> list[dict[str, Any]]:
    """A MoveON listing means places are open this year.

    Reports built without a partner catalog keep the chances-PDF roster.
    """

    marked: list[dict[str, Any]] = []
    for group in groups:
        item = dict(group)
        item["checked_against_moveon"] = from_moveon
        if from_moveon:
            item["offers_places"] = bool(item.get("profiles"))
        else:
            item["offers_places"] = bool(item.get("is_latest_roster", True))
        marked.append(item)
    return marked


def _attach_profile_catalog(groups: Sequence[Mapping[str, Any]], catalog: Any) -> list[dict[str, Any]]:
    """Attach profile relations while keeping the renderer tolerant of no catalog."""

    if catalog is None:
        return _mark_offers_places(
            [_profile_group_defaults(group) for group in groups],
            from_moveon=False,
        )
    attached = attach_profiles(groups, catalog)
    meta = _profile_catalog_meta(catalog)
    return _mark_offers_places(
        [_profile_group_defaults(group, meta) for group in attached],
        from_moveon=True,
    )


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
        <td class="score-cell"><div class="score-stack"><div class="score-line"><span class="score-value">{score_label}</span></div><div role="img" class="score-track{' is-empty' if score is None else ''}" aria-label="{html.escape(track_label, quote=True)}"><span class="score-fill" style="width: {_score_for_width(score):.4f}%"></span></div></div></td>
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
            f'<span class="basis-badge basis-assigned" tabindex="0" role="note" title="No first-round wishes. Shown as a full bar because spots were left over.">No applicants'
            f'<span class="basis-tooltip" role="tooltip"><strong class="basis-tooltip-title">No first-round wishes</strong><span class="basis-tooltip-body">TUM recorded no wishes that year, so the bar is full: spots were left over.</span></span></span></div>'
            f'<div role="img" class="score-track" aria-label="{html.escape(track_text, quote=True)}">'
            f'<span class="score-fill" style="width: 100%"></span></div></div>'
        )

    if score is None or category == "na":
        return (
            f'<div class="score-stack">'
            f'<div class="score-line"><span class="score-na">—</span></div>'
            f'<div role="img" class="score-track is-empty" aria-label="{html.escape(track_text, quote=True)}"></div></div>'
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
            f'<div role="img" class="score-track is-tier" aria-label="{html.escape(track_text, quote=True)}">'
            f'<span class="score-fill tier-{tier}-fill" style="width: {_score_for_width(score):.4f}%"></span></div></div>'
        )

    # 2. Exact Decimal Percentage
    if score_basis == "exact_percentage":
        return (
            f'<div class="score-stack">'
            f'<div class="score-line"><span class="score-value">{score:.1f}%</span>'
            f'<span class="basis-badge basis-exact" tabindex="0" role="note" title="Exact % (Release 2017): Spots divided by first-round wishes, published as a number in the 2016/17 release.">Exact %'
            f'<span class="basis-tooltip" role="tooltip"><strong class="basis-tooltip-title">Exact published % (Release 2017)</strong><span class="basis-tooltip-body">Spots divided by first-round wishes, published as a number in TUM’s 2016/17 release.</span></span></span></div>'
            f'<div role="img" class="score-track is-exact" aria-label="{html.escape(track_text, quote=True)}">'
            f'<span class="score-fill is-exact-fill" style="width: {_score_for_width(score):.4f}%"></span></div></div>'
        )

    # 3. Continuous Vector / Raster Bar
    return (
        f'<div class="score-stack">'
        f'<div class="score-line"><span class="score-value">{score:.1f}%</span>'
        f'<span class="basis-badge basis-bar" tabindex="0" role="note" title="Visual bar (Releases 2025–2027): Spots divided by first-round wishes, measured from the length of TUM\'s bar.">Visual Bar'
        f'<span class="basis-tooltip" role="tooltip"><strong class="basis-tooltip-title">Visual bar (Releases 2025–2027)</strong><span class="basis-tooltip-body">Spots divided by first-round wishes, measured from the length of TUM’s bar.</span></span></span></div>'
        f'<div role="img" class="score-track" aria-label="{html.escape(track_text, quote=True)}">'
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
        f"{score_label}, {_wishes_label(score)}"
        if score is not None
        else "N/A; no active exchange"
    )
    release = _release_year(normalized)
    release_text = f"Release {release}" if release is not None else "—"
    score_content = _render_score_display(normalized, score, track_text)
    return f"""
          <tr class="annual-data-row" data-annual-index="{annual_index}" data-year="{year}">
            <td class="numeric annual-year-cell" data-label="Year">{_escaped(year)}</td>
            <td class="score-cell annual-score-cell" data-label="Spots to wishes">{score_content}</td>
            <td class="annual-status-cell" data-label="Status">{status_html}</td>
            <td class="source-cell annual-source-cell" data-label="Source"><span class="source-release">{html.escape(release_text, quote=True)}</span></td>
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
    elif group.get("checked_against_moveon"):
        pieces.append('<span class="profile-missing">Not on MoveON</span>')
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
        if group.get("checked_against_moveon"):
            heading = "Not on MoveON"
            copy = "This university is not on the current MoveON list, so there are no places to apply this year."
        else:
            heading = "No profile data available"
            copy = "This institution is retained in the historical comparison, but no structured MoveON profile is available in the current catalogue."
        return f'''
          <section class="profile-panel profile-panel-empty" aria-label="University profile">
            <div class="profile-panel-heading">
              <div><span class="panel-eyebrow">PARTNER PROFILE &amp; REQUIREMENTS</span><h3>{html.escape(heading, quote=True)}</h3></div>
            </div>
            <p class="profile-empty-copy">{html.escape(copy, quote=True)}</p>
            {_render_further_panel(group)}
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
          {_render_further_panel(group)}
        </section>
    '''


_PARENTHETICAL_RE = re.compile(r"\s*\([^)]*\)")
_CAMPUS_PIN_TABLE: dict[str, list[dict[str, Any]]] | None = None


def _map_search_name(university: str) -> str:
    """Drop parenthetical qualifiers so a campus search uses the institution name."""

    cleaned = _PARENTHETICAL_RE.sub(" ", university)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")
    return cleaned or university.strip()


def _campus_pin_table() -> Mapping[str, list[dict[str, Any]]]:
    """Faculty campuses with checked coordinates, keyed by MoveON relation id."""

    global _CAMPUS_PIN_TABLE
    if _CAMPUS_PIN_TABLE is None:
        path = Path(__file__).resolve().parents[1] / "data" / "source" / "moveon" / "campuses.json"
        table: dict[str, list[dict[str, Any]]] = {}
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            raw = payload.get("by_relation_id") if isinstance(payload, dict) else None
            if isinstance(raw, dict):
                table = {
                    str(key): [item for item in value if isinstance(item, dict)]
                    for key, value in raw.items()
                    if isinstance(value, list)
                }
        _CAMPUS_PIN_TABLE = table
    return _CAMPUS_PIN_TABLE


def _profile_field_text(profile: Mapping[str, Any], label: str) -> str:
    for field in profile.get("fields") or []:
        if isinstance(field, Mapping) and _text(field.get("label")).strip() == label:
            return _text(field.get("text")).strip()
    return ""


def _split_campus_line(line: str) -> tuple[str, str]:
    if ":" in line:
        left, right = line.split(":", 1)
        if 2 <= len(left.strip()) <= 72:
            return left.strip(), right.strip()
    match = re.match(r"^(.*?)\s+\((.+)\)\s*$", line)
    if match and len(match.group(1).strip()) >= 2:
        return match.group(1).strip(), match.group(2).strip()
    return line, ""


def _profile_campus_lines(profile: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for raw in _profile_field_text(profile, "Campus").replace("||", "\n").splitlines():
        line = raw.strip(" •\t-")
        if not line:
            continue
        folded = line.casefold()
        if folded.startswith("students are affiliated") or folded.startswith("for rmit"):
            continue
        if len(line) > 140:
            continue
        lines.append(line)
    return lines


def _map_place(
    *,
    name: str,
    label: str,
    meta: str,
    tag: str,
    query: str,
    fallback: str,
    address: str = "",
    lat: Any = None,
    lon: Any = None,
) -> dict[str, Any]:
    place: dict[str, Any] = {
        "name": name or label,
        "label": label or name,
        "meta": meta,
        "tag": tag,
        "query": query or name or label,
        "fallback": fallback or query or name,
        "address": address,
    }
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        if math.isfinite(lat) and math.isfinite(lon):
            place["lat"] = float(lat)
            place["lon"] = float(lon)
    return place


def _map_places(group: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One map place per campus. Faculty splits stay on separate pins."""

    university = _text(group.get("university")).strip() or "University"
    country = _text(group.get("country")).strip()
    search_name = _map_search_name(university)
    profiles = [
        profile
        for profile in (group.get("profiles") or [])
        if isinstance(profile, Mapping)
    ]
    places: list[dict[str, Any]] = []
    seen: set[str] = set()
    table = _campus_pin_table()

    def add(place: dict[str, Any]) -> None:
        key = place["label"].casefold()
        if key in seen:
            return
        seen.add(key)
        places.append(place)

    for profile in profiles:
        relation_id = _text(profile.get("relation_id")).strip()
        city = _text(profile.get("city")).strip()
        place_country = _text(profile.get("country")).strip() or country
        for pin in table.get(relation_id) or []:
            label = _text(pin.get("label") or pin.get("name")).strip() or search_name
            campus_name = _text(pin.get("name")).strip() or label
            who = _text(pin.get("who")).strip()
            tag = _text(pin.get("tag")).strip()
            address = _text(pin.get("address")).strip()
            query = _text(pin.get("query")).strip() or ", ".join(
                part for part in (search_name, campus_name, address, place_country) if part
            )
            add(
                _map_place(
                    name=campus_name,
                    label=label,
                    meta=who,
                    tag=tag,
                    query=query,
                    fallback=", ".join(part for part in (city, place_country) if part) or campus_name,
                    address=address,
                    lat=pin.get("lat"),
                    lon=pin.get("lon"),
                )
            )
    if places:
        return places

    for profile in profiles:
        city = _text(profile.get("city")).strip()
        place_country = _text(profile.get("country")).strip() or country
        lines = _profile_campus_lines(profile)
        if len(lines) < 2:
            continue
        for line in lines:
            label, tag = _split_campus_line(line)
            query = ", ".join(
                part for part in (search_name, line, city, place_country) if part
            )
            add(
                _map_place(
                    name=label,
                    label=label,
                    meta=tag,
                    tag=tag,
                    query=query,
                    fallback=", ".join(part for part in (city, place_country) if part) or label,
                )
            )
    if places:
        return places

    if profiles:
        seen_cities: set[tuple[str, str]] = set()
        for profile in profiles:
            city = _text(profile.get("city")).strip()
            place_country = _text(profile.get("country")).strip() or country
            key = (city.casefold(), place_country.casefold())
            if key in seen_cities:
                continue
            seen_cities.add(key)
            label = city or place_country or search_name
            add(
                _map_place(
                    name=label,
                    label=label,
                    meta=place_country if city else "",
                    tag="",
                    query=", ".join(part for part in (search_name, city, place_country) if part),
                    fallback=", ".join(part for part in (city, place_country) if part) or search_name,
                )
            )
        return places

    label = country or search_name
    return [
        _map_place(
            name=label,
            label=label,
            meta="",
            tag="",
            query=", ".join(part for part in (search_name, country) if part) or search_name,
            fallback=country or search_name,
        )
    ]


def _render_map_panel(group: Mapping[str, Any], group_index: int) -> str:
    """Collapsed campus location. Tiles and named pins load only after Show map."""

    places = _map_places(group)
    frame_id = f"campus-map-{group_index}"
    places_attr = html.escape(_safe_json(places), quote=True)
    current = places[0]
    meta = _text(current.get("meta")).strip()
    meta_html = (
        f'<p class="location-meta">{html.escape(meta, quote=True)}</p>'
        if meta
        else '<p class="location-meta" hidden></p>'
    )
    chips = ""
    if len(places) > 1:
        buttons = []
        for index, place in enumerate(places):
            current_class = " is-current" if index == 0 else ""
            pressed = "true" if index == 0 else "false"
            buttons.append(
                f'<button type="button" class="map-place{current_class}" '
                f'data-map-index="{index}" aria-pressed="{pressed}">'
                f'{html.escape(_text(place.get("label")), quote=True)}</button>'
            )
        chips = (
            '<div class="map-places" role="group" aria-label="Campuses">'
            + "".join(buttons)
            + "</div>"
        )
    pin = (
        '<svg class="location-pin" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">'
        '<path fill="currentColor" d="M12 2.2a6.8 6.8 0 0 0-6.8 6.8c0 5.1 6.8 12.8 6.8 12.8s6.8-7.7 6.8-12.8A6.8 6.8 0 0 0 12 2.2zm0 9.2a2.4 2.4 0 1 1 0-4.8 2.4 2.4 0 0 1 0 4.8z"/>'
        "</svg>"
    )
    fit_hidden = " hidden"
    return f'''
        <section class="location-panel" data-map-places="{places_attr}" aria-label="Campus location">
          <div class="location-bar">
            <div class="location-copy">
              {pin}
              <div class="location-text">
                <p class="location-name">{html.escape(_text(current.get("name")), quote=True)}</p>
                {meta_html}
              </div>
            </div>
            <button class="map-toggle" type="button" aria-expanded="false" aria-controls="{frame_id}">Show map</button>
          </div>
          {chips}
          <div id="{frame_id}" class="map-frame" hidden>
            <p class="map-status" role="status">Looking up this campus…</p>
            <div class="campus-map" hidden>
              <div class="campus-map-stage" role="group" aria-label="Named campus pins"></div>
              <div class="map-zoom">
                <button type="button" class="map-zoom-btn" data-map-zoom="in" aria-label="Zoom in">+</button>
                <button type="button" class="map-zoom-btn" data-map-zoom="out" aria-label="Zoom out">−</button>
              </div>
              <a class="map-attribution" href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">© OpenStreetMap</a>
            </div>
            <div class="map-caption">
              <p class="map-note">Campus names sit on the pins. Choose one to see who studies there.</p>
              <button type="button" class="map-fit-btn" data-map-zoom="fit"{fit_hidden}>Show all campuses</button>
              <a class="map-external" href="https://www.openstreetmap.org/" target="_blank" rel="noopener noreferrer">Open in OpenStreetMap</a>
            </div>
          </div>
        </section>
    '''


_FURTHER_INFORMATION: dict[str, Any] | None = None
_MONTHS = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _further_information() -> Mapping[str, Any]:
    """Checked ranking and experience-report snapshot, loaded once."""

    global _FURTHER_INFORMATION
    if _FURTHER_INFORMATION is None:
        path = Path(__file__).resolve().parents[1] / "data" / "source" / "further" / "information.json"
        payload: dict[str, Any] = {}
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("schema_version") == "further-information-v1":
                payload = loaded
        _FURTHER_INFORMATION = payload
    return _FURTHER_INFORMATION


def _checked_label(value: Any) -> str:
    text = _text(value).strip()
    parts = text.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return text
    year, month, day = (int(part) for part in parts)
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return text
    return f"{day} {_MONTHS[month]} {year}"


def _display_metric(value: Any) -> str:
    text = _text(value).strip()
    if text.casefold() in {"", "n/a", "na", "-", "—"}:
        return ""
    return text


def _external_anchor(url: Any, label: str) -> str:
    safe = _safe_profile_url(url)
    if not safe:
        return ""
    return (
        f'<a class="ranking-link" href="{html.escape(safe, quote=True)}" '
        f'target="_blank" rel="noopener noreferrer">{html.escape(label, quote=True)}</a>'
    )


def _places_phrase(count: int) -> str:
    if count <= 0:
        return "No reports filed"
    noun = "place" if count == 1 else "places"
    return f"at least {count} {noun}"


def _relation_hint(profile: Mapping[str, Any]) -> str:
    levels = _profile_levels(profile)
    degree = " and ".join(_PROFILE_LEVEL_LABELS[level] for level in levels)
    subjects = _profile_subjects(profile)
    subject = ""
    if subjects:
        subject = subjects[0]
        if "-" in subject:
            subject = subject.split("-", 1)[1].strip()
        subject = subject.split("/")[0].strip()
        if len(subject) > 42:
            subject = subject[:39].rstrip() + "…"
    return " · ".join(part for part in (degree, subject) if part)


def _report_link_labels(profiles: Sequence[Mapping[str, Any]]) -> list[str]:
    if len(profiles) <= 1:
        return ["Open experience reports"]
    labels = [_relation_hint(profile) or "Partner relation" for profile in profiles]
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    seen: dict[str, int] = {}
    rendered: list[str] = []
    for label in labels:
        if counts[label] > 1:
            seen[label] = seen.get(label, 0) + 1
            label = f"{label} ({seen[label]})"
        rendered.append(f"Open experience reports · {label}")
    return rendered


def _further_row(label: str, body: str) -> str:
    return (
        "<div>"
        f"<dt>{html.escape(label, quote=True)}</dt>"
        f"<dd>{body}</dd>"
        "</div>"
    )


def _fact(label: str, value: Any) -> str:
    text = _display_metric(value)
    if not text:
        return ""
    return f"{html.escape(label, quote=True)} {html.escape(text, quote=True)}"


def _render_ranking_row(source: str, entry: Any, meta: Mapping[str, Any]) -> str:
    title = _text(meta.get("name")).strip() or source.upper()
    edition = _text(meta.get("edition")).strip()
    label = f"{title} {edition}".strip()
    if not isinstance(entry, Mapping):
        link_label = "View QS rankings" if source == "qs" else "View THE rankings"
        link = _external_anchor(meta.get("table_url"), link_label)
        return _further_row(label, f"Not listed in this edition. {link}".strip())
    if source == "qs":
        facts = (
            _fact("Overall", entry.get("score")),
            _fact("Academic reputation", entry.get("academic")),
            _fact("Employer reputation", entry.get("employer")),
        )
        link_label = "View on QS"
    else:
        facts = (
            _fact("Overall", entry.get("score")),
            _fact("Teaching", entry.get("teaching")),
            _fact("Research quality", entry.get("research_quality")),
            _fact("International outlook", entry.get("international")),
        )
        link_label = "View on THE"
    link = _external_anchor(entry.get("url") or meta.get("table_url"), link_label)
    world = _text(entry.get("world")).strip()
    if world == "Reporter":
        lead = "Reporter. Submitted data, but not given a rank in this edition."
    else:
        country_rank = _text(entry.get("country_rank")).strip()
        country = _text(entry.get("country")).strip()
        place = (
            f"{html.escape(country_rank, quote=True)} in {html.escape(country, quote=True)}"
            if country_rank and country
            else ""
        )
        rank = html.escape(world or "Rank unavailable", quote=True)
        lead = f"<strong>{rank}</strong>{', ' + place if place else ''}."
    listed = _text(entry.get("listed_as")).strip()
    listed_text = f"Listed as {html.escape(listed, quote=True)}." if listed else ""
    detail = ". ".join(part for part in facts if part)
    body = " ".join(part for part in (lead, f"{detail}." if detail else "", listed_text, link) if part)
    return _further_row(label, body)


def _render_report_counts(group: Mapping[str, Any], info: Mapping[str, Any]) -> str:
    table = info.get("by_relation_id")
    if not isinstance(table, Mapping):
        return ""
    matched: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    seen: set[str] = set()
    for profile in group.get("profiles") or []:
        if not isinstance(profile, Mapping):
            continue
        relation_id = _text(profile.get("relation_id")).strip()
        if not relation_id or relation_id in seen:
            continue
        seen.add(relation_id)
        row = table.get(relation_id)
        if isinstance(row, Mapping):
            matched.append((profile, row))
    if not matched:
        return ""
    meta = info.get("reports") if isinstance(info.get("reports"), Mapping) else {}
    winter = sum(int(row.get("winter") or 0) for _profile, row in matched)
    summer = sum(int(row.get("summer") or 0) for _profile, row in matched)
    winter_label = _text(meta.get("winter_label")).strip() or "Winter semester"
    summer_label = _text(meta.get("summer_label")).strip() or "Summer semester"
    winter_window = _text(meta.get("winter_window")).strip()
    summer_window = _text(meta.get("summer_window")).strip()
    combined = (
        "These totals combine the TUM partner relations on this page."
        if len(matched) > 1
        else ""
    )
    links = []
    for (profile, row), label in zip(matched, _report_link_labels([profile for profile, _row in matched]), strict=True):
        links.append(_external_anchor(row.get("details_url"), label))

    def semester(label: str, window: str, count: int) -> str:
        when = html.escape(label, quote=True)
        if window:
            when = f"{when}, {html.escape(window, quote=True)}"
        return f"{when}: <strong>{html.escape(_places_phrase(count), quote=True)}</strong>."

    body = " ".join(
        part
        for part in (
            semester(winter_label, winter_window, winter),
            semester(summer_label, summer_window, summer),
            "Each Erfahrungsbericht was written by a student who received a place. The count is a lower bound for that semester, not the full number of places.",
            combined,
            " ".join(links),
        )
        if part
    )
    return _further_row("Experience reports", body)


def _render_further_panel(group: Mapping[str, Any]) -> str:
    """QS, THE, and last-academic-year experience reports for a known partner."""

    info = _further_information()
    universities = info.get("by_university")
    university = _text(group.get("university")).strip()
    entry = universities.get(university) if isinstance(universities, Mapping) else None
    reports = _render_report_counts(group, info)
    if not isinstance(entry, Mapping) and not reports:
        return ""
    qs_meta = info.get("qs") if isinstance(info.get("qs"), Mapping) else {}
    the_meta = info.get("the") if isinstance(info.get("the"), Mapping) else {}
    checked = _checked_label(info.get("checked"))
    note = "Rankings are independent of TUMexchange placement."
    if checked:
        note = f"Checked {html.escape(checked, quote=True)}. {note}"
    rows = ""
    if isinstance(entry, Mapping):
        rows = _render_ranking_row("qs", entry.get("qs"), qs_meta) + _render_ranking_row(
            "the", entry.get("the"), the_meta
        )
    rows += reports
    return f'''
        <div class="profile-further">
          <p class="profile-further-note">{note}</p>
          <dl class="profile-further-rows">{rows}</dl>
        </div>
    '''


def _wishes_label(score: float | None) -> str:
    """Read a bar as spots divided by first-round wishes."""

    if score is None:
        return "no recorded data"
    if score >= 99.95:
        return "at least one spot per wish"
    if score <= 0:
        return "spots to wishes"
    ratio = 100.0 / score
    if abs(ratio - round(ratio)) < 0.05:
        rendered = str(int(round(ratio)))
    elif ratio >= 10:
        rendered = f"{ratio:.0f}"
    else:
        rendered = f"{ratio:.1f}"
    return f"about {rendered} wishes per spot"


def _chances_document_note(group: Mapping[str, Any]) -> str:
    """Say the bars omit a university that is absent from the newest chances PDF."""

    if group.get("is_latest_roster", True):
        return ""
    release = group.get("latest_release_year")
    years = [
        year
        for year in (item.get("year") for item in (group.get("records") or []))
        if isinstance(year, int)
    ]
    if release is not None and years:
        text = (
            f"Not included in the {release} chances document. "
            f"The bars stop at {max(years)}."
        )
    elif release is not None:
        text = f"Not included in the {release} chances document."
    else:
        text = "Not included in the latest chances document."
    return f'<p class="chances-gap">{html.escape(text, quote=True)}</p>'


def _render_university_row(group: Mapping[str, Any], group_index: int) -> str:
    score = group.get("mean_score")
    score_label = f"{score:.1f}%" if score is not None else "N/A"
    score_unit = _wishes_label(score if isinstance(score, (int, float)) else None)
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
            '<th scope="col">Spots to wishes</th><th scope="col">Status</th>'
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
    is_current = bool(group.get("is_latest_roster", True))
    offers_places = bool(group.get("offers_places", is_current))
    hidden_attr = "" if offers_places else " hidden"
    chances_note = _chances_document_note(group)
    return f"""
      <tr class="data-row university-row" data-group-index="{group_index}"{hidden_attr}>
        <td class="university-cell" data-label="University"><button class="university-toggle" type="button" aria-expanded="false" aria-controls="{detail_id}"><span><span class="university-name">{_escaped(group.get("university"), "Unnamed university")}</span>{_render_profile_summary(group)}</span><span class="expand-label">View profile &amp; history</span></button></td>
        <td class="country-cell" data-label="Country">{_escaped(group.get("country"))}</td>
        <td class="numeric years-cell" data-label="Years recorded"><strong>{scored} of {total}</strong><span>years recorded</span></td>
        <td class="score-cell" data-label="Spots to wishes"><div class="score-stack"><div class="score-line"><span class="score-value">{score_label}</span><span class="score-unit">{score_unit}</span></div><div role="img" class="score-track{' is-empty' if score is None else ''}" aria-label="{html.escape((score_label + ', ' + score_unit) if score is not None else 'N/A; no recorded data', quote=True)}"><span class="score-fill" style="width: {_score_for_width(score):.4f}%"></span></div></div></td>
        <td class="details-cell" data-label="Profile and history"><button class="detail-toggle university-detail-toggle" type="button" aria-expanded="false" aria-controls="{detail_id}">View profile &amp; history</button></td>
      </tr>
      <tr id="{detail_id}" class="university-detail-row" data-group-detail="{group_index}" hidden><td colspan="5"><div class="detail-card-shell">{_render_map_panel(group, group_index)}{_render_profile_panel(group, group_index)}<section class="annual-panel" aria-label="Historical annual data"><div class="annual-heading"><div><span class="panel-eyebrow">HISTORICAL ADMISSION DATA</span><h3>Spots and first-round wishes by year</h3>{chances_note}</div><span class="annual-count-badge">{scored} of {total} years with active exchange data</span></div>{annual_content}</section></div></td></tr>
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
        "offers_places": bool(group.get("offers_places", group.get("is_latest_roster", True))),
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
<html lang="en" data-theme="workspace">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light">
  <title>TUMexchange Acceptance Information</title>
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
    html {
      overflow-x: clip;
      -webkit-text-size-adjust: 100%;
    }
    body {
      margin: 0; min-width: 320px; width: 100%; overflow-x: hidden; position: relative;
      background: var(--wash); line-height: 1.45;
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
      display: none;
      position: absolute; z-index: 10; top: 40px; right: 0; width: min(280px, calc(100vw - 40px));
      margin: 0; padding: 10px 11px; border: 1px solid var(--line);
      background: var(--paper); color: var(--muted); box-shadow: var(--shadow);
      font-size: .77rem; font-weight: 400; opacity: 0; pointer-events: none;
    }
    .help-trigger:hover + .help-tooltip,
    .help-trigger:focus-visible + .help-tooltip,
    .help:focus-within .help-tooltip { display: block; opacity: 1; pointer-events: auto; }
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
    .preset-short { display: none; }
    .preset-full { display: inline; }
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
      display: none;
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
      display: block;
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
    .chances-gap { margin: 8px 0 0; max-width: 62ch; color: var(--muted); font-size: .82rem; }
    .profile-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 5px 7px; margin-top: 7px; color: var(--muted); font-size: .72rem; }
    .profile-badge { display: inline-flex; align-items: center; min-height: 22px; padding: 2px 7px; border-radius: 999px; background: var(--blue-soft); color: var(--blue-deep); font-size: .68rem; font-weight: 700; white-space: nowrap; }
    .profile-badge-master { background: var(--green-soft); color: var(--green); }
    .profile-badge-unknown { background: #edf1f3; color: #52616b; }
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
    .profile-further {
      margin-top: 14px;
      padding-top: 12px;
      border-top: 1px solid var(--line);
    }
    .profile-further-note {
      max-width: 72ch;
      margin: 0 0 8px;
      color: var(--muted);
      font-size: .78rem;
    }
    .profile-further-rows { display: grid; gap: 0; margin: 0; }
    .profile-further-rows > div {
      display: grid;
      grid-template-columns: minmax(9.5rem, 14rem) minmax(0, 1fr);
      gap: 2px 16px;
      align-items: baseline;
      padding-top: 8px;
      border-top: 1px solid #e6eef5;
    }
    .profile-further-rows > div:first-child { padding-top: 0; border-top: 0; }
    .profile-further-rows dt { color: var(--muted); font-size: .72rem; font-weight: 700; }
    .profile-further-rows dd { margin: 0; color: #365264; font-size: .83rem; }
    .profile-further-rows strong { color: var(--blue-deep); font-weight: 650; }
    .ranking-link { font-weight: 650; white-space: nowrap; }
    .location-panel {
      padding: 14px 16px 14px;
      border: 1px solid #c9dceb;
      border-radius: 6px;
      background: var(--paper);
      box-shadow: 0 2px 6px rgba(0, 51, 89, 0.04);
    }
    .location-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    .location-copy {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }
    .location-pin { flex: none; color: var(--blue); }
    .location-text { min-width: 0; }
    .location-name {
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      font-size: 1.15rem;
      font-weight: 400;
      line-height: 1.2;
      color: var(--ink);
    }
    .location-meta {
      margin: 2px 0 0;
      color: var(--muted);
      font-size: .8rem;
    }
    .map-toggle {
      flex: none;
      min-height: 36px;
      padding: 6px 14px;
      border: 1px solid #b8d2e5;
      border-radius: 4px;
      background: var(--blue-soft);
      color: var(--blue-deep);
      font-size: .8rem;
      font-weight: 650;
    }
    .map-toggle:hover {
      background: var(--blue);
      border-color: var(--blue);
      color: #fff;
    }
    .map-toggle[aria-expanded="true"] {
      background: var(--paper);
      color: var(--blue-deep);
    }
    .map-toggle[aria-expanded="true"]:hover {
      background: var(--blue-soft);
      color: var(--blue-deep);
    }
    .map-places {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 12px;
    }
    .map-place {
      min-height: 32px;
      padding: 4px 10px;
      border: 1px solid #c6d5df;
      border-radius: 4px;
      background: var(--paper);
      color: var(--muted);
      font-size: .78rem;
      font-weight: 650;
    }
    .map-place:hover { border-color: #91b4cc; color: var(--blue-deep); }
    .map-place.is-current {
      border-color: var(--blue);
      background: var(--blue-soft);
      color: var(--blue-deep);
    }
    .map-frame { min-width: 0; margin-top: 14px; }
    .map-status {
      margin: 0 0 10px;
      color: var(--muted);
      font-size: .84rem;
    }
    .campus-map {
      position: relative;
      height: 340px;
      overflow: hidden;
      border: 1px solid #cfe0ec;
      border-radius: 4px;
      background: #e7eef3;
      touch-action: none;
    }
    .campus-map-stage {
      position: absolute;
      inset: 0;
      overflow: hidden;
      cursor: grab;
    }
    .campus-map-stage.is-dragging { cursor: grabbing; }
    .campus-tile {
      position: absolute;
      width: 256px;
      height: 256px;
      user-select: none;
      pointer-events: none;
    }
    .campus-pin {
      position: absolute;
      z-index: 2;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 4px;
      max-width: 168px;
      padding: 0;
      border: 0;
      background: transparent;
      transform: translate(-50%, -100%);
      cursor: pointer;
    }
    .campus-pin.is-below { transform: translate(-50%, 8px); flex-direction: column-reverse; }
    .campus-pin-card {
      display: flex;
      flex-direction: column;
      gap: 1px;
      max-width: 160px;
      padding: 4px 8px 5px;
      border: 1px solid #c5d7e6;
      border-radius: 4px;
      background: rgba(255, 255, 255, .96);
      box-shadow: 0 2px 8px rgba(22, 58, 88, .16);
      color: var(--ink);
      text-align: left;
    }
    .campus-pin-name {
      font-size: .78rem;
      font-weight: 750;
      line-height: 1.15;
    }
    .campus-pin-tag {
      color: var(--muted);
      font-size: .68rem;
      font-weight: 650;
      line-height: 1.2;
    }
    .campus-pin-point {
      width: 14px;
      height: 14px;
      border: 2px solid #fff;
      border-radius: 50%;
      background: var(--blue-deep);
      box-shadow: 0 1px 4px rgba(22, 58, 88, .45);
    }
    .campus-pin.is-current { z-index: 3; }
    .campus-pin.is-current .campus-pin-card {
      border-color: var(--blue-deep);
      background: var(--blue-deep);
      color: #fff;
    }
    .campus-pin.is-current .campus-pin-tag { color: #d5e6f2; }
    .campus-pin.is-current .campus-pin-point {
      background: var(--blue);
      box-shadow: 0 0 0 4px rgba(35, 103, 166, .28);
    }
    .map-zoom {
      position: absolute;
      z-index: 4;
      top: 8px;
      right: 8px;
      display: flex;
      gap: 4px;
    }
    .map-zoom-btn {
      width: 32px;
      height: 32px;
      padding: 0;
      border: 1px solid #c5d7e6;
      border-radius: 4px;
      background: rgba(255, 255, 255, .96);
      color: var(--blue-deep);
      font-size: 1rem;
      font-weight: 700;
      line-height: 1;
      box-shadow: 0 1px 4px rgba(22, 58, 88, .12);
    }
    .map-fit-btn {
      flex: none;
      padding: 0;
      border: 0;
      background: transparent;
      color: var(--blue-deep);
      font-size: .78rem;
      font-weight: 650;
      text-decoration: underline;
      text-underline-offset: .18em;
    }
    .map-attribution {
      position: absolute;
      z-index: 4;
      right: 10px;
      bottom: 8px;
      padding: 2px 6px;
      border-radius: 3px;
      background: rgba(255, 255, 255, .88);
      color: var(--muted);
      font-size: .68rem;
      text-decoration: none;
    }
    .map-caption {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin-top: 8px;
    }
    .map-note { margin: 0; color: var(--muted); font-size: .75rem; }
    .map-external { flex: none; font-size: .78rem; font-weight: 650; }
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
      .shell { width: min(100% - 24px, 1440px); }
      .hero { grid-template-columns: 1fr; gap: 20px; padding: 24px 0 16px; }
      .utility { align-items: center; justify-content: space-between; gap: 12px; padding: 12px 0; }
      .downloads a {
        display: inline-flex; align-items: center; gap: 5px;
        padding: 6px 12px; border-radius: 4px;
        background: var(--blue-soft); color: var(--blue-deep);
        font-weight: 650; font-size: .78rem; text-decoration: none;
      }
      .controls { grid-template-columns: 1fr 1fr; align-items: end; }
      .search-field { grid-column: 1 / -1; }
      .reset-button { grid-column: 2; align-self: end; }
      .result-line, footer, .annual-heading { align-items: flex-start; flex-direction: column; gap: 6px; }
      .mobile-sort {
        display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px;
        margin: 0 0 14px;
      }
      .mobile-sort-label {
        grid-column: 1 / -1; margin-bottom: 2px;
        color: var(--muted); font-size: .74rem; font-weight: 700;
        text-transform: uppercase; letter-spacing: .06em;
      }
      .mobile-sort-button {
        display: inline-flex; align-items: center; justify-content: center; gap: 6px;
        min-height: 44px; padding: 8px 10px;
        border: 1px solid #c6d5df; border-radius: 6px; background: var(--paper);
        color: var(--blue-deep); font-size: .78rem; font-weight: 650;
      }
      .mobile-sort-button.is-active { border-color: var(--blue); background: var(--blue-soft); }
      .mobile-sort-button .sort-indicator { min-width: .7em; color: var(--blue-deep); }
      .table-frame { overflow: visible; background: transparent; border: 0; box-shadow: none; }
      #report-table, .annual-table { min-width: 0; }
      #report-table > thead { display: none; }
      #report-table, #report-table > tbody { display: block; width: 100%; }
      #report-table > tbody { display: block; }
      tr.university-row {
        display: flex; flex-direction: column; gap: 10px;
        padding: 16px 14px; border: 1px solid var(--line); border-radius: 8px;
        background: var(--paper); box-shadow: 0 2px 6px rgba(22, 58, 88, 0.05);
        margin-bottom: 12px; transition: background 0.15s ease, border-color 0.15s ease;
      }
      tr.university-row.is-expanded {
        background: #eaf3fa;
        border-color: var(--blue);
        border-bottom: 0;
        border-bottom-left-radius: 0;
        border-bottom-right-radius: 0;
        margin-bottom: 0;
        box-shadow: inset 5px 0 0 var(--blue);
      }
      tr.university-detail-row { display: block; }
      tr.university-detail-row:not([hidden]) {
        display: block;
        margin-bottom: 12px;
        border: 1px solid var(--blue);
        border-top: 0;
        border-bottom: 3px solid var(--blue);
        border-bottom-left-radius: 8px;
        border-bottom-right-radius: 8px;
        background: #eef5fa;
        box-shadow: inset 5px 0 0 var(--blue);
      }
      tr.university-detail-row > td, tr.empty-row > td { display: block; width: 100%; padding: 0; border: 0; }
      tr.university-row > td {
        display: block; width: 100%; min-width: 0; padding: 0; border: 0;
      }
      tr.university-row > td::before { display: none !important; }
      .university-toggle .expand-label { display: none; }
      tr.university-row > .details-cell {
        display: block;
        order: 10;
        width: 100%;
        margin-top: 4px;
      }
      tr.university-row > .details-cell button {
        display: flex; align-items: center; justify-content: center;
        width: 100%; min-height: 44px; padding: 10px 14px;
        border-radius: 6px; background: var(--blue-soft); color: var(--blue-deep);
        font-size: .84rem; font-weight: 650; border: 1px solid #c8deef;
        transition: background 0.15s ease, color 0.15s ease;
      }
      tr.university-row.is-expanded > .details-cell button {
        background: var(--blue-deep); color: #ffffff; border-color: var(--blue-deep);
      }
      .university-toggle {
        display: flex; flex-direction: column; width: 100%; align-items: stretch;
        text-align: left; background: transparent; border: 0; padding: 0; cursor: pointer;
      }
      .university-toggle .university-name {
        display: block; font-size: 1.08rem; font-weight: 700; line-height: 1.35;
        color: var(--ink); margin-bottom: 4px;
      }
      tr.university-row > .country-cell {
        display: inline; font-size: .84rem; color: var(--muted); font-weight: 600;
      }
      tr.university-row > .years-cell {
        display: inline; font-size: .8rem; color: var(--muted);
      }
      tr.university-row > .years-cell::before {
        content: " · "; color: #94a3b8; font-weight: 400;
      }
      tr.university-row > .years-cell span { display: inline; margin-left: 3px; }
      tr.university-row > .score-cell { width: 100%; max-width: none; margin-top: 2px; }
      .score-stack { width: 100%; }
      .score-line { display: flex; align-items: baseline; gap: 6px; margin-bottom: 4px; }
      .score-value { font-size: 1.25rem; font-weight: 700; color: var(--blue-deep); }
      .score-unit { font-size: .78rem; color: var(--muted); }
      .score-track { width: 100%; max-width: none; height: 8px; }
      .detail-card-shell { padding: 12px 10px; gap: 12px; }
      .location-panel { padding: 12px; }
      .location-bar { align-items: stretch; flex-direction: column; gap: 10px; }
      .map-toggle { width: 100%; min-height: 44px; }
      .campus-map { height: 280px; }
      .campus-pin { max-width: 132px; }
      .campus-pin-card { max-width: 124px; padding: 3px 6px 4px; }
      .campus-pin-name { font-size: .72rem; }
      .map-zoom-btn { width: 40px; height: 40px; }
      .map-caption { align-items: flex-start; flex-direction: column; gap: 4px; }
      .profile-panel { padding: 14px 12px; }
      .profile-further-rows > div { grid-template-columns: 1fr; gap: 2px; }
      .ranking-link { white-space: normal; }
      .profile-panel-heading { align-items: flex-start; flex-direction: column; gap: 5px; }
      .profile-relation > summary { align-items: flex-start; flex-direction: column; gap: 8px; }
      .profile-relation-badges { justify-content: flex-start; }
      .profile-fields { grid-template-columns: 1fr; gap: 8px; }
      .annual-panel { padding: 14px 12px; }
      .annual-table { width: 100%; min-width: 0; border: 0; overflow: visible; }
      .annual-table > thead { display: none; }
      .annual-table > tbody { display: flex; flex-direction: column; gap: 8px; width: 100%; }
      tr.annual-data-row {
        display: grid; grid-template-columns: 82px 1fr;
        gap: 4px 10px; align-items: center; padding: 10px 12px 12px;
        border: 1px solid #d9e6f2; border-radius: 6px; background: #ffffff;
        width: 100%; box-sizing: border-box;
      }
      tr.annual-data-row > td { display: block; padding: 0; border: 0; min-width: 0; }
      tr.annual-data-row > td::before { display: none !important; }
      tr.annual-data-row > .annual-year-cell {
        grid-column: 1; grid-row: 1; font-size: 1.05rem; font-weight: 700; color: var(--blue-deep);
      }
      tr.annual-data-row > .annual-source-cell {
        grid-column: 1; grid-row: 2; font-size: .72rem; color: var(--muted); white-space: nowrap;
      }
      tr.annual-data-row > .annual-score-cell {
        grid-column: 2; grid-row: 1 / span 2; width: 100%;
      }
      tr.annual-data-row > .annual-status-cell { grid-column: 1 / -1; }
      tr.annual-data-row > .annual-status-cell:has(.status-measured) { display: none; }
    }
    @media (max-width: 768px) {
      .basis-tooltip {
        position: absolute; bottom: calc(100% + 7px); left: auto; right: -6px;
        transform: none; width: max-content; max-width: min(270px, calc(100vw - 36px));
        z-index: 100;
      }
      .basis-tooltip::after { left: auto; right: 18px; transform: none; }
      .basis-badge:hover .basis-tooltip,
      .basis-badge:focus-visible .basis-tooltip,
      .basis-badge:focus .basis-tooltip {
        display: block;
        transform: translateY(-2px);
      }
      .help-tooltip {
        position: fixed;
        top: auto;
        bottom: 24px;
        left: 16px;
        right: 16px;
        width: auto;
        max-width: none;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
        border-radius: 8px;
        z-index: 1000;
      }
      .summary {
        display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-bottom: 20px;
      }
      .summary > div {
        padding: 10px 4px; text-align: center; background: var(--paper);
        border: 1px solid var(--line); border-radius: 6px;
        min-width: 0; box-sizing: border-box;
      }
      .summary dt {
        font-size: .62rem; text-transform: uppercase; letter-spacing: .03em;
        margin-bottom: 3px; color: var(--muted);
        white-space: normal; line-height: 1.25;
      }
      .summary dd {
        font-size: clamp(0.92rem, 3.8vw, 1.15rem); font-weight: 700; color: var(--blue-deep); margin: 0;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .summary .stat-sub { display: none; }
      .controls {
        display: flex;
        flex-direction: column;
        align-items: stretch;
        gap: 12px;
        margin-bottom: 16px;
      }
      .field {
        width: 100%;
        min-width: 0;
        box-sizing: border-box;
      }
      .field label {
        display: block;
        width: 100%;
        margin: 0 0 6px;
        color: var(--muted);
        font-size: .8rem;
        font-weight: 650;
        text-align: left;
      }
      .label-with-help {
        display: flex;
        align-items: center;
        gap: 7px;
        width: 100%;
      }
      .field input, .field select {
        width: 100%;
        box-sizing: border-box;
        min-height: 46px;
        font-size: 16px !important;
        border-radius: 6px;
      }
      .search-field input {
        min-height: 48px;
      }
      .reset-button {
        width: 100%;
        box-sizing: border-box;
        min-height: 46px;
        display: flex;
        align-items: center;
        justify-content: center;
        margin: 4px 0 0;
        font-size: .88rem;
        font-weight: 650;
        border-radius: 6px;
      }
    }
    @media (max-width: 640px) {
      .shell { width: min(100% - 20px, 1440px); }
      .hero { padding: 16px 0 12px; gap: 14px; }
      .hero h1 { font-size: 1.7rem; line-height: 1.2; margin-bottom: 6px; }
      .hero .lede { font-size: .9rem; }
      .method-card { padding: 14px; border-radius: 6px; }
      .method-card h2 { font-size: 1.1rem; margin: 0 0 6px; }
      .method-card p { font-size: .8rem; line-height: 1.35; margin: 0 0 6px; }
      .preset-full { display: none; }
      .preset-short { display: inline; }
      .year-filter-section { padding: 12px; border-radius: 6px; margin: 4px 0 12px; width: 100%; box-sizing: border-box; }
      .year-filter-header { flex-direction: column; align-items: stretch; gap: 8px; margin-bottom: 10px; }
      .year-filter-label { font-size: .74rem; text-align: center; }
      .year-filter-presets { display: grid; grid-template-columns: repeat(3, 1fr); gap: 5px; width: 100%; box-sizing: border-box; }
      .preset-btn {
        min-height: 38px;
        padding: 6px 2px;
        font-size: .74rem;
        font-weight: 650;
        text-align: center;
        border-radius: 6px;
        white-space: nowrap;
        display: flex;
        align-items: center;
        justify-content: center;
      }
      .year-pills { display: grid; grid-template-columns: repeat(5, 1fr); gap: 5px; width: 100%; box-sizing: border-box; }
      .year-pill {
        min-height: 42px;
        padding: 4px 1px;
        font-size: .76rem;
        border-radius: 6px;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 2px;
        width: 100%;
        box-sizing: border-box;
        white-space: nowrap;
      }
      .year-check { font-size: .7rem; }
      .year-era-tag { display: none; }
      .mobile-sort { display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px; margin: 0 0 12px; }
      .mobile-sort-label { font-size: .72rem; }
      .mobile-sort-button { min-height: 42px; font-size: .76rem; padding: 6px 8px; justify-content: center; text-align: center; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; animation-duration: .01ms !important; }
    }
    @media print {
      body { background: #fff; }
      .utility, .controls, .mobile-sort, .detail-toggle, .map-toggle, .map-frame, footer { display: none; }
      .shell { width: 100%; }
      .hero { padding: 20px 0; }
      .table-frame { overflow: visible; box-shadow: none; }
      table { min-width: 0; }
      .university-detail-row[hidden] { display: table-row !important; }
    }

    /* Violet workspace layout, including responsive and sticky-filter behavior. */
:root { --shadow:none; font-family:"Helvetica Neue",Helvetica,Arial,sans-serif; font-size:16px; }
body { line-height:1.5; }
h1 { font-family:inherit; font-weight:650; font-size:clamp(1.8rem,3vw,2.65rem); line-height:1.15; letter-spacing:-.035em; max-width:720px; }
.shell { width:min(1456px,calc(100% - 64px)); }
.utility { min-height:76px; font-size:.875rem; }
.brand { font-weight:650; font-size:.95rem; }
.brand-mark { width:32px;height:32px;border:0;border-radius:6px;background:var(--blue);color:var(--paper);font:700 18px/1 inherit; }
.downloads a { color:var(--blue-deep); font-weight:550; }
.hero { padding:36px 0 26px; gap:40px; grid-template-columns:minmax(0,1.25fr) minmax(320px,1fr); align-items:start; }
.lede { max-width:65ch; font-size:1rem;line-height:1.6; }
.eyebrow { display:none; }
.detail-kicker,.summary dt,.year-filter-label,th { letter-spacing:0; text-transform:none; }
.method-card { background:var(--paper); color:var(--ink); border:1px solid var(--line); border-left:3px solid var(--blue); box-shadow:none; padding:18px 22px; }
.method-card h2 { color:var(--ink); font-size:1rem; margin:0 0 8px; }
.method-card p { color:var(--muted); font-size:.83rem; line-height:1.55; }
.method-card strong { color:var(--ink); }
.method-note { font-style:normal; }
.summary { background:transparent; border:0;border-bottom:1px solid var(--line); margin:0 0 26px; gap:0; grid-template-columns:1.5fr 1fr 1fr; }
.summary div { padding:0 22px 20px; background:transparent; border-right:1px solid var(--line); }
.summary div:first-child { padding-left:0; }
.summary div:last-child { border-right:0; }
.summary dt { font-size:.8rem; font-weight:500; }
.summary dd { font-size:1.5rem; margin-top:6px; line-height:1.3; font-weight:650; overflow:visible; white-space:normal; }
.summary dd .stat-sub { display:block; margin:3px 0 0; font-size:.78rem; }
.filter-heading { font-size:1.15rem; margin-bottom:16px; }
.controls { gap:16px; }
.field label { color:var(--ink);font-size:.82rem;font-weight:550; }
.field input,.field select { border-color:var(--line);min-height:46px;border-radius:5px;font-size:.95rem;padding:10px 12px; }
.field input::placeholder { color:var(--muted); }
.field input:focus,.field select:focus { outline:2px solid var(--blue);outline-offset:2px; }
.reset-button { border-color:var(--line);border-radius:5px;min-height:46px; }
.year-filter-section { margin:4px 0 0;padding:18px 20px;border-radius:6px;box-shadow:none;background:var(--paper); }
.year-filter-label { font-size:.85rem; }
.preset-btn { border-color:var(--line);border-radius:4px;background:var(--paper);color:var(--blue-deep);min-height:36px; }
.preset-btn:hover { background:var(--blue-soft);border-color:var(--blue); }
.year-pill { min-height:40px;border-radius:5px;background:var(--paper);border-color:var(--line);color:var(--muted);padding:6px 12px;box-shadow:none; }
.year-pill:hover { background:var(--blue-soft); }
.year-pill[aria-pressed="true"] { background:var(--blue-deep);border-color:var(--blue-deep);color:#fff;box-shadow:none; }
.year-era-tag { background:var(--wash);color:var(--muted);font-weight:500; }
.result-line { color:var(--muted);font-size:.83rem;margin:20px 0 14px; }
.table-frame { border:1px solid var(--line);box-shadow:none; }
th { background:var(--wash);font-size:.8rem;color:var(--muted); }
td { color:var(--ink);border-color:var(--line);font-size:.9rem; }
.university-name { font-weight:600;line-height:1.4; }
.university-toggle { flex-wrap:wrap;gap:5px; }
.expand-label { color:var(--blue-deep);font-size:.75rem; }
.profile-badge { font-size:.73rem; font-weight:550; }
.profile-summary,.years-cell span,.score-unit { font-size:.78rem; }
.detail-toggle { background:var(--paper);border-color:var(--line);min-height:40px;border-radius:5px;box-shadow:none; }
.score-track { background:var(--line);height:7px; }
.score-value { font-weight:650; }
tr.university-row.is-expanded, tr.university-row.is-expanded td { background:var(--blue-soft) !important; }
tr.university-detail-row > td { background:var(--wash); }
tr.university-detail-row:not([hidden]) { box-shadow:inset 3px 0 0 var(--blue); }
footer { font-size:.82rem;color:var(--muted); }
:focus-visible { outline:3px solid var(--blue);outline-offset:3px; }
@media(prefers-reduced-motion:reduce) { *,*::before,*::after { transition:none !important;animation:none !important;scroll-behavior:auto !important; } }

[data-theme="workspace"] { --ink:#282742;--muted:#62627a;--line:#d5d5e3;--paper:#fff;--wash:#f5f5fa;--blue:#6454bd;--blue-deep:#51419e;--blue-soft:#f0edff;--green:#31644a;--green-soft:#eaf4ed;font-family:"Helvetica Neue",Arial,sans-serif; }
[data-theme="workspace"] .brand-mark { border-radius:9px; }
[data-theme="workspace"] .hero { padding:28px 0 22px; }
[data-theme="workspace"] h1 { font-size:clamp(1.8rem,2.5vw,2.25rem); }
[data-theme="workspace"] .method-card { border-radius:8px; }
[data-theme="workspace"] .table-frame { border-radius:8px; }
[data-theme="workspace"] .summary { margin-bottom:22px; }
[data-theme="workspace"] tbody tr.data-row:hover { background:var(--blue-soft); }
@media(min-width:1150px) {
 [data-theme="workspace"] .shell { display:grid;grid-template-columns:272px minmax(0,1fr);column-gap:28px;grid-template-areas:"nav nav" "hero hero" "summary summary" "filtertitle results" "filters table" "footer footer";align-items:start; }
 [data-theme="workspace"] .utility { grid-area:nav; }
 [data-theme="workspace"] .hero { grid-area:hero; }
 [data-theme="workspace"] .summary { grid-area:summary; }
 [data-theme="workspace"] .shell > section { display:contents; }
 [data-theme="workspace"] .filter-heading { grid-area:filtertitle;margin:0;padding:10px 0 18px; }
 [data-theme="workspace"] .controls { grid-area:filters;display:flex;flex-direction:column;align-items:stretch;gap:15px;background:var(--paper);padding:18px;border:1px solid var(--line);border-radius:8px; }
 [data-theme="workspace"] .result-line { grid-area:results; margin:8px 0 18px; }
 [data-theme="workspace"] .table-frame { grid-area:table;min-width:0; }
 [data-theme="workspace"] .year-filter-section { border:0;border-top:1px solid var(--line);padding:18px 0 0;margin:0; }
 [data-theme="workspace"] .year-filter-header { gap:12px; }
 [data-theme="workspace"] .year-filter-presets { display:grid;grid-template-columns:1fr; width:100%; }
 [data-theme="workspace"] .year-pills { gap:7px; }
 [data-theme="workspace"] .year-pill { padding:6px 9px;font-size:.8rem; }
 [data-theme="workspace"] table { min-width:760px; }
 [data-theme="workspace"] .score-cell { min-width:145px; }
 [data-theme="workspace"] footer { grid-area:footer; }
}

@media(max-width:900px) {
 .shell { width:calc(100% - 36px); }
 .utility { min-height:68px;gap:14px; }
 .brand { font-size:.85rem; }
 .hero { grid-template-columns:1fr;gap:18px;padding:26px 0 22px; }
 .hero h1 { font-size:2rem; }
 .summary { grid-template-columns:1.5fr .75fr 1fr; }
 .controls { gap:14px; }
 .summary dd { font-size:1.2rem; }
 .summary dd .stat-sub { font-size:.73rem; }
}
@media(max-width:640px) {
 .shell { width:calc(100% - 28px); }
 .utility { min-height:76px;align-items:center; }
 .brand { max-width:220px;gap:9px;line-height:1.35; }
 .brand-mark { flex-shrink:0; }
 .downloads a { font-size:.78rem; }
 .hero h1 { font-size:1.8rem;line-height:1.2; }
 .hero .lede { font-size:.95rem; }
 .method-card { padding:16px; }
 .method-card p { font-size:.84rem; }
 .summary { display:grid;grid-template-columns:1fr 1fr;row-gap:18px;padding:0 0 18px; }
 .summary div { padding:0 12px 0 0;border:0; }
 .summary div:first-child { grid-column:1/-1; }
 .summary dd .stat-sub { display:inline;margin-left:6px; }
 .year-filter-section { padding:16px;margin:4px 0 0; }
 .year-filter-presets { gap:6px; }
 .year-pill { min-height:40px; }
 .table-frame { border:0;border-radius:0 !important;background:transparent; }
 tr.university-row { background:var(--paper);border:1px solid var(--line);border-radius:8px;box-shadow:none;margin-bottom:14px; }
 tr.university-row > td { padding:0; }
 .result-line { font-size:.8rem; }
 .result-line > span:last-child { font-size:.76rem; }
 .mobile-sort { margin:16px 0; }
}

:root { color-scheme:light; }
.year-pill,.preset-btn,.detail-toggle,tr.university-row { transition:none; }
@media(min-width:901px) {
 .utility { min-height:64px; }
 .hero { padding:26px 0 22px; }
 .summary div { padding-bottom:14px; }
 .summary { margin-bottom:22px; }
 .controls { position:relative; }
 .reset-button { position:absolute;right:0;top:-49px;min-height:36px;padding:6px 12px;font-size:.8rem; }
 .filter-heading { padding-right:150px; }
}
@media(min-width:1150px) {
 [data-theme="workspace"] .reset-button { position:static;min-height:44px; }
 [data-theme="workspace"] .filter-heading { padding-right:0; }
}

.profile-panel-heading h3 { font-family:inherit;font-weight:600; }
.panel-eyebrow { letter-spacing:0;text-transform:none;font-size:.76rem; }
.profile-panel,.location-panel,.history-panel { box-shadow:none;border-color:var(--line); }
.profile-relation { background:var(--paper);border-color:var(--line); }
.profile-relation > summary:hover { background:var(--blue-soft); }
.profile-field dd { color:var(--ink); }
.profile-further-rows dd { color:var(--ink); }
.location-heading h3,.history-heading h3,.empty-state strong { font-family:inherit;font-weight:600; }
.location-name,.annual-heading h3 { font-family:inherit;font-weight:600; }
.annual-panel { box-shadow:none;border-color:var(--line); }
.annual-table { border-color:var(--line); }
.annual-table th { background:var(--wash);color:var(--muted);border-color:var(--line);text-transform:none;letter-spacing:0;font-size:.78rem; }
.annual-table td { border-color:var(--line); }
.annual-count-badge { background:var(--blue-soft);border-color:var(--line); }

@media(min-width:1150px) {
 [data-theme="workspace"] body { overflow-x:clip; }
 [data-theme="workspace"] .controls {
   position:sticky;top:16px;max-height:calc(100vh - 32px);max-height:calc(100dvh - 32px);
   overflow-y:auto;overflow-x:hidden;scrollbar-gutter:stable;overscroll-behavior-y:auto;
 }
 [data-theme="workspace"] #report-table { table-layout:fixed;min-width:0;width:100%; }
 [data-theme="workspace"] #report-table > thead th:nth-child(1) { width:36%; }
 [data-theme="workspace"] #report-table > thead th:nth-child(2) { width:11%; }
 [data-theme="workspace"] #report-table > thead th:nth-child(3) { width:13%; }
 [data-theme="workspace"] #report-table > thead th:nth-child(4) { width:23%; }
 [data-theme="workspace"] #report-table > thead th:nth-child(5) { width:17%; }
 [data-theme="workspace"] #report-table > thead .sort-button { white-space:normal; }
 [data-theme="workspace"] .university-toggle > span:first-child { min-width:0;max-width:100%; }
 [data-theme="workspace"] .university-name { overflow-wrap:anywhere; }
 [data-theme="workspace"] .score-cell { min-width:0; }
 [data-theme="workspace"] .detail-toggle { max-width:100%; }
}
@media(min-width:1150px) {
 [data-theme="workspace"] .university-toggle { flex-direction:column;align-items:flex-start;gap:6px; }
}

[data-theme="workspace"] .label-with-help { position:relative; }
[data-theme="workspace"] .label-with-help .help { position:static; }
[data-theme="workspace"] .label-with-help .help-tooltip { left:0;right:auto;top:calc(100% + 8px);width:100%;max-width:none;z-index:6; }

@media(min-width:1150px) {
 [data-theme="workspace"] .detail-card-shell { min-width:0;grid-template-columns:minmax(0,1fr); }
 [data-theme="workspace"] .detail-card-shell > * { min-width:0; }
 [data-theme="workspace"] .annual-table { min-width:0;table-layout:fixed; }
 [data-theme="workspace"] .annual-table th,[data-theme="workspace"] .annual-table td { overflow-wrap:anywhere;white-space:normal; }
 [data-theme="workspace"] .annual-heading,[data-theme="workspace"] .profile-panel-heading,[data-theme="workspace"] .location-bar { flex-wrap:wrap; }
}

@media(max-width:768px) {
 [data-theme="workspace"] .label-with-help .help-tooltip { position:fixed;top:auto;bottom:24px;left:16px;right:16px;width:auto;max-width:none;z-index:1000; }
}
@media(max-width:640px) {
 [data-theme="workspace"] .year-pills { grid-template-columns:repeat(5,minmax(0,1fr));gap:5px;min-width:0; }
 [data-theme="workspace"] .year-pill { min-width:0;padding:4px 1px;gap:2px;font-size:.76rem;min-height:44px; }
 [data-theme="workspace"] .year-check { font-size:.7rem; }
}

@media(min-width:1150px) {
 [data-theme="workspace"] .shell { grid-template-areas:"nav nav" "hero hero" "summary summary" "filtertitle results" "filters table" ". footer"; }
 [data-theme="workspace"] .controls { gap:11px;padding:14px;overflow-anchor:none; }
 [data-theme="workspace"] .field label { margin-bottom:5px; }
 [data-theme="workspace"] .label-with-help label { margin-bottom:0; }
 [data-theme="workspace"] .field input,[data-theme="workspace"] .field select { min-height:44px;padding:8px 10px; }
 [data-theme="workspace"] .reset-button { min-height:40px; }
 [data-theme="workspace"] .year-filter-section { padding-top:12px; }
 [data-theme="workspace"] .year-filter-header { gap:10px;margin-bottom:10px; }
 [data-theme="workspace"] .year-filter-label { font-size:.8rem; }
 [data-theme="workspace"] .year-filter-presets { grid-template-columns:repeat(3,minmax(0,1fr));gap:5px; }
 [data-theme="workspace"] .preset-full { display:none; }
 [data-theme="workspace"] .preset-short { display:inline; }
 [data-theme="workspace"] .preset-btn { min-width:0;padding:6px 2px;font-size:.7rem;min-height:40px; }
 [data-theme="workspace"] .year-pills { display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:5px; }
 [data-theme="workspace"] .year-pill { min-width:0;min-height:44px;padding:4px 1px;font-size:.75rem;gap:2px; }
 [data-theme="workspace"] .year-check { font-size:.65rem; }
 [data-theme="workspace"] .year-era-tag { display:none; }
 [data-theme="workspace"] .table-frame { min-height:calc(100vh - 32px);min-height:calc(100dvh - 32px); }
 [data-theme="workspace"] footer { min-width:0;flex-wrap:wrap;gap:12px;padding-top:20px; }
}
@media(min-width:1150px) {
 [data-theme="workspace"] .table-frame { background:transparent;border:0;overflow:visible; }
 [data-theme="workspace"] #report-table { background:var(--paper);border:1px solid var(--line);border-radius:8px;border-collapse:separate;border-spacing:0; }
 [data-theme="workspace"] #report-table > thead th:first-child { border-top-left-radius:8px; }
 [data-theme="workspace"] #report-table > thead th:last-child { border-top-right-radius:8px; }
}
  </style>
</head>
<body>
  <div class="shell">
    <nav class="utility" aria-label="Report navigation">
      <div class="brand"><span class="brand-mark" aria-hidden="true">T</span><span>TUMexchange Acceptance Information</span></div>
      <div class="downloads"><a href="rows.csv" download>Download CSV data</a></div>
    </nav>
    <header class="hero">
      <div>
        <h1>TUMexchange Acceptance Information</h1>
        <p class="lede">See how first-round wishes compared with available spots, and check partner university requirements. Filter by country, degree level, or subject area.</p>
      </div>
      <aside class="method-card" aria-labelledby="method-heading">
        <p class="eyebrow">How to read the bars</p>
        <h2 id="method-heading">What the bars mean</h2>
        <p><strong>Full bar (100%):</strong> There were at least as many spots as first-round wishes, or nobody applied.</p>
        <p><strong>Partial bar:</strong> Spots divided by first-round wishes. 33% means about three wishes for each spot.</p>
        <p class="method-note">A shorter bar means more first-round wishes per spot. Later rounds are not included. Past years are a guide, not an admission guarantee.</p>
      </aside>
    </header>
    <dl class="summary" aria-label="Report summary">
      <div><dt>Partner universities</dt><dd id="stat-universities">@@ACTIVE_UNIVERSITIES@@ <span class="stat-sub">with places this year (@@UNIVERSITIES@@ total)</span></dd></div>
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
              <button class="help-trigger" type="button" aria-label="Explain the full bar for years with no wishes" aria-describedby="assigned-help">i</button>
              <span class="help-tooltip" id="assigned-help" role="tooltip">Years with no applications are shown as 100% because there were spots and no first-round wishes.</span>
            </span>
          </span>
          <select id="status-filter">@@STATUS_OPTIONS@@</select>
        </div>
        <div class="field"><label for="roster-filter">Application availability</label><select id="roster-filter"><option value="current" selected>Places this year (listed on MoveON)</option><option value="all">All universities, including past agreements</option></select></div>
        <div class="field"><label for="degree-filter">Degree level</label><select id="degree-filter"><option value="">All degree levels</option><option value="bachelor">Bachelor</option><option value="master">Master</option><option value="unknown">Unknown</option></select></div>
        <div class="field"><label for="subject-filter">Subject area</label><select id="subject-filter">@@SUBJECT_OPTIONS@@</select></div>
        <button class="reset-button" type="button" id="reset-filters">Reset filters</button>
        <div class="field year-filter-section" role="group" aria-labelledby="year-filter-heading">
          <div class="year-filter-header">
            <span id="year-filter-heading" class="year-filter-label">Years included in average calculation</span>
            <div class="year-filter-presets">
              <button type="button" class="preset-btn" id="preset-core" title="Reset to standard 5-year core window (2022–2026)"><span class="preset-full">Core (2022–2026)</span><span class="preset-short">Core (22–26)</span></button>
              <button type="button" class="preset-btn" id="preset-all" title="Include all 10 historical years (2017–2026)"><span class="preset-full">All Years (2017–2026)</span><span class="preset-short">All (17–26)</span></button>
              <button type="button" class="preset-btn" id="preset-recent" title="Focus strictly on latest 3 observation years (2024–2026)"><span class="preset-full">Recent 3y (2024–2026)</span><span class="preset-short">Recent 3y</span></button>
            </div>
          </div>
          <div class="year-pills" id="year-pills" role="group" aria-label="Toggle observation years in average calculation">
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
      <div class="result-line" aria-live="polite"><span id="result-count"><strong>@@ACTIVE_UNIVERSITIES@@</strong> universities with places this year</span><span>Averages reflect years with active exchange data</span></div>
      <div class="mobile-sort" role="group" aria-label="Sort this list">
        <span class="mobile-sort-label">Sort</span>
        <button type="button" class="mobile-sort-button" data-sort-column="university" aria-pressed="false">University<span class="sort-indicator" aria-hidden="true"></span></button>
        <button type="button" class="mobile-sort-button" data-sort-column="country" aria-pressed="false">Country<span class="sort-indicator" aria-hidden="true"></span></button>
        <button type="button" class="mobile-sort-button" data-sort-column="scored" aria-pressed="false">Years recorded<span class="sort-indicator" aria-hidden="true"></span></button>
        <button type="button" class="mobile-sort-button" data-sort-column="score" aria-pressed="false">Spots to wishes<span class="sort-indicator" aria-hidden="true"></span></button>
      </div>
    </section>
    <div class="table-frame">
      <table id="report-table">
        <caption class="visually-hidden">TUMexchange universities with first-round wishes per spot and partner requirements</caption>
        <thead><tr>
          <th scope="col" class="sortable" data-sort-column="university" aria-sort="none"><button type="button" class="sort-button">University<span class="sort-indicator" aria-hidden="true"></span></button></th>
          <th scope="col" class="sortable" data-sort-column="country" aria-sort="none"><button type="button" class="sort-button">Country<span class="sort-indicator" aria-hidden="true"></span></button></th>
          <th scope="col" class="sortable" data-sort-column="scored" aria-sort="none"><button type="button" class="sort-button">Years recorded<span class="sort-indicator" aria-hidden="true"></span></button></th>
          <th scope="col" class="sortable" data-sort-column="score" aria-sort="none"><button type="button" class="sort-button">Spots to wishes<span class="sort-indicator" aria-hidden="true"></span></button></th>
          <th scope="col"><span class="visually-hidden">Details</span></th>
        </tr></thead>
        <tbody id="rows-body">@@INITIAL_ROWS@@</tbody>
      </table>
    </div>
    <footer><p>Bars show spots divided by first-round wishes, measured from official TUM charts.</p><p><a href="rows.csv" download>Download complete CSV dataset</a></p></footer>
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
      groups.forEach((group) => { group._haystack = foldSearch(group.search_text); });
      let cachedSearch = null;
      let cachedTokens = [];
      const searchTokens = () => {
        if (cachedSearch === state.search) return cachedTokens;
        cachedSearch = state.search;
        cachedTokens = foldSearch(state.search).split(/\s+/).filter(Boolean);
        return cachedTokens;
      };
      const searchMatches = (group) => {
        const tokens = searchTokens();
        if (!tokens.length) return true;
        return tokens.every((token) => group._haystack.includes(token));
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
      const wishesLabel = (score) => {
        const value = Number(score);
        if (!Number.isFinite(value)) return "no recorded data";
        if (value >= 99.95) return "at least one spot per wish";
        if (value <= 0) return "spots to wishes";
        const ratio = 100 / value;
        const rendered = Math.abs(ratio - Math.round(ratio)) < 0.05
          ? String(Math.round(ratio))
          : (ratio >= 10 ? ratio.toFixed(0) : ratio.toFixed(1));
        return "about " + rendered + " wishes per spot";
      };
      const setText = (element, value) => {
        if (element && element.textContent !== value) element.textContent = value;
      };
      const rowViews = new Map();
      const viewFor = (index) => {
        let view = rowViews.get(index);
        if (view) return view;
        const summary = summaryRows.get(index);
        const detail = detailRows.get(index);
        view = {
          summary,
          detail,
          yearsStrong: summary ? summary.querySelector(".years-cell strong") : null,
          scoreValEl: summary ? summary.querySelector(".score-value") : null,
          scoreUnitEl: summary ? summary.querySelector(".score-unit") : null,
          scoreTrack: summary ? summary.querySelector(".score-track") : null,
          scoreFill: summary ? summary.querySelector(".score-fill") : null,
          headingSpan: detail ? detail.querySelector(".annual-count-badge") : null,
          annualRows: detail ? Array.from(detail.querySelectorAll("tr.annual-data-row")) : [],
        };
        rowViews.set(index, view);
        return view;
      };
      const updateGroupScores = () => {
        groups.forEach((group) => {
          const metrics = computeGroupMetrics(group);
          group.current_score = metrics.score;
          group.current_scored_count = metrics.scored_count;
          group.current_total_count = metrics.total_count;
          group.current_categories = metrics.categories;
          const view = viewFor(Number(group.index));
          setText(view.yearsStrong, metrics.scored_count + " of " + metrics.total_count);
          if (metrics.score !== null) {
            const label = metrics.score.toFixed(1) + "%";
            const unit = wishesLabel(metrics.score);
            setText(view.scoreValEl, label);
            setText(view.scoreUnitEl, unit);
            if (view.scoreTrack) {
              view.scoreTrack.classList.remove("is-empty");
              const aria = label + ", " + unit;
              if (view.scoreTrack.getAttribute("aria-label") !== aria) view.scoreTrack.setAttribute("aria-label", aria);
            }
            if (view.scoreFill) {
              const width = metrics.score.toFixed(4) + "%";
              if (view.scoreFill.style.width !== width) view.scoreFill.style.width = width;
            }
          } else {
            setText(view.scoreValEl, "N/A");
            setText(view.scoreUnitEl, "no recorded data");
            if (view.scoreTrack) {
              view.scoreTrack.classList.add("is-empty");
              if (view.scoreTrack.getAttribute("aria-label") !== "N/A; no recorded data") {
                view.scoreTrack.setAttribute("aria-label", "N/A; no recorded data");
              }
            }
            if (view.scoreFill && view.scoreFill.style.width !== "0%") view.scoreFill.style.width = "0%";
          }
          setText(view.headingSpan, metrics.scored_count + " of " + metrics.total_count + " years with active exchange data");
          view.annualRows.forEach((row) => {
            const inactive = !state.years.has(Number(row.dataset.year));
            if (row.classList.contains("is-year-inactive") !== inactive) row.classList.toggle("is-year-inactive", inactive);
          });
        });
      };
      const matches = (group) => {
        if (state.roster === "current" && !group.offers_places) return false;
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
      let appliedSort = "source";
      const reorderRows = () => {
        const fragment = document.createDocumentFragment();
        orderedGroups().forEach((group) => {
          const index = Number(group.index);
          const summary = summaryRows.get(index);
          const detail = detailRows.get(index);
          if (!summary || !detail) return;
          fragment.append(summary, detail);
        });
        const empty = document.getElementById("empty-state-row");
        body.appendChild(fragment);
        if (empty) body.appendChild(empty);
        appliedSort = state.sort;
      };
      const render = () => {
        const filterPanel = document.querySelector(".controls");
        const filterStyle = filterPanel ? getComputedStyle(filterPanel) : null;
        const stickyInset = filterStyle ? parseFloat(filterStyle.top) || 0 : 0;
        const filtersPinned = filterStyle && filterStyle.position === "sticky"
          && filterPanel.getBoundingClientRect().top <= stickyInset + 1;
        const resultsFrame = body.closest(".table-frame");
        const resultsTop = resultsFrame ? resultsFrame.getBoundingClientRect().top + window.scrollY : 0;
        if (state.sort !== appliedSort) reorderRows();
        let visibleCount = 0;
        groups.forEach((group) => {
          const index = Number(group.index);
          const summary = summaryRows.get(index);
          const detail = detailRows.get(index);
          if (!summary || !detail) return;
          const shown = matches(group);
          if (shown) visibleCount += 1;
          if (summary.hidden === shown) summary.hidden = !shown;
          if (!shown && (!detail.hidden || summary.classList.contains("is-expanded"))) {
            const button = summary.querySelector(".university-toggle");
            if (button) setExpanded(button, detail, false);
          }
        });
        ensureEmptyRow(visibleCount === 0);
        if (state.roster === "current") {
          resultCount.innerHTML = "<strong>" + visibleCount + "</strong> universit" + (visibleCount === 1 ? "y" : "ies") + " with places this year";
        } else {
          resultCount.innerHTML = "<strong>" + visibleCount + "</strong> partner universit" + (visibleCount === 1 ? "y" : "ies") + " shown";
        }
        syncSortHeaders();
        if (filtersPinned && resultsFrame) {
          window.scrollTo({ top: Math.max(0, resultsTop - stickyInset), behavior: "instant" });
        }
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
      const mapViews = new WeakMap();
      const mapCache = new Map();
      let mapNextAt = 0;
      const tileSize = 256;
      const project = (lat, lon, zoom) => {
        const scale = tileSize * Math.pow(2, zoom);
        const sin = Math.sin(lat * Math.PI / 180);
        const clamped = Math.min(0.9999, Math.max(-0.9999, sin));
        return {
          x: (lon + 180) / 360 * scale,
          y: (0.5 - Math.log((1 + clamped) / (1 - clamped)) / (4 * Math.PI)) * scale,
        };
      };
      const unproject = (x, y, zoom) => {
        const scale = tileSize * Math.pow(2, zoom);
        const lon = x / scale * 360 - 180;
        const n = Math.PI - 2 * Math.PI * y / scale;
        const lat = 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
        return { lat, lon };
      };
      const nominatim = async (query) => {
        const wait = mapNextAt - Date.now();
        if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
        mapNextAt = Date.now() + 1100;
        const response = await fetch(
          "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q=" + encodeURIComponent(query),
          { headers: { Accept: "application/json" } }
        );
        if (!response.ok) throw new Error("lookup failed");
        const data = await response.json();
        return Array.isArray(data) && data.length ? data[0] : null;
      };
      const locatePlace = async (place) => {
        if (Number.isFinite(Number(place.lat)) && Number.isFinite(Number(place.lon))) {
          return { lat: Number(place.lat), lon: Number(place.lon), fixed: true };
        }
        const key = (place.query || "") + "\n" + (place.fallback || "");
        if (mapCache.has(key)) return mapCache.get(key);
        let hit = place.query ? await nominatim(place.query) : null;
        let cityOnly = false;
        if (!hit && place.fallback && place.fallback !== place.query) {
          hit = await nominatim(place.fallback);
          cityOnly = Boolean(hit);
        }
        const located = hit ? { lat: Number(hit.lat), lon: Number(hit.lon), fixed: false, cityOnly } : null;
        mapCache.set(key, located);
        return located;
      };
      const readPlaces = (panel) => {
        try { return JSON.parse(panel.dataset.mapPlaces || "[]"); }
        catch (error) { return []; }
      };
      const fitView = (points, width, height) => {
        if (!points.length) return { zoom: 14, originX: 0, originY: 0 };
        let zoom = points.length === 1 ? 15 : 16;
        const pad = 72;
        while (zoom > 3) {
          const projected = points.map((point) => project(point.lat, point.lon, zoom));
          const minX = Math.min(...projected.map((point) => point.x));
          const maxX = Math.max(...projected.map((point) => point.x));
          const minY = Math.min(...projected.map((point) => point.y));
          const maxY = Math.max(...projected.map((point) => point.y));
          if (maxX - minX + pad * 2 <= width && maxY - minY + pad * 2 <= height) break;
          zoom -= 1;
        }
        const projected = points.map((point) => project(point.lat, point.lon, zoom));
        const minX = Math.min(...projected.map((point) => point.x));
        const maxX = Math.max(...projected.map((point) => point.x));
        const minY = Math.min(...projected.map((point) => point.y));
        const maxY = Math.max(...projected.map((point) => point.y));
        return {
          zoom,
          originX: (minX + maxX) / 2 - width / 2,
          originY: (minY + maxY) / 2 - height / 2,
          fitted: true,
        };
      };
      const focusView = (point, width, height, zoom) => {
        const projected = project(point.lat, point.lon, zoom);
        return {
          zoom,
          originX: projected.x - width / 2,
          originY: projected.y - height / 2,
          fitted: false,
        };
      };
      const paintMap = (panel) => {
        const stage = panel.querySelector(".campus-map-stage");
        const map = panel.querySelector(".campus-map");
        if (!stage || !map || map.hidden) return;
        const view = mapViews.get(panel);
        const places = readPlaces(panel).filter((place) => Number.isFinite(Number(place.lat)));
        if (!view || !places.length) return;
        const width = stage.clientWidth || 640;
        const height = stage.clientHeight || 340;
        const zoom = view.zoom;
        const originX = view.originX;
        const originY = view.originY;
        const tiles = document.createElement("div");
        const x0 = Math.floor(originX / tileSize);
        const x1 = Math.floor((originX + width) / tileSize);
        const y0 = Math.floor(originY / tileSize);
        const y1 = Math.floor((originY + height) / tileSize);
        const maxIndex = Math.pow(2, zoom);
        for (let tx = x0; tx <= x1; tx += 1) {
          for (let ty = y0; ty <= y1; ty += 1) {
            if (ty < 0 || ty >= maxIndex) continue;
            const wrapped = ((tx % maxIndex) + maxIndex) % maxIndex;
            const img = document.createElement("img");
            img.className = "campus-tile";
            img.alt = "";
            img.width = tileSize;
            img.height = tileSize;
            img.style.left = (tx * tileSize - originX) + "px";
            img.style.top = (ty * tileSize - originY) + "px";
            img.src = "https://a.tile.openstreetmap.fr/osmfr/" + zoom + "/" + wrapped + "/" + ty + ".png";
            tiles.appendChild(img);
          }
        }
        const selected = Number(panel.dataset.mapIndex || 0);
        places.forEach((place, index) => {
          const projected = project(Number(place.lat), Number(place.lon), zoom);
          const x = projected.x - originX;
          const y = projected.y - originY;
          const button = document.createElement("button");
          button.type = "button";
          button.className = "campus-pin" + (index === selected ? " is-current" : "") + (y < 58 ? " is-below" : "");
          button.style.left = x + "px";
          button.style.top = y + "px";
          button.dataset.mapIndex = String(index);
          const name = place.label || place.name || "Campus";
          button.setAttribute("aria-label", name + (place.tag ? ", " + place.tag : ""));
          button.setAttribute("aria-pressed", String(index === selected));
          const card = document.createElement("span");
          card.className = "campus-pin-card";
          const nameEl = document.createElement("span");
          nameEl.className = "campus-pin-name";
          nameEl.textContent = name;
          card.appendChild(nameEl);
          if (place.tag) {
            const tagEl = document.createElement("span");
            tagEl.className = "campus-pin-tag";
            tagEl.textContent = place.tag;
            card.appendChild(tagEl);
          }
          const point = document.createElement("span");
          point.className = "campus-pin-point";
          point.setAttribute("aria-hidden", "true");
          button.append(card, point);
          tiles.appendChild(button);
        });
        stage.replaceChildren(tiles);
        const fitButton = panel.querySelector("[data-map-zoom='fit']");
        if (fitButton) fitButton.hidden = places.length < 2 || view.fitted;
      };
      const selectCampus = (panel, index, zoomIn) => {
        const places = readPlaces(panel);
        const place = places[index] || places[0];
        if (!place) return;
        panel.dataset.mapIndex = String(places.indexOf(place) < 0 ? 0 : index);
        panel.querySelectorAll(".map-place").forEach((button, buttonIndex) => {
          const current = buttonIndex === Number(panel.dataset.mapIndex);
          button.classList.toggle("is-current", current);
          button.setAttribute("aria-pressed", String(current));
        });
        const nameEl = panel.querySelector(".location-name");
        const metaEl = panel.querySelector(".location-meta");
        if (nameEl) nameEl.textContent = place.name || place.label || "";
        if (metaEl) {
          metaEl.textContent = place.meta || "";
          metaEl.hidden = !place.meta;
        }
        const note = panel.querySelector(".map-note");
        if (note) {
          note.textContent = place.address
            ? place.label + " · " + place.address
            : "Campus names sit on the pins. Choose one to see who studies there.";
        }
        const link = panel.querySelector(".map-external");
        const stage = panel.querySelector(".campus-map-stage");
        if (Number.isFinite(Number(place.lat)) && Number.isFinite(Number(place.lon))) {
          const lat = Number(place.lat);
          const lon = Number(place.lon);
          if (link) {
            link.href = "https://www.openstreetmap.org/?mlat=" + lat.toFixed(5) + "&mlon=" + lon.toFixed(5) + "#map=16/" + lat.toFixed(5) + "/" + lon.toFixed(5);
          }
          if (zoomIn && stage) {
            const width = stage.clientWidth || 640;
            const height = stage.clientHeight || 340;
            mapViews.set(panel, focusView(place, width, height, 15));
          }
        } else if (link) {
          link.href = "https://www.openstreetmap.org/search?query=" + encodeURIComponent(place.query || place.label || "");
        }
        paintMap(panel);
      };
      const showMap = async (panel) => {
        const status = panel.querySelector(".map-status");
        const map = panel.querySelector(".campus-map");
        const places = readPlaces(panel);
        if (status) {
          status.hidden = false;
          status.textContent = "Looking up this campus…";
        }
        try {
          const located = [];
          for (const place of places) {
            const hit = await locatePlace(place);
            if (hit && Number.isFinite(hit.lat) && Number.isFinite(hit.lon)) {
              place.lat = hit.lat;
              place.lon = hit.lon;
              place.cityOnly = Boolean(hit.cityOnly);
              located.push(place);
            }
          }
          panel.dataset.mapPlaces = JSON.stringify(places);
          if (!located.length) {
            if (status) {
              status.hidden = false;
              status.textContent = "OpenStreetMap has no match for this campus. Open the search to look nearby.";
            }
            if (map) map.hidden = true;
            return;
          }
          if (status) status.hidden = located.length === places.length;
          if (status && located.length !== places.length) {
            status.hidden = false;
            status.textContent = "Some campuses could not be placed. The named pins are the ones OpenStreetMap matched.";
          }
          if (map) map.hidden = false;
          await new Promise((resolve) => requestAnimationFrame(resolve));
          const stage = panel.querySelector(".campus-map-stage");
          const width = stage && stage.clientWidth ? stage.clientWidth : 640;
          const height = stage && stage.clientHeight ? stage.clientHeight : 340;
          mapViews.set(panel, fitView(located, width, height));
          const index = Number(panel.dataset.mapIndex || 0);
          selectCampus(panel, Number.isFinite(index) ? index : 0, false);
          if (status && located.length === places.length) status.hidden = true;
        } catch (error) {
          if (status) {
            status.hidden = false;
            status.textContent = "The map could not be loaded. Check your connection, then try again.";
          }
        }
      };
      const zoomMap = (panel, nextZoom, anchorX, anchorY) => {
        const stage = panel.querySelector(".campus-map-stage");
        const view = mapViews.get(panel);
        if (!stage || !view) return;
        const width = stage.clientWidth || 640;
        const height = stage.clientHeight || 340;
        const ax = Number.isFinite(anchorX) ? anchorX : width / 2;
        const ay = Number.isFinite(anchorY) ? anchorY : height / 2;
        const zoom = Math.max(3, Math.min(18, Math.round(nextZoom)));
        if (zoom === view.zoom) return;
        const before = unproject(view.originX + ax, view.originY + ay, view.zoom);
        const projected = project(before.lat, before.lon, zoom);
        view.zoom = zoom;
        view.originX = projected.x - ax;
        view.originY = projected.y - ay;
        view.fitted = false;
        paintMap(panel);
      };
      const bindMapStage = (panel) => {
        const stage = panel.querySelector(".campus-map-stage");
        if (!stage || stage.dataset.bound === "true") return;
        stage.dataset.bound = "true";
        let drag = null;
        let wheelBucket = 0;
        stage.addEventListener("wheel", (event) => {
          if (!mapViews.has(panel)) return;
          event.preventDefault();
          const rect = stage.getBoundingClientRect();
          wheelBucket += event.deltaY;
          const threshold = event.ctrlKey ? 28 : 70;
          if (Math.abs(wheelBucket) < threshold) return;
          const direction = wheelBucket > 0 ? -1 : 1;
          wheelBucket = 0;
          const view = mapViews.get(panel);
          zoomMap(panel, view.zoom + direction, event.clientX - rect.left, event.clientY - rect.top);
        }, { passive: false });
        stage.addEventListener("gesturestart", (event) => {
          event.preventDefault();
          const view = mapViews.get(panel);
          if (view) stage.dataset.gestureZoom = String(view.zoom);
        });
        stage.addEventListener("gesturechange", (event) => {
          event.preventDefault();
          const start = Number(stage.dataset.gestureZoom);
          if (!Number.isFinite(start)) return;
          const rect = stage.getBoundingClientRect();
          zoomMap(panel, start + Math.log2(event.scale || 1), event.clientX - rect.left, event.clientY - rect.top);
        });
        stage.addEventListener("pointerdown", (event) => {
          if (event.target.closest(".campus-pin")) return;
          const view = mapViews.get(panel);
          if (!view) return;
          drag = { id: event.pointerId, x: event.clientX, y: event.clientY, originX: view.originX, originY: view.originY };
          stage.classList.add("is-dragging");
          stage.setPointerCapture(event.pointerId);
        });
        stage.addEventListener("pointermove", (event) => {
          if (!drag || drag.id !== event.pointerId) return;
          const view = mapViews.get(panel);
          if (!view) return;
          view.originX = drag.originX - (event.clientX - drag.x);
          view.originY = drag.originY - (event.clientY - drag.y);
          view.fitted = false;
          paintMap(panel);
        });
        const endDrag = (event) => {
          if (!drag || drag.id !== event.pointerId) return;
          drag = null;
          stage.classList.remove("is-dragging");
        };
        stage.addEventListener("pointerup", endDrag);
        stage.addEventListener("pointercancel", endDrag);
      };
      const tableFrame = document.querySelector(".table-frame");
      const toggleFromRow = (row, selector) => {
        const button = row.querySelector(selector);
        if (button) toggleButton(button);
      };
      tableFrame.addEventListener("click", (event) => {
        const emptyReset = event.target.closest("[data-empty-reset]");
        if (emptyReset) { reset(); return; }
        const zoomButton = event.target.closest("[data-map-zoom]");
        if (zoomButton) {
          const panel = zoomButton.closest(".location-panel");
          const stage = panel && panel.querySelector(".campus-map-stage");
          const view = panel && mapViews.get(panel);
          if (!panel || !stage || !view) return;
          const width = stage.clientWidth || 640;
          const height = stage.clientHeight || 340;
          const action = zoomButton.dataset.mapZoom;
          if (action === "fit") {
            const points = readPlaces(panel).filter((place) => Number.isFinite(Number(place.lat)));
            mapViews.set(panel, fitView(points, width, height));
            paintMap(panel);
            return;
          }
          const nextZoom = action === "in" ? view.zoom + 1 : view.zoom - 1;
          zoomMap(panel, nextZoom, width / 2, height / 2);
          return;
        }
        const campusPin = event.target.closest(".campus-pin");
        if (campusPin) {
          const panel = campusPin.closest(".location-panel");
          if (panel) selectCampus(panel, Number(campusPin.dataset.mapIndex || 0), true);
          return;
        }
        const placeButton = event.target.closest(".map-place");
        if (placeButton) {
          const panel = placeButton.closest(".location-panel");
          if (!panel) return;
          const toggle = panel.querySelector(".map-toggle");
          const frame = panel.querySelector(".map-frame");
          if (toggle && toggle.getAttribute("aria-expanded") !== "true") {
            toggle.setAttribute("aria-expanded", "true");
            toggle.textContent = "Hide map";
            if (frame) frame.hidden = false;
            bindMapStage(panel);
            showMap(panel).then(() => selectCampus(panel, Number(placeButton.dataset.mapIndex || 0), true));
            return;
          }
          selectCampus(panel, Number(placeButton.dataset.mapIndex || 0), true);
          return;
        }
        const mapToggle = event.target.closest(".map-toggle");
        if (mapToggle) {
          const panel = mapToggle.closest(".location-panel");
          const frame = document.getElementById(mapToggle.getAttribute("aria-controls") || "");
          const expanded = mapToggle.getAttribute("aria-expanded") === "true";
          mapToggle.setAttribute("aria-expanded", String(!expanded));
          mapToggle.textContent = expanded ? "Show map" : "Hide map";
          if (frame) frame.hidden = expanded;
          if (!expanded && panel) {
            bindMapStage(panel);
            const map = panel.querySelector(".campus-map");
            if (!map || map.hidden || !mapViews.has(panel)) showMap(panel);
          }
          return;
        }
        if (event.target.closest("a, summary, .help-trigger, .sort-button, .campus-map")) return;
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
    active_groups_count = sum(1 for g in groups if g.get("offers_places"))
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
