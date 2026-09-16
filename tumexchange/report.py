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
    "measured": "Measured",
    "no_applications": "Assigned 100%",
    "ambiguous": "Ambiguous",
    "missing": "N/A",
    "no_cooperation": "N/A",
    "no_places": "N/A",
    "bar_absent": "N/A",
}

_CATEGORY_LABELS = {
    "measured": "Measured",
    "assigned": "Assigned 100%",
    "ambiguous": "Ambiguous",
    "na": "N/A",
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
    while True:
        match = re.search(r"\s*\(([^()]*)\)\s*$", text)
        if match is None:
            break
        qualifier = match.group(1).strip()
        if not (
            qualifier.casefold().startswith("planned transfer ")
            or re.fullmatch(r"[A-Z0-9&.+-]{2,16}", qualifier)
        ):
            break
        text = text[: match.start()].rstrip()
    return " ".join(text.casefold().split())


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


def _render_annual_row(
    record: Mapping[str, Any], group_index: int, annual_index: int
) -> str:
    normalized = _normalize_record(record, annual_index)
    score = _score_value(normalized)
    score_label = f"{score:.1f}%" if score is not None else "N/A"
    score_unit = "of full bar" if score is not None else ""
    category = _status_category(normalized.get("status"))
    detail_id = f"annual-detail-{group_index}-{annual_index}"
    track_text = (
        f"{score_label} of a full reference bar"
        if score is not None
        else "N/A; bar not measured"
    )
    return f"""
          <tr class="annual-data-row" data-annual-index="{annual_index}">
            <td class="numeric" data-label="Year">{_escaped(normalized.get("year"))}</td>
            <td data-label="Status"><span class="status-pill status-{html.escape(category, quote=True)}">{html.escape(_CATEGORY_LABELS.get(category, "N/A"), quote=True)}</span></td>
            <td class="score-cell" data-label="Normalized score"><div class="score-stack"><div class="score-line"><span class="score-value">{score_label}</span><span class="score-unit">{score_unit}</span></div><div class="score-track{' is-empty' if score is None else ''}" aria-label="{html.escape(track_text, quote=True)}"><span class="score-fill" style="width: {_score_for_width(score):.4f}%"></span></div></div></td>
            <td class="source-cell" data-label="Source">{_source_cell(normalized)}</td>
            <td class="annual-status-reason" data-label="Raw status">{html.escape(_status_key(normalized.get("status")), quote=True)}</td>
            <td class="calibration-cell" data-label="Calibration">{_escaped(normalized.get("calibration_id"), "Not recorded")}</td>
            <td class="details-cell" data-label="Measurement"><button class="detail-toggle annual-detail-toggle" type="button" aria-expanded="false" aria-controls="{detail_id}">View details</button></td>
          </tr>
          <tr id="{detail_id}" class="annual-detail-row" hidden><td colspan="7">{_render_details(normalized, annual_index)}</td></tr>
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
              <div><span class="detail-kicker">Partner profile</span><h3>No profile data available</h3></div>
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
            <div><span class="detail-kicker">Partner profile</span><h3>TUM partner details</h3></div>
            <span class="profile-panel-meta">{len(profiles)} {relation_label}{subject_note}</span>
          </div>
          <div class="profile-relations">{cards}</div>
        </section>
    '''


def _render_university_row(group: Mapping[str, Any], group_index: int) -> str:
    score = group.get("mean_score")
    score_label = f"{score:.1f}%" if score is not None else "N/A"
    score_unit = "mean · full bar" if score is not None else "no scored years"
    detail_id = f"university-detail-{group_index}"
    scored = int(group.get("scored_count") or 0)
    total = int(group.get("total_count") or 0)
    records = list(group.get("records") or [])
    annual_rows = "".join(
        _render_annual_row(record, group_index, annual_index)
        for annual_index, record in enumerate(records)
    )
    if records:
        annual_content = (
            '<table class="annual-table"><thead><tr><th scope="col">Year</th><th scope="col">Status</th>'
            '<th scope="col">Normalized score</th><th scope="col">Source release / document</th>'
            '<th scope="col">Raw status</th><th scope="col">Calibration</th><th scope="col">'
            '<span class="visually-hidden">Measurement details</span></th></tr></thead><tbody>'
            + annual_rows
            + "</tbody></table>"
        )
    else:
        annual_content = (
            '<div class="historical-empty"><strong>No historical data</strong>'
            '<p>This institution is present in the current profile catalogue, but no extracted annual bar observations are available.</p></div>'
        )
    source_count = int(group.get("source_count") or 0)
    roster_note = ""
    if not bool(group.get("is_latest_roster", True)):
        latest_release = group.get("latest_release_year")
        label = (
            f"Not listed in release {latest_release}"
            if latest_release is not None
            else "Not listed in the latest release"
        )
        roster_note = f'<span class="roster-note">{html.escape(label, quote=True)}</span>'
    return f"""
      <tr class="data-row university-row" data-group-index="{group_index}">
        <td class="university-cell" data-label="University"><button class="university-toggle" type="button" aria-expanded="false" aria-controls="{detail_id}"><span><span class="university-name">{_escaped(group.get("university"), "Unnamed university")}</span>{roster_note}{_render_profile_summary(group)}</span><span class="expand-label">View profile &amp; history</span></button></td>
        <td data-label="Country">{_escaped(group.get("country"))}</td>
        <td class="numeric years-cell" data-label="Years scored"><strong>{scored} of {total}</strong><span>years scored</span></td>
        <td data-label="Annual statuses"><div class="status-list">{_render_status_pills(group.get("categories") or [])}</div></td>
        <td class="score-cell" data-label="Mean score"><div class="score-stack"><div class="score-line"><span class="score-value">{score_label}</span><span class="score-unit">{score_unit}</span></div><div class="score-track{' is-empty' if score is None else ''}" aria-label="{html.escape((score_label + ' mean of full bar') if score is not None else 'N/A; no scored years', quote=True)}"><span class="score-fill" style="width: {_score_for_width(score):.4f}%"></span></div></div></td>
        <td class="source-cell source-summary-cell" data-label="Source releases">{_source_summary(group)}</td>
        <td class="details-cell" data-label="Profile and history"><button class="detail-toggle university-detail-toggle" type="button" aria-expanded="false" aria-controls="{detail_id}">View profile &amp; history</button></td>
      </tr>
      <tr id="{detail_id}" class="university-detail-row" data-group-detail="{group_index}" hidden><td colspan="7">{_render_profile_panel(group, group_index)}<section class="annual-panel" aria-label="Historical annual data"><div class="annual-heading"><strong>Historical annual entries, newest first</strong><span>{scored} of {total} years contribute to the mean · {source_count} source release{'s' if source_count != 1 else ''}</span></div>{annual_content}</section></td></tr>
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
            '<td colspan="7"><div class="empty-state"><strong>No extracted rows yet</strong>'
            "<p>Run the extractor to populate this report.</p></div></td></tr>"
        )
    catalog = _calibration_catalog(current_records, calibration)
    source_keys = {
        _source_descriptor(record)["key"] for record in current_records
    }
    source_count = len(source_keys)
    scored_entries = sum(_score_value(record) is not None for record in current_records)
    calibration_label = _calibration_label(catalog)
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
  <title>TUMexchange bar comparison</title>
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
    .method-card p { margin-bottom: 0; color: #d5e2eb; font-size: .86rem; }
    .summary {
      display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 1px;
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
      position: absolute; z-index: 10; top: 40px; right: 0; width: min(255px, calc(100vw - 40px));
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
    .reset-button:hover { background: var(--blue-soft); }
    .result-line {
      display: flex; justify-content: space-between; gap: 16px; margin-bottom: 12px;
      color: var(--muted); font-size: .82rem;
    }
    .result-line strong { color: var(--ink); }
    .table-frame { overflow-x: auto; background: var(--paper); border: 1px solid var(--line); box-shadow: var(--shadow); }
    table { width: 100%; min-width: 1120px; border-collapse: collapse; text-align: left; }
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
    tr.university-row, tr.annual-data-row { cursor: pointer; }
    .university-cell { width: 23%; color: var(--ink); }
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
    .years-cell span { display: block; color: var(--muted); font-size: .72rem; }
    .numeric { font-variant-numeric: tabular-nums; }
    .source-cell { color: var(--muted); }
    .source-cell-inner { display: grid; gap: 2px; min-width: 145px; }
    .source-release { color: var(--blue-deep); font-size: .74rem; font-weight: 700; white-space: nowrap; }
    .source-document { display: block; max-width: 210px; overflow: hidden; color: var(--muted); font-size: .73rem; text-overflow: ellipsis; white-space: nowrap; }
    .source-summary-cell { min-width: 170px; }
    .status-list { display: flex; flex-wrap: wrap; gap: 5px; }
    .status-pill {
      display: inline-flex; align-items: center; min-height: 26px; padding: 3px 8px;
      border-radius: 2px; font-size: .73rem; font-weight: 700; white-space: nowrap;
    }
    .status-measured { background: var(--blue-soft); color: var(--blue-deep); }
    .status-assigned { background: var(--green-soft); color: var(--green); }
    .status-na { background: #edf1f3; color: #60717b; }
    .status-ambiguous { background: var(--amber-soft); color: var(--amber); }
    .score-cell { min-width: 220px; }
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
    .details-cell { width: 112px; text-align: right; }
    .detail-toggle {
      min-height: 44px; padding: 8px 0; border: 0; border-bottom: 1px solid #91adc0;
      background: transparent; color: var(--blue-deep); font-size: .82rem; font-weight: 650;
    }
    .detail-toggle:hover { border-bottom-color: var(--blue-deep); }
    .university-detail-row > td, .annual-detail-row > td { padding: 0; background: #f8fbfd; }
    .profile-panel { padding: 20px 22px 18px; border-left: 4px solid #79b4df; background: var(--paper); }
    .profile-panel-empty { border-left-color: #a9bac5; }
    .profile-panel-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 20px; margin-bottom: 13px; }
    .profile-panel-heading h3 { margin: 0; font-family: Georgia, serif; font-size: 1.2rem; font-weight: 400; }
    .profile-panel-meta, .profile-retrieved { color: var(--muted); font-size: .78rem; }
    .profile-empty-copy { max-width: 720px; margin: 0; color: var(--muted); font-size: .84rem; }
    .profile-relations { display: grid; gap: 8px; }
    .profile-relation { border: 1px solid var(--line); background: #fbfdfe; }
    .profile-relation > summary { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 12px 14px; cursor: pointer; list-style-position: inside; color: var(--ink); }
    .profile-relation > summary::marker { color: var(--blue); }
    .profile-relation > summary { position: relative; padding-right: 48px; }
    .profile-relation > summary::after { content: "+"; position: absolute; right: 16px; top: 10px; color: var(--blue); font-size: 1.4rem; }
    .profile-relation[open] > summary::after { content: "−"; }
    .profile-open-hint { flex-basis: 100%; color: var(--blue); font-size: .75rem; }
    .profile-hint-open, .profile-relation[open] .profile-hint-closed { display: none; }
    .profile-relation[open] .profile-hint-open { display: inline; }
    .profile-relation > summary:hover { background: var(--blue-soft); }
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
    .historical-empty { padding: 23px 18px; border: 1px dashed var(--line); background: var(--paper); color: var(--muted); }
    .historical-empty strong { display: block; margin-bottom: 3px; color: var(--ink); }
    .historical-empty p { margin: 0; font-size: .83rem; }
    .annual-panel { padding: 14px 18px 18px; border-left: 4px solid var(--blue); }
    .annual-heading { display: flex; justify-content: space-between; gap: 12px; margin: 0 0 8px; color: var(--muted); font-size: .8rem; }
    .annual-heading strong { color: var(--ink); }
    .annual-table { min-width: 920px; border: 1px solid var(--line); box-shadow: none; }
    .annual-table th { padding: 9px 11px; font-size: .66rem; }
    .annual-table td { padding: 9px 11px; font-size: .82rem; }
    .annual-status-reason { color: var(--muted); font: .72rem ui-monospace, SFMono-Regular, Menlo, monospace; white-space: nowrap; }
    .calibration-cell { max-width: 190px; overflow-wrap: anywhere; color: var(--muted); font-size: .72rem; }
    .detail-panel { padding: 22px 24px 25px; border-left: 4px solid var(--blue); }
    .detail-heading { display: flex; justify-content: space-between; gap: 24px; align-items: baseline; margin-bottom: 18px; }
    .detail-heading h3 { margin-bottom: 0; font-family: Georgia, serif; font-size: 1.2rem; font-weight: 400; }
    .detail-heading a { font-size: .82rem; font-weight: 650; white-space: nowrap; }
    .detail-grid { display: grid; grid-template-columns: repeat(4, minmax(130px, 1fr)); gap: 12px; margin: 0 0 17px; }
    .detail-grid div { min-width: 0; padding-right: 12px; border-right: 1px solid var(--line); }
    .detail-grid dt { margin-bottom: 3px; color: var(--muted); font-size: .72rem; }
    .detail-grid dd { margin: 0; color: var(--ink); font-size: .84rem; font-variant-numeric: tabular-nums; }
    .technical-value { color: var(--muted); font-size: .72rem; }
    .breakable { overflow-wrap: anywhere; }
    .detail-note, .detail-formula { margin: 0 0 10px; color: #4d6472; font-size: .83rem; }
    .detail-formula { color: var(--muted); font-style: italic; }
    .raw-record-details { margin-top: 18px; border-top: 1px solid var(--line); }
    .raw-record-details summary { padding-top: 13px; color: var(--blue-deep); cursor: pointer; font-size: .82rem; font-weight: 650; }
    .raw-record { overflow: auto; max-height: 330px; margin: 12px 0 0; padding: 13px; background: #edf3f7; color: #27465a; font: .75rem/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; }
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
      .summary { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .controls { grid-template-columns: repeat(3, minmax(145px, 1fr)); }
      .search-field { grid-column: 1 / -1; }
    }
    @media (max-width: 960px) {
      .shell { width: min(100% - 28px, 1440px); }
      .hero { grid-template-columns: 1fr; gap: 22px; padding: 28px 0 18px; }
      .utility { align-items: flex-start; flex-direction: column; gap: 10px; padding: 14px 0; }
      .downloads { gap: 12px; }
      .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .summary dd { white-space: normal; }
      .controls { grid-template-columns: 1fr 1fr; }
      .search-field, .reset-button { grid-column: 1 / -1; }
      .result-line, footer, .annual-heading, .detail-heading { align-items: flex-start; flex-direction: column; gap: 6px; }
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
      tr.university-detail-row, tr.annual-detail-row, tr.empty-row { display: block; }
      tr.university-detail-row > td, tr.annual-detail-row > td, tr.empty-row > td {
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
      .university-cell, .score-cell, .source-summary-cell, .source-cell-inner, .details-cell { width: auto; min-width: 0; }
      .years-cell span { display: none; }
      .university-row .source-document { display: none; }
      .summary > div:last-child { grid-column: 1 / -1; }
      .score-cell, .score-track, .source-document, .source-cell-inner { max-width: none; }
      .source-document { white-space: normal; }
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
      tr.annual-data-row > .annual-status-reason, tr.annual-data-row > .calibration-cell { display: none; }
      .details-cell { text-align: left; }
    }
    @media (max-width: 840px) {
      .hero { grid-template-columns: 1fr; gap: 24px; }
      .detail-grid { grid-template-columns: repeat(2, minmax(130px, 1fr)); }
    }
    @media (max-width: 620px) {
      .summary { grid-template-columns: 1fr 1fr; }
      .detail-heading { align-items: flex-start; flex-direction: column; gap: 10px; }
      .detail-grid { grid-template-columns: 1fr; }
      .detail-grid div { border-right: 0; padding-right: 0; }
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
      .university-detail-row[hidden], .annual-detail-row[hidden] { display: table-row !important; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <nav class="utility" aria-label="Report files">
      <div class="brand"><span class="brand-mark" aria-hidden="true">T</span><span>TUMexchange / bar register</span></div>
      <div class="downloads"><a href="rows.csv" download>Download rows.csv</a><a href="calibration.json" download>Download calibration.json</a></div>
    </nav>
    <header class="hero">
      <div>
        <p class="eyebrow">Offline comparison report</p>
        <h1>TUMexchange bar comparison</h1>
        <p class="lede">Compare historical selection scores and explore TUM's partner requirements. Filter by degree and subject, or search for a university, city, or practical detail.</p>
      </div>
      <aside class="method-card" aria-labelledby="method-heading">
        <p class="eyebrow">Interpretation</p>
        <h2 id="method-heading">What the bars mean</h2>
        <p>This is a normalized historical selection-ratio indicator under a linear bar-width model. A full bar is assigned 100%; half the calibrated width is 50%. “No applications” is explicitly assigned 100%. This is not an individual admission probability and is not a grade-based prediction yet.</p>
      </aside>
    </header>
    <dl class="summary" aria-label="Report summary">
      <div><dt>Universities</dt><dd id="stat-universities">@@UNIVERSITIES@@</dd></div>
      <div><dt>Annual entries</dt><dd id="stat-annual">@@ANNUAL@@</dd></div>
      <div><dt>Scored year entries</dt><dd id="stat-scored">@@SCORED@@</dd></div>
      <div><dt>Source releases</dt><dd id="stat-sources">@@SOURCES@@</dd></div>
      <div><dt>Calibrations</dt><dd id="stat-calibration" title="@@CALIBRATION_LABEL@@">@@CALIBRATIONS@@</dd></div>
    </dl>
    <section aria-labelledby="filters-heading">
      <h2 id="filters-heading" class="filter-heading">Find a university</h2>
      <div class="controls">
        <div class="field search-field"><label for="search">Search university, country, city, subject, year, or source</label><input id="search" type="search" placeholder="UFRJ, São Paulo, Brazil, 2024" autocomplete="off" spellcheck="false"></div>
        <div class="field"><label for="country-filter">Country</label><select id="country-filter">@@COUNTRY_OPTIONS@@</select></div>
        <div class="field">
          <span class="label-with-help">
            <label for="status-filter">Status</label>
            <span class="help">
              <button class="help-trigger" type="button" aria-label="Explain assigned 100% status" aria-describedby="assigned-help">i</button>
              <span class="help-tooltip" id="assigned-help" role="tooltip">Assigned 100% means no applications were reported; it is treated as 100% under the explicit project assumption. It is not a measured numeric bar width.</span>
            </span>
          </span>
          <select id="status-filter">@@STATUS_OPTIONS@@</select>
        </div>
        <div class="field"><label for="degree-filter">Degree level</label><select id="degree-filter"><option value="">All degree levels</option><option value="bachelor">Bachelor</option><option value="master">Master</option><option value="unknown">Unknown</option></select></div>
        <div class="field"><label for="subject-filter">Subject area</label><select id="subject-filter">@@SUBJECT_OPTIONS@@</select></div>
        <button class="reset-button" type="button" id="reset-filters">Reset filters</button>
      </div>
      <div class="result-line" aria-live="polite"><span id="result-count"><strong>@@UNIVERSITIES@@</strong> universit@@UNIVERSITY_SUFFIX@@ shown</span><span>Means include measured and assigned 100% years; N/A years are excluded.</span></div>
      <div class="mobile-sort" role="group" aria-label="Sort this list">
        <span class="mobile-sort-label">Sort</span>
        <button type="button" class="mobile-sort-button" data-sort-column="university" aria-pressed="false">University<span class="sort-indicator" aria-hidden="true"></span></button>
        <button type="button" class="mobile-sort-button" data-sort-column="country" aria-pressed="false">Country<span class="sort-indicator" aria-hidden="true"></span></button>
        <button type="button" class="mobile-sort-button" data-sort-column="scored" aria-pressed="false">Years scored<span class="sort-indicator" aria-hidden="true"></span></button>
        <button type="button" class="mobile-sort-button" data-sort-column="score" aria-pressed="false">Mean score<span class="sort-indicator" aria-hidden="true"></span></button>
      </div>
    </section>
    <div class="table-frame">
      <table id="report-table">
        <caption class="visually-hidden">TUMexchange universities with annual normalized bar scores and source provenance</caption>
        <thead><tr><th scope="col" class="sortable" data-sort-column="university" aria-sort="none"><button type="button" class="sort-button">University<span class="sort-indicator" aria-hidden="true"></span></button></th><th scope="col" class="sortable" data-sort-column="country" aria-sort="none"><button type="button" class="sort-button">Country<span class="sort-indicator" aria-hidden="true"></span></button></th><th scope="col" class="sortable" data-sort-column="scored" aria-sort="none"><button type="button" class="sort-button">Years scored<span class="sort-indicator" aria-hidden="true"></span></button></th><th scope="col">Annual statuses</th><th scope="col" class="sortable" data-sort-column="score" aria-sort="none"><button type="button" class="sort-button">Mean normalized score<span class="sort-indicator" aria-hidden="true"></span></button></th><th scope="col">Source releases</th><th scope="col"><span class="visually-hidden">Annual rows</span></th></tr></thead>
        <tbody id="rows-body">@@INITIAL_ROWS@@</tbody>
      </table>
    </div>
    <footer><p>Report generated for visual bar comparison.</p><p>Calibration metadata is source-specific: @@CALIBRATION_LABEL@@</p></footer>
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
      const state = { search: "", country: "", status: "", degree: "", subject: "", sort: "source" };
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
      const categoryMatches = (group) => !state.status || (group.categories || []).includes(state.status);
      const profileMatches = (group) => {
        if (!state.degree && !state.subject) return true;
        const profiles = Array.isArray(group.profiles) ? group.profiles : [];
        if (!profiles.length) return state.degree === "unknown" && !state.subject;
        return profiles.some((profile) => {
          const levels = Array.isArray(profile.levels) ? profile.levels.map((value) => text(value).toLowerCase()) : [];
          const knownLevels = levels.filter((value) => value === "bachelor" || value === "master");
          const degreeMatches = !state.degree
            || (state.degree === "unknown" ? knownLevels.length === 0 : knownLevels.includes(state.degree));
          const subjects = Array.isArray(profile.subject_areas) ? profile.subject_areas : [];
          const subjectMatches = !state.subject || subjects.some((value) => text(value) === state.subject);
          return degreeMatches && subjectMatches;
        });
      };
      const matches = (group) => {
        if (state.country && text(group.country) !== state.country) return false;
        if (!categoryMatches(group)) return false;
        if (!profileMatches(group)) return false;
        return searchMatches(group);
      };
      const compareText = (a, b) => text(a).localeCompare(text(b), undefined, { sensitivity: "base", numeric: true });
      const compareNullable = (av, bv, descending) => {
        if (av === null && bv !== null) return 1;
        if (av !== null && bv === null) return -1;
        if (av !== null && bv !== null && av !== bv) return descending ? bv - av : av - bv;
        return 0;
      };
      const compareGroups = (a, b, sortKey) => {
        const sourceOrder = Number(a.index) - Number(b.index);
        if (sortKey === "source") return sourceOrder;
        if (sortKey === "university-asc" || sortKey === "university-desc") {
          const direction = sortKey === "university-desc" ? -1 : 1;
          return direction * compareText(a.university, b.university) || sourceOrder;
        }
        if (sortKey === "country-asc" || sortKey === "country-desc") {
          const direction = sortKey === "country-desc" ? -1 : 1;
          return direction * compareText(a.country, b.country) || compareText(a.university, b.university) || sourceOrder;
        }
        if (sortKey === "scored-desc" || sortKey === "scored-asc") {
          return compareNullable(numeric(a.scored_count), numeric(b.scored_count), sortKey === "scored-desc") || compareText(a.university, b.university) || sourceOrder;
        }
        if (sortKey === "score-desc" || sortKey === "score-asc") {
          return compareNullable(numeric(a.mean_score), numeric(b.mean_score), sortKey === "score-desc") || compareText(a.university, b.university) || sourceOrder;
        }
        return sourceOrder;
      };
      const orderedGroups = () => groups.slice().sort((a, b) => compareGroups(a, b, state.sort));
      const syncSortHeaders = () => {
        sortHeaders.forEach((header) => {
          const column = sortColumns[header.dataset.sortColumn];
          let value = "none";
          if (column && state.sort === column.asc) value = "ascending";
          if (column && state.sort === column.desc) value = "descending";
          header.setAttribute("aria-sort", value);
        });
        mobileSortButtons.forEach((button) => {
          const column = sortColumns[button.dataset.sortColumn];
          const active = Boolean(column && (state.sort === column.asc || state.sort === column.desc));
          button.classList.toggle("is-active", active);
          button.setAttribute("aria-pressed", String(active));
          const indicator = button.querySelector(".sort-indicator");
          if (!indicator || !column) return;
          indicator.textContent = state.sort === column.asc ? "▲" : state.sort === column.desc ? "▼" : "";
        });
      };
      const cycleSort = (columnName) => {
        const column = sortColumns[columnName];
        if (!column) return;
        const first = column[column.first];
        const second = column.first === "asc" ? column.desc : column.asc;
        state.sort = state.sort === first ? second : first;
        render();
      };
      const setExpanded = (button, detail, expanded) => {
        detail.hidden = !expanded;
        const detailId = button.getAttribute("aria-controls");
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
          row.innerHTML = "<td colspan=\"7\"><div class=\"empty-state\"><strong></strong><p></p><button class=\"reset-button\" type=\"button\" data-empty-reset>Reset filters</button></div></td>";
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
          if (!shown) detail.hidden = true;
          fragment.append(summary, detail);
        });
        body.appendChild(fragment);
        ensureEmptyRow(visible.length === 0);
        resultCount.innerHTML = "<strong>" + visible.length + "</strong> universit" + (visible.length === 1 ? "y" : "ies") + " shown";
        syncSortHeaders();
      };
      const reset = () => {
        state.search = "";
        state.country = "";
        state.status = "";
        state.degree = "";
        state.subject = "";
        state.sort = "source";
        search.value = "";
        countryFilter.value = "";
        statusFilter.value = "";
        degreeFilter.value = "";
        subjectFilter.value = "";
        render();
        search.focus();
      };
      search.addEventListener("input", () => { state.search = search.value; render(); });
      countryFilter.addEventListener("change", () => { state.country = countryFilter.value; render(); });
      statusFilter.addEventListener("change", () => { state.status = statusFilter.value; render(); });
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
        const annualRow = event.target.closest("tr.annual-data-row");
        if (annualRow) {
          toggleFromRow(annualRow, ".annual-detail-toggle");
          return;
        }
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
      render();
    })();
  </script>
</body>
</html>
"""
    replacements = {
        "@@UNIVERSITIES@@": str(len(groups)),
        "@@ANNUAL@@": str(len(current_records)),
        "@@SCORED@@": str(scored_entries),
        "@@SOURCES@@": str(source_count),
        "@@CALIBRATIONS@@": str(len(catalog)),
        "@@CALIBRATION_LABEL@@": html.escape(calibration_label, quote=True),
        "@@COUNTRY_OPTIONS@@": _select_options(groups, "country", "All countries"),
        "@@STATUS_OPTIONS@@": _select_options(current_records, "status", "All statuses"),
        "@@SUBJECT_OPTIONS@@": profile_subject_options,
        "@@INITIAL_ROWS@@": initial_rows,
        "@@REPORT_DATA@@": _safe_json(data_payload),
        "@@UNIVERSITY_SUFFIX@@": "y" if len(groups) == 1 else "ies",
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
