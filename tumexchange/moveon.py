"""Fetch and normalize the public TUMexchange MoveON report.

The report page renders only a small first batch in the browser, but its
public WordPress AJAX endpoint returns the complete relation payload in one
response.  This module keeps the network-specific part in one place and
exports a small JSON contract for the website.

The source contains two kinds of identifiers:

* ``core_id`` identifies the institution record used by the report page.
* ``relation_id`` identifies the exchange relation.  More than one relation
  can therefore share a ``core_id``; both identifiers are retained.

Some PDF links are intentionally rendered as ``href="#"`` and are resolved
by JavaScript through another AJAX request.  Downloading those PDFs is not
needed for the catalogue, so those links explicitly point back to TUM's
directory and retain the document ID instead of preserving an unusable ``#``.
"""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


SOURCE_URL = "https://tum.adv-pub.moveon4.de/report-page-6940/"
AJAX_PATH = "/ap-dashboard/admin-ajax.php"
SCHEMA_VERSION = "moveon-catalog-v1"


class MoveOnError(ValueError):
    """Raised when the public MoveON response cannot form a complete catalog."""


def _utc_timestamp() -> str:
    """Return a compact UTC timestamp suitable for catalog metadata."""

    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ajax_url(source_url: str) -> str:
    parsed = urlsplit(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"source_url must be an absolute HTTP(S) URL: {source_url!r}")
    return urlunsplit((parsed.scheme, parsed.netloc, AJAX_PATH, "", ""))


def _details_url(source_url: str, record: Mapping[str, Any]) -> str:
    """Build the official experience-report URL observed in the directory."""

    parsed = urlsplit(source_url)
    path = parsed.path.rstrip("/") + "/"
    query = urlencode(
        {
            "core_id": str(record.get("core_id", "")),
            "relation_id": str(record.get("relation_id", "")),
            "openwhichtab": "travelreports",
            "hide_elements": "true",
            "whichtab": "all",
        }
    )
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


class _FieldHTMLParser(HTMLParser):
    """Extract readable text and anchors from one MoveON HTML field."""

    _BLOCK_START = {"blockquote", "div", "li", "ol", "p", "ul"}
    _BLOCK_END = _BLOCK_START

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self._link: dict[str, Any] | None = None

    def _newline(self) -> None:
        if not self.parts or self.parts[-1] != "\n":
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "br" or tag in self._BLOCK_START:
            self._newline()
        if tag == "a":
            attributes = {key.lower(): value or "" for key, value in attrs}
            self._link = {
                "href": attributes.get("href", ""),
                "title": attributes.get("title", ""),
                "aria_label": attributes.get("aria-label", ""),
                "document_id": attributes.get("data-id", ""),
                "parts": [],
            }

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._link is not None:
            link = self._link
            label = _clean_text("".join(link["parts"]))
            if not label:
                label = _clean_text(link.get("title") or link.get("aria_label") or link.get("href"))
            self.links.append(
                {
                    "label": label,
                    "_href": html.unescape(str(link.get("href", ""))).strip(),
                    "document_id": str(link.get("document_id", "")),
                }
            )
            self._link = None
        if tag in self._BLOCK_END:
            self._newline()

    def handle_data(self, data: str) -> None:
        self.parts.append(data)
        if self._link is not None:
            self._link["parts"].append(data)


