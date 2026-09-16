"""Validate and join the external MoveON university profile catalog.

The PDF bars and the MoveON directory describe different kinds of facts.  The
former are historical, release-scoped observations; the latter is a mutable
catalogue of current exchange relations.  This module only joins the two at
report-build time.  It deliberately keeps relation IDs and does not make
fuzzy name matches.
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class ProfileValidationError(ValueError):
    """Raised when a saved profile catalog or reviewed match is invalid."""


_LEVELS = ("bachelor", "master")
_SUPPORTED_SCHEMA_VERSION = "moveon-catalog-v1"
_FACULTY_WORDS = {
    "campus",
    "college",
    "department",
    "faculty",
    "graduate",
    "school",
    "science",
    "engineering",
    "information",
    "technology",
    "institute",
    "program",
    "programme",
}

# The bar PDFs use a few short country labels while MoveON uses country names.
# Keys and values are intentionally normalized through _country_key below.
_COUNTRY_ALIASES = {
    "uk": "united kingdom",
    "great britain": "united kingdom",
    "england": "united kingdom",
    "usa": "united states",
    "us": "united states",
    "u.s.a.": "united states",
    "korea": "korea, republic of",
    "south korea": "korea, republic of",
    "burkina faso": "burkina faso",
    "burkina faso republic": "burkina faso",
    "czechia": "czech republic",
    "russian federation": "russia",
    "hong kong": "china",
    "taiwan": "taiwan",
}

_PROFILE_FIELDS = {
    "institution_id",
    "relation_id",
    "name",
    "country",
    "city",
    "levels",
    "subject_areas",
    "fields",
    "source_url",
    "details_url",
    "retrieved_at",
}


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _text(value)).strip().casefold()
    return " ".join(text.split())


def _country_key(value: Any) -> str:
    text = _fold(value)
    text = re.sub(r"[.·]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return _COUNTRY_ALIASES.get(text, text)


def _acronym_qualifier(qualifier: str) -> bool:
    """Return true for a parenthesized short alias, preserving departments."""

    raw = _text(qualifier).strip()
    q = _fold(raw)
    if not q or any(word in q.split() for word in _FACULTY_WORDS):
        return False
    if re.fullmatch(r"[A-Z0-9&.+-]{2,16}", raw):
        return True
    # Common aliases can contain spaces, e.g. ``UC Davis`` or ``UC Chile``.
    words = q.split()
    if len(words) > 3 or len(q) > 24:
        return False
    # Mixed-case or title-case parentheticals are meaningful qualifiers unless
    # they begin with an explicit all-caps acronym (for example ``UC Davis``
    # or ``IIT Bombay``).  This keeps ``(Law)`` and ``(Lima)`` distinct.
    if len(words) < 2 or not re.fullmatch(r"[A-Z][A-Z0-9&.+-]{1,5}", raw.split()[0]):
        return False
    return all(re.fullmatch(r"[A-Za-z0-9&.+-]+", word) for word in words)


def _name_key(value: Any) -> str:
    """Normalize harmless aliases while retaining faculty/program qualifiers."""

    text = unicodedata.normalize("NFKC", _text(value)).strip()
    while True:
        match = re.search(r"\s*\(([^()]*)\)\s*$", text)
        if match is None:
            break
        qualifier = match.group(1).strip()
        if not (
            qualifier.casefold().startswith("planned transfer ")
            or _acronym_qualifier(qualifier)
        ):
            break
        text = text[: match.start()].rstrip()
    return _fold(text)


def _identity_key(name: Any, country: Any) -> tuple[str, str]:
    return _country_key(country), _name_key(name)


def _url(value: Any, *, field: str, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise ProfileValidationError(f"profile {field} must be an HTTP(S) URL")
        return None
    if not isinstance(value, str):
        raise ProfileValidationError(f"profile {field} must be a string or null")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProfileValidationError(f"profile {field} must be an absolute HTTP(S) URL: {value!r}")
    return value


def _string_list(value: Any, *, field: str, required: bool = True) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list):
        raise ProfileValidationError(f"profile {field} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ProfileValidationError(f"profile {field} contains a non-empty string requirement")
        item = item.strip()
        if item not in result:
            result.append(item)
    return result


def _validate_links(value: Any, *, context: str) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProfileValidationError(f"{context}.links must be a list")
    links: list[dict[str, str]] = []
    for index, link in enumerate(value):
        if not isinstance(link, Mapping):
            raise ProfileValidationError(f"{context}.links[{index}] must be an object")
        label = link.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ProfileValidationError(f"{context}.links[{index}].label must be non-empty")
        url = _url(link.get("url"), field=f"{context}.links[{index}].url", required=True)
        assert url is not None
        links.append({"label": label.strip(), "url": url})
    return links


def _validate_field(value: Any, *, index: int) -> dict[str, Any]:
    context = f"profile fields[{index}]"
    if not isinstance(value, Mapping):
        raise ProfileValidationError(f"{context} must be an object")
    label = value.get("label")
    text = value.get("text")
    if not isinstance(label, str) or not label.strip():
        raise ProfileValidationError(f"{context}.label must be non-empty")
    if not isinstance(text, str):
        raise ProfileValidationError(f"{context}.text must be a string")
    return {
        "label": label.strip(),
        "text": text,
        "links": _validate_links(value.get("links", []), context=context),
    }


def _validate_profile(value: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProfileValidationError(f"profiles[{index}] must be an object")
    missing = sorted(_PROFILE_FIELDS - set(value))
    if missing:
        raise ProfileValidationError(
            f"profiles[{index}] is missing required fields: {', '.join(missing)}"
        )
    result = dict(value)
    for field in ("institution_id", "relation_id"):
        identifier = value.get(field)
        if isinstance(identifier, int):
            identifier = str(identifier)
        if not isinstance(identifier, str) or not identifier.strip():
            raise ProfileValidationError(f"profiles[{index}].{field} must be non-empty")
        result[field] = identifier.strip()
    for field in ("name", "country", "retrieved_at"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ProfileValidationError(f"profiles[{index}].{field} must be a non-empty string")
        result[field] = value[field].strip()
    if value.get("city") is None:
        result["city"] = ""
    elif not isinstance(value.get("city"), str):
        raise ProfileValidationError(f"profiles[{index}].city must be a string or null")
    else:
        result["city"] = value["city"].strip()
    levels = _string_list(value.get("levels"), field=f"profiles[{index}].levels")
    unknown_levels = sorted(set(levels) - set(_LEVELS))
    if unknown_levels:
        raise ProfileValidationError(
            f"profiles[{index}].levels contains unsupported values: {', '.join(unknown_levels)}"
        )
    result["levels"] = [level for level in _LEVELS if level in levels]
    result["subject_areas"] = _string_list(
        value.get("subject_areas"), field=f"profiles[{index}].subject_areas"
    )
    fields = value.get("fields")
    if not isinstance(fields, list):
        raise ProfileValidationError(f"profiles[{index}].fields must be a list")
    result["fields"] = [_validate_field(item, index=field_index) for field_index, item in enumerate(fields)]
    result["source_url"] = _url(value.get("source_url"), field=f"profiles[{index}].source_url", required=True)
    result["details_url"] = _url(value.get("details_url"), field=f"profiles[{index}].details_url", required=True)
    return result


def _validate_matches(matches: Any, profiles: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    if matches is None:
        return {}
    if not isinstance(matches, Mapping):
        raise ProfileValidationError("catalog matches must be an object mapping names to relation IDs")
    relation_ids = {str(profile["relation_id"]) for profile in profiles}
    result: dict[str, list[str]] = {}
    for raw_name, raw_ids in matches.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ProfileValidationError("catalog matches contains an empty historical name")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ProfileValidationError(f"catalog matches[{raw_name!r}] must be a non-empty list")
        ids: list[str] = []
        for relation_id in raw_ids:
            if isinstance(relation_id, int):
                relation_id = str(relation_id)
            if not isinstance(relation_id, str) or not relation_id.strip():
                raise ProfileValidationError(f"catalog matches[{raw_name!r}] contains an invalid relation ID")
            relation_id = relation_id.strip()
            if relation_id not in relation_ids:
                raise ProfileValidationError(
                    f"catalog match {raw_name!r} refers to unknown relation_id {relation_id!r}"
                )
            if relation_id not in ids:
                ids.append(relation_id)
        key = _name_key(raw_name)
        if not key:
            raise ProfileValidationError(f"catalog matches[{raw_name!r}] has no usable normalized name")
        if key in result and result[key] != ids:
            raise ProfileValidationError(f"catalog contains conflicting match aliases for {raw_name!r}")
        result[key] = ids
    return result


def _validate_catalog(catalog: Any) -> dict[str, Any]:
    if not isinstance(catalog, Mapping):
        raise ProfileValidationError("profile catalog must be a JSON object")
    schema_version = catalog.get("schema_version")
    if schema_version is not None and schema_version != _SUPPORTED_SCHEMA_VERSION:
        raise ProfileValidationError(
            f"unsupported profile catalog schema_version: {schema_version!r}"
        )
    retrieved_at = catalog.get("retrieved_at")
    if not isinstance(retrieved_at, str) or not retrieved_at.strip():
        raise ProfileValidationError("profile catalog retrieved_at must be a non-empty string")
    _url(catalog.get("source_url"), field="catalog source_url", required=True)
    profiles = catalog.get("profiles")
    if not isinstance(profiles, list):
        raise ProfileValidationError("profile catalog profiles must be a list")
    normalized = [_validate_profile(profile, index=index) for index, profile in enumerate(profiles)]
    for field in ("expected_relation_count", "retrieved_relation_count"):
        count = catalog.get(field)
        if count is not None and (not isinstance(count, int) or count < 0):
            raise ProfileValidationError(f"catalog {field} must be a non-negative integer")
        if count is not None and count != len(normalized):
            raise ProfileValidationError(
                f"catalog {field}={count} does not match profiles={len(normalized)}"
            )
    relation_ids = [profile["relation_id"] for profile in normalized]
    if len(set(relation_ids)) != len(relation_ids):
        raise ProfileValidationError("profile catalog contains duplicate relation_id values")
    result = dict(catalog)
    result["profiles"] = normalized
    result["matches"] = _validate_matches(catalog.get("matches"), normalized)
    return result


def load_profiles(path: str | Path) -> dict[str, Any]:
    """Load and validate one saved MoveON profile catalog."""

    path = Path(path)
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileValidationError(f"could not read profile catalog {path}: {exc}") from exc
    return _validate_catalog(payload)


def _profile_has_faculty_qualifier(name: Any) -> bool:
    text = unicodedata.normalize("NFKC", _text(name)).casefold()
    return any(word in re.findall(r"[a-z0-9]+", text) for word in _FACULTY_WORDS)


def _profile_search_text(profile: Mapping[str, Any]) -> list[str]:
    parts = [
        _text(profile.get("name")),
        _text(profile.get("country")),
        _text(profile.get("city")),
        *(_text(level) for level in profile.get("levels", []) or []),
        *(_text(subject) for subject in profile.get("subject_areas", []) or []),
    ]
    for field in profile.get("fields", []) or []:
        if isinstance(field, Mapping):
            parts.extend([_text(field.get("label")), _text(field.get("text"))])
            for link in field.get("links", []) or []:
                if isinstance(link, Mapping):
                    parts.extend([_text(link.get("label")), _text(link.get("url"))])
    return [part for part in parts if part]


def _group_country(group: Mapping[str, Any]) -> str:
    return _text(group.get("country"))


def _profiles_for_group(
    group: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    matches: Mapping[str, Sequence[str]],
) -> list[dict[str, Any]]:
    name = _text(group.get("university"))
    explicit = matches.get(_name_key(name))
    by_relation = {str(profile["relation_id"]): profile for profile in profiles}
    if explicit is not None:
        selected = [by_relation[relation_id] for relation_id in explicit]
        group_country = _country_key(_group_country(group))
        if group_country:
            wrong = [profile for profile in selected if _country_key(profile.get("country")) != group_country]
            if wrong:
                raise ProfileValidationError(
                    f"reviewed match for {name!r} crosses country boundary"
                )
        return [copy.deepcopy(profile) for profile in selected]

    group_key = _identity_key(name, _group_country(group))
    candidates = [profile for profile in profiles if _identity_key(profile.get("name"), profile.get("country")) == group_key]
    if not candidates:
        return []
    # A faculty or school qualifier is semantically part of the institution
    # identity.  Never attach an unreviewed generic relation to that group.
    if _profile_has_faculty_qualifier(name):
        # Keep faculty/school qualifiers, but still allow a harmless display
        # acronym such as ``(QUT)`` or ``(HUST)`` to differ.
        exact = [profile for profile in candidates if _name_key(profile.get("name")) == _name_key(name)]
        return [copy.deepcopy(profile) for profile in exact]
    # Same institution/name can legitimately have several relation profiles.
    # Keep all relation records; the UI can apply degree and subject filters per
    # profile instead of accidentally unioning incompatible relations.
    return [copy.deepcopy(profile) for profile in candidates]


def _base_source_only_group(profile: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "country": profile["country"],
        "university": profile["name"],
        "records": [],
        "categories": [],
        "source_documents": [],
        "source_count": 0,
        "scored_count": 0,
        "total_count": 0,
        "mean_score": None,
        "calibration_ids": [],
        "is_latest_roster": True,
        "source_only": True,
    }


def _enrich_group(group: Mapping[str, Any], matched: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result = copy.deepcopy(dict(group))
    result["profiles"] = [copy.deepcopy(profile) for profile in matched]
    result["profile_levels"] = list(dict.fromkeys(level for profile in matched for level in profile.get("levels", [])))
    result["profile_subjects"] = list(dict.fromkeys(subject for profile in matched for subject in profile.get("subject_areas", [])))
    result["profile_cities"] = list(dict.fromkeys(profile.get("city", "") for profile in matched if profile.get("city")))
    existing = _text(result.get("search_text"))
    profile_parts = [part for profile in matched for part in _profile_search_text(profile)]
    result["search_text"] = " ".join([existing, *profile_parts]).casefold()
    result.setdefault("source_only", False)
    return result


def profile_coverage(groups: Sequence[Mapping[str, Any]], catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Report reviewed/exact coverage without changing any groups."""

    normalized_catalog = _validate_catalog(catalog)
    profiles = normalized_catalog["profiles"]
    matches = normalized_catalog.get("matches", {})
    matched_ids: set[str] = set()
    matched_groups: list[str] = []
    unmatched_groups: list[str] = []
    for group in groups:
        selected = _profiles_for_group(group, profiles, matches)
        name = _text(group.get("university"))
        if selected:
            matched_groups.append(name)
            matched_ids.update(str(profile["relation_id"]) for profile in selected)
        else:
            unmatched_groups.append(name)
    source_only = [
        profile["relation_id"] for profile in profiles if str(profile["relation_id"]) not in matched_ids
    ]
    return {
        "historical_groups": len(groups),
        "matched_groups": len(matched_groups),
        "unmatched_groups": unmatched_groups,
        "matched_relations": len(matched_ids),
        "catalog_relations": len(profiles),
        "source_only_relations": source_only,
    }