def _clean_text(value: Any) -> str:
    """Normalize HTML-derived whitespace while keeping paragraph boundaries."""

    if value is None:
        return ""
    value = html.unescape(str(value)).replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for line in value.split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", line).strip()
        if line:
            lines.append(line)
        elif lines and lines[-1] != "":
            lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _resolve_link(href: str, *, source_url: str, details_url: str) -> str | None:
    """Return a usable HTTP(S) URL, replacing MoveON's JavaScript PDF links."""

    href = html.unescape(href).strip()
    if not href or href == "#":
        return source_url
    absolute = urljoin(source_url, href)
    parsed = urlsplit(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return absolute


def _parse_field(
    label: Any,
    raw_value: Any,
    *,
    source_url: str,
    details_url: str,
) -> dict[str, Any]:
    """Normalize one ``shortname``/``fullname`` pair from the source."""

    raw_value = "" if raw_value is None else str(raw_value)
    parser = _FieldHTMLParser()
    try:
        parser.feed(raw_value)
        parser.close()
    except Exception as exc:  # pragma: no cover - malformed source HTML is rare.
        raise MoveOnError(f"could not parse MoveON field {label!r}: {exc}") from exc

    links: list[dict[str, str]] = []
    for link in parser.links:
        href = link.pop("_href")
        resolved = _resolve_link(href, source_url=source_url, details_url=details_url)
        if resolved is not None:
            entry = {"label": link["label"], "url": resolved}
            if not href or href == "#":
                entry["label"] += " (on TUM's directory)"
                entry["document_id"] = link.get("document_id", "")
            links.append(entry)

    return {
        "label": _clean_text(label),
        "text": _clean_text("".join(parser.parts)),
        "links": links,
    }


def _field_entries(record: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    """Yield all field objects from all source tabs, retaining source order."""

    informatics = record.get("informatics") or []
    if not isinstance(informatics, list):
        return
    for tab in informatics:
        if not isinstance(tab, Mapping):
            continue
        for values in tab.values():
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, Mapping) and "shortname" in value:
                    yield str(value.get("shortname", "")), value


def _split_pipe_values(text: str) -> list[str]:
    return [item.strip() for item in text.split("||") if item.strip()]


def _levels(fields: Iterable[Mapping[str, Any]]) -> list[str]:
    levels: set[str] = set()
    for field in fields:
        if str(field.get("label", "")).casefold() != "level":
            continue
        text = str(field.get("text", ""))
        for value in _split_pipe_values(text):
            lowered = value.casefold()
            if "bachelor" in lowered or "undergraduate" in lowered:
                levels.add("bachelor")
            if "master" in lowered or "postgraduate" in lowered:
                levels.add("master")
    return [level for level in ("bachelor", "master") if level in levels]


def _subject_areas(fields: Iterable[Mapping[str, Any]]) -> list[str]:
    for field in fields:
        if str(field.get("label", "")).casefold() == "subject area":
            return _split_pipe_values(str(field.get("text", "")))
    return []


def _city(fields: Iterable[Mapping[str, Any]]) -> str:
    for field in fields:
        if str(field.get("label", "")).casefold() == "city":
            return str(field.get("text", "")).strip()
    return ""


def _profile(record: Mapping[str, Any], *, source_url: str, retrieved_at: str) -> dict[str, Any]:
    details_url = _details_url(source_url, record)
    fields = [
        _parse_field(label, value.get("fullname"), source_url=source_url, details_url=details_url)
        for label, value in _field_entries(record)
    ]
    return {
        "institution_id": str(record.get("core_id", "")),
        "relation_id": str(record.get("relation_id", "")),
        "name": _clean_text(record.get("universityname", "")),
        "country": _clean_text(record.get("country_fullname") or record.get("country", "")),
        "city": _city(fields),
        "levels": _levels(fields),
        "subject_areas": _subject_areas(fields),
        "fields": fields,
        "source_url": source_url,
        "details_url": details_url,
        "retrieved_at": retrieved_at,
    }


def _decode_response(raw: bytes | str) -> Mapping[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        excerpt = str(raw)[:300].replace("\n", " ")
        raise MoveOnError(f"MoveON AJAX response was not JSON: {excerpt!r}") from exc
    if not isinstance(value, Mapping):
        raise MoveOnError("MoveON AJAX response must be a JSON object")
    return value


def _fetch_payload(source_url: str, *, timeout: float) -> Mapping[str, Any]:
    data = urlencode(
        {
            "action": "load_report_data_on_ajax_load",
            "searched_data[publisher_id]": "46",
            "ajaxload": "0",
        }
    ).encode("utf-8")
    request = Request(
        _ajax_url(source_url),
        data=data,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": source_url,
            "User-Agent": "tumexchange-bars MoveON catalog importer",
            "X-Requested-With": "XMLHttpRequest",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return _decode_response(response.read())
    except HTTPError as exc:
        raise MoveOnError(f"MoveON AJAX request failed with HTTP {exc.code}") from exc
    except URLError as exc:
        raise MoveOnError(f"MoveON AJAX request failed: {exc.reason}") from exc


def _validate_payload(payload: Mapping[str, Any], expected_count: int | None = None) -> list[Mapping[str, Any]]:
    records = payload.get("labelvalue")
    counters = payload.get("counters")
    if not isinstance(records, list):
        raise MoveOnError("MoveON response has no labelvalue list")
    if not isinstance(counters, Mapping):
        raise MoveOnError("MoveON response has no counters object")
    reported_count = counters.get("institutions")
    if not isinstance(reported_count, int) or reported_count <= 0:
        raise MoveOnError("MoveON response has no positive source relation count")
    if reported_count != len(records):
        raise MoveOnError(
            f"MoveON response count mismatch: counters.institutions={reported_count!r}, "
            f"labelvalue={len(records)}"
        )
    if expected_count and len(records) != expected_count:
        raise MoveOnError(f"MoveON coverage incomplete: expected {expected_count}, got {len(records)}")
    ids = [(str(row.get("core_id", "")), str(row.get("relation_id", ""))) for row in records if isinstance(row, Mapping)]
    if len(ids) != len(records) or any(not core or not relation for core, relation in ids):
        raise MoveOnError("MoveON response contains a record without core_id or relation_id")
    if len({relation for _, relation in ids}) != len(ids):
        raise MoveOnError("MoveON response contains duplicate relation_id values")
    if any(not row.get("universityname") or not list(_field_entries(row)) for row in records):
        raise MoveOnError("MoveON response contains an unnamed or incomplete relation")
    return [row for row in records if isinstance(row, Mapping)]


def fetch_catalog(
    url: str = SOURCE_URL,
    *,
    timeout: float = 30.0,
    expected_relation_count: int | None = None,
) -> dict[str, Any]:
    """Fetch the complete public report and return the normalized catalog.

    Coverage is checked against the source's current count. An optional
    expected count allows a caller to pin a particular snapshot.
    """

    retrieved_at = _utc_timestamp()
    payload = _fetch_payload(url, timeout=timeout)
    records = _validate_payload(payload, expected_relation_count)
    profiles = [_profile(record, source_url=url, retrieved_at=retrieved_at) for record in records]
    counters = payload.get("counters")
    return {
        "schema_version": SCHEMA_VERSION,
        "retrieved_at": retrieved_at,
        "source_url": url,
        "expected_relation_count": expected_relation_count or len(profiles),
        "retrieved_relation_count": len(profiles),
        "counts": {
            "countries": counters.get("countries"),
            "institutions": counters.get("institutions"),
            "programs": counters.get("programs"),
        },
        "profiles": profiles,
    }


def _validate_catalog(catalog: Mapping[str, Any]) -> None:
    profiles = catalog.get("profiles")
    expected = catalog.get("expected_relation_count")
    if not isinstance(profiles, list):
        raise MoveOnError("catalog profiles must be a list")
    if not isinstance(expected, int) or expected <= 0:
        raise MoveOnError("catalog expected_relation_count must be a positive integer")
    if len(profiles) != expected:
        raise MoveOnError(f"catalog coverage incomplete: expected {expected}, got {len(profiles)}")
    relation_ids = [str(profile.get("relation_id", "")) for profile in profiles if isinstance(profile, Mapping)]
    if len(relation_ids) != len(profiles) or any(not relation_id for relation_id in relation_ids):
        raise MoveOnError("catalog contains a profile without relation_id")
    if len(set(relation_ids)) != len(relation_ids):
        raise MoveOnError("catalog contains duplicate relation_id values")


def write_catalog(catalog: Mapping[str, Any], path: str | os.PathLike[str] | Path) -> Path:
    """Atomically write a validated catalog JSON file and return its path."""

    _validate_catalog(catalog)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return destination


__all__ = [
    "AJAX_PATH",
    "MoveOnError",
    "SCHEMA_VERSION",
    "SOURCE_URL",
    "fetch_catalog",
    "write_catalog",
]