def attach_profiles(
    groups: Sequence[Mapping[str, Any]], catalog: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Return report groups enriched with reviewed/current MoveON profiles.

    Matching is exact after Unicode/whitespace normalization and country alias
    normalization.  The optional ``catalog['matches']`` mapping is the only
    way to attach a relation whose historical label contains a department or
    other distinction absent from the current MoveON display name.
    """

    normalized_catalog = _validate_catalog(catalog)
    profiles = normalized_catalog["profiles"]
    matches = normalized_catalog.get("matches", {})
    result = [_enrich_group(group, _profiles_for_group(group, profiles, matches)) for group in groups]

    matched_relation_ids = {
        str(profile["relation_id"])
        for group in result
        for profile in group.get("profiles", [])
    }
    # Current MoveON relations without a matching historical bar group remain
    # visible as source-only entries.  Group only exact institution/country
    # names; duplicate relations stay in the profiles list.
    source_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for profile in profiles:
        relation_id = str(profile["relation_id"])
        if relation_id in matched_relation_ids:
            continue
        if _profile_has_faculty_qualifier(profile.get("name")):
            key = (_country_key(profile.get("country")), _fold(profile.get("name")))
        else:
            key = _identity_key(profile.get("name"), profile.get("country"))
        source_groups.setdefault(key, _base_source_only_group(profile))
        if profile not in source_groups[key].setdefault("_source_profiles", []):
            source_groups[key]["_source_profiles"].append(copy.deepcopy(profile))
    for base in source_groups.values():
        matched = base.pop("_source_profiles", [])
        result.append(_enrich_group(base, matched))
    return result


__all__ = [
    "ProfileValidationError",
    "attach_profiles",
    "load_profiles",
    "profile_coverage",
]
