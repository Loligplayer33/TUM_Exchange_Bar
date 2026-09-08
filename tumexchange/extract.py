"""Vector extraction and calibration for TUMexchange chance tables.

The source workbook is exported as a vector PDF.  Numeric bars are therefore
read from the PDF rectangle objects, while labels and availability statuses
come from the PDF text layer.  Rasterisation is intentionally not used: a
scanned PDF fails loudly so that a visually plausible but uncalibrated result
cannot enter the data set.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import statistics
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

EXTRACTION_VERSION = "0.2.0-adaptive-vector-layout"

STATUS_TEXT = {
    "no cooperation": "no_cooperation",
    "no places": "no_places",
    "no applications": "no_applications",
}
VALID_STATUSES = {
    "measured",
    "no_cooperation",
    "no_places",
    "no_applications",
    "bar_absent",
    "ambiguous",
}
YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
RELEASE_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


class ExtractionError(RuntimeError):
    """Base class for an extraction that cannot be trusted."""


class UnsupportedLayoutError(ExtractionError):
    """Raised when the input is not a supported vector table PDF."""


class CalibrationError(ExtractionError):
    """Raised when explicit full-bar references cannot be validated."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _number(value: Any, *, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CalibrationError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise CalibrationError(f"{name} must be a positive finite number")
    return number


def _bbox(rect: Mapping[str, Any]) -> list[float]:
    """Return a stable top-left PDF bbox used by pdfplumber."""

    return [
        float(rect["x0"]),
        float(rect["top"]),
        float(rect["x1"]),
        float(rect["bottom"]),
    ]


def _rounded(value: float | None, places: int = 9) -> float | None:
    if value is None:
        return None
    return round(float(value), places)


def _source_release_year(path: Path, observed_years: Iterable[int], override: int | None) -> int:
    """Return the document release year used to resolve overlapping sources."""

    if override is not None:
        year = int(override)
        if year < 1900 or year > 2200:
            raise ExtractionError("source release year must be between 1900 and 2200")
        return year
    filename_years = [int(value) for value in RELEASE_YEAR_RE.findall(path.stem)]
    if filename_years:
        return max(filename_years)
    years = list(observed_years)
    if not years:
        raise ExtractionError(
            "Could not infer a source release year; pass --source-release-year explicitly"
        )
    # TUMexchange releases describe the three preceding selection rounds.
    return max(years) + 1


def _word_lines(words: Sequence[Mapping[str, Any]], tolerance: float = 2.0) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for word in sorted(words, key=lambda item: (float(item["top"]), float(item["x0"]))):
        if not str(word.get("text", "")).strip():
            continue
        if not lines or float(word["top"]) - lines[-1]["top"] > tolerance:
            lines.append({"top": float(word["top"]), "bottom": float(word["bottom"]), "words": [word]})
        else:
            lines[-1]["words"].append(word)
            lines[-1]["bottom"] = max(lines[-1]["bottom"], float(word["bottom"]))
    return lines


def _text(words: Iterable[Mapping[str, Any]]) -> str:
    return " ".join(str(word["text"]) for word in sorted(words, key=lambda item: float(item["x0"]))).strip()


def _looks_like_country(
    line: Mapping[str, Any],
    *,
    size_threshold: float,
    country_fonts: set[str],
) -> bool:
    left = list(line["left_words"])
    right = list(line["right_words"])
    if not left or right:
        return False
    if max(float(word["x1"]) for word in left) >= 250:
        return False
    sizes = [float(word.get("size", 0)) for word in left]
    # Both known exports use a larger type size for country headings, but the
    # absolute sizes differ (7.32/6.72pt and 8.04/7.32pt).  The threshold is
    # derived per document rather than binding extraction to either template.
    # One 2026 heading ("Israel") is anomalously set at the university size,
    # so retain the independently styled heading font as a secondary signal.
    fonts = {str(word.get("fontname", "")) for word in left}
    return statistics.median(sizes) >= size_threshold or bool(fonts & country_fonts)


def _find_year_columns(pages: Sequence[Any]) -> dict[int, dict[str, float]]:
    year_words: list[Mapping[str, Any]] = []
    for page in pages:
        for word in page.extract_words(extra_attrs=["size"]):
            text = str(word.get("text", ""))
            if YEAR_RE.fullmatch(text) and float(word["x0"]) >= 280:
                year_words.append(word)

    # Headers are only printed on the first page in this document.  Pick the
    # largest x-separated same-line group so prose mentioning a year does not
    # become a column definition.
    groups: list[list[Mapping[str, Any]]] = []
    for word in sorted(year_words, key=lambda item: (float(item["top"]), float(item["x0"]))):
        if not groups or float(word["top"]) - float(groups[-1][0]["top"]) > 3:
            groups.append([word])
        else:
            groups[-1].append(word)
    groups = [group for group in groups if len(group) >= 2]
    if groups:
        chosen = max(groups, key=lambda group: len(group))
        if len(chosen) >= 2:
            chosen = sorted(chosen, key=lambda item: float(item["x0"]))
            header_columns = [
                {
                    "x0": float(word["x0"]),
                    "x1": float(word["x1"]),
                    "center": (float(word["x0"]) + float(word["x1"])) / 2,
                }
                for word in chosen
            ]
            # Numeric bars repeat at exactly one x0 and height per annual
            # column.  Selecting the most frequent compact rectangle origins
            # adapts to both known templates: 8.4pt bars in the 2027 release
            # and 5.52pt bars in the 2026 release.  Header offsets differ by
            # more than 24pt, so deriving x0 from header text is unsafe.
            candidates: dict[float, list[Mapping[str, Any]]] = {}
            for page in pages:
                for rect in page.rects:
                    height = float(rect["height"])
                    if (
                        3.0 <= height <= 10.0
                        and float(rect["width"]) > 0.05
                        and float(rect["x0"]) >= 280
                    ):
                        candidates.setdefault(round(float(rect["x0"]), 3), []).append(rect)
            ranked = sorted(
                candidates,
                key=lambda x0: (-len(candidates[x0]), x0),
            )
            if len(ranked) < len(header_columns):
                raise UnsupportedLayoutError(
                    "Could not infer repeated numeric bar tracks for every year column"
                )
            track_x0s = sorted(ranked[: len(header_columns)])
            return {
                int(word["text"]): {
                    **column,
                    "x0": float(track_x0s[index]),
                    "track_x0": float(track_x0s[index]),
                    "header_x0": float(column["x0"]),
                    "bar_height": statistics.median(
                        float(rect["height"]) for rect in candidates[track_x0s[index]]
                    ),
                }
                for index, (word, column) in enumerate(zip(chosen, header_columns))
            }

    # A same-layout PDF can omit the year header only when an override carries
    # the column map.  Without that map infer x positions from vector bars but
    # refuse to invent year labels.
    raise UnsupportedLayoutError(
        "Could not infer year columns from vector text; provide a calibration "
        "override containing year_columns or use the original table layout"
    )


def _column_for_x(x0: float, columns: Mapping[int, Mapping[str, float]]) -> int | None:
    return min(columns, key=lambda year: abs(float(columns[year]["x0"]) - x0)) if columns else None


def _table_start(
    page: Any,
    page_index: int,
    lines: Sequence[Mapping[str, Any]],
    year_columns: Mapping[int, Any],
    *,
    label_right: float,
    country_size_threshold: float,
    country_fonts: set[str],
) -> float:
    if page_index == 0:
        header_bottoms = [
            float(word["bottom"])
            for word in page.extract_words()
            if YEAR_RE.fullmatch(str(word.get("text", ""))) and float(word["x0"]) >= 280
        ]
        if header_bottoms:
            return max(header_bottoms) + 4
    for line in lines:
        left = [word for word in line["words"] if float(word["x0"]) < label_right]
        right = [word for word in line["words"] if float(word["x0"]) >= label_right]
        probe = {"left_words": left, "right_words": right}
        if _looks_like_country(
            probe,
            size_threshold=country_size_threshold,
            country_fonts=country_fonts,
        ):
            return float(line["top"]) - 1
    # This is only reached for a malformed page and will normally be followed
    # by an explicit no-rows error.
    return 0.0


def _normalise_status(words: Sequence[Mapping[str, Any]]) -> tuple[str | None, str | None]:
    if not words:
        return None, None
    ordered = sorted(words, key=lambda item: float(item["x0"]))
    tokens = [_norm(str(word["text"])) for word in ordered]
    matches: list[tuple[str, str]] = []
    for start in range(len(tokens)):
        if start + 1 >= len(tokens):
            continue
        phrase = f"{tokens[start]} {tokens[start + 1]}"
        status = STATUS_TEXT.get(phrase)
        if status:
            matches.append((status, phrase))
    if not matches:
        return None, None
    unique = {status for status, _ in matches}
    if len(unique) > 1:
        return "ambiguous", " / ".join(phrase for _, phrase in matches)
    return matches[0][0], matches[0][1]


def _rects_for_columns(page: Any, columns: Mapping[int, Mapping[str, float]]) -> tuple[dict[int, list[dict[str, Any]]], dict[int, list[dict[str, Any]]]]:
    bars: dict[int, list[dict[str, Any]]] = {year: [] for year in columns}
    status_boxes: dict[int, list[dict[str, Any]]] = {year: [] for year in columns}
    for rect in page.rects:
        x0 = float(rect["x0"])
        year = _column_for_x(x0, columns)
        if year is None or abs(x0 - float(columns[year]["x0"])) > 7.0:
            continue
        height = float(rect["height"])
        width = float(rect["width"])
        bar_height = float(columns[year].get("bar_height", 8.4))
        if abs(height - bar_height) <= 0.15 and abs(x0 - float(columns[year]["x0"])) <= 0.15 and width > 0.05:
            bars[year].append(rect)
        elif bar_height + 1.0 <= height <= 14.5 and width > 0.05:
            # These are the blue status backgrounds.  Their width includes
            # padding and is never used as a numeric bar width.
            status_boxes[year].append(rect)
    return bars, status_boxes


def _matching_rects(
    rects: Sequence[Mapping[str, Any]],
    line: Mapping[str, Any],
    *,
    expected_offset: float,
    tolerance: float,
) -> list[Mapping[str, Any]]:
    """Return rectangles belonging to the nearest row.

    A broad vertical overlap window is tempting here, but it includes the
    following row whenever a text line is taller than a normal cell.  Match
    against the expected top edge instead and retain ties so genuinely
    ambiguous duplicate rectangles remain visible to the caller.
    """

    expected = float(line["top"]) + expected_offset
    candidates = [
        rect
        for rect in rects
        if abs(float(rect["top"]) - expected) <= tolerance
    ]
    if not candidates:
        return []
    nearest = min(abs(float(rect["top"]) - expected) for rect in candidates)
    # PDF exporters can emit two identical rectangle objects.  Keep both when
    # they are truly tied; a neighboring row is separated by about 12pt.
    return [
        rect
        for rect in candidates
        if abs(abs(float(rect["top"]) - expected) - nearest) <= 0.01
    ]


def _prepare_rows(pages: Sequence[Any], columns: Mapping[int, Mapping[str, float]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    country: str | None = None
    row_index = 0
    label_right = min(float(column["x0"]) for column in columns.values()) - 5.0
    size_counts: dict[float, int] = {}
    country_fonts: set[str] = set()
    for page in pages:
        for word in page.extract_words(extra_attrs=["size", "fontname"]):
            size = round(float(word.get("size", 0)), 3)
            if float(word["x0"]) < label_right and 5.0 <= size <= 10.0:
                size_counts[size] = size_counts.get(size, 0) + 1
    # Ignore one-off title/header sizes. Country headings and university
    # labels each occur throughout the document.
    table_sizes = sorted(
        (size for size, count in size_counts.items() if count >= 10),
        reverse=True,
    )
    if len(table_sizes) < 2:
        raise UnsupportedLayoutError("Could not distinguish country and university label sizes")
    country_size_threshold = (table_sizes[0] + table_sizes[1]) / 2
    university_font_counts: dict[str, int] = {}
    observed_fonts: set[str] = set()
    for page in pages:
        for word in page.extract_words(extra_attrs=["size", "fontname"]):
            if float(word["x0"]) >= label_right:
                continue
            font = str(word.get("fontname", ""))
            observed_fonts.add(font)
            if float(word.get("size", 0)) < country_size_threshold:
                university_font_counts[font] = university_font_counts.get(font, 0) + 1
    if not university_font_counts:
        raise UnsupportedLayoutError("Could not infer university label typography")
    university_font = max(university_font_counts, key=university_font_counts.get)
    country_fonts = observed_fonts - {university_font}
    for page_index, page in enumerate(pages):
        words = page.extract_words(extra_attrs=["size", "fontname"])
        lines = _word_lines(words)
        start = _table_start(
            page,
            page_index,
            lines,
            columns,
            label_right=label_right,
            country_size_threshold=country_size_threshold,
            country_fonts=country_fonts,
        )
        bars, status_boxes = _rects_for_columns(page, columns)
        page_candidates: list[dict[str, Any]] = []
        for line in lines:
            if float(line["top"]) < start:
                continue
            left = [word for word in line["words"] if float(word["x0"]) < label_right]
            right = [word for word in line["words"] if float(word["x0"]) >= label_right]
            if not left:
                continue
            probe = {"left_words": left, "right_words": right}
            if _looks_like_country(
                probe,
                size_threshold=country_size_threshold,
                country_fonts=country_fonts,
            ):
                country = _text(left)
                continue
            candidate = {
                "page": page_index + 1,
                "top": float(line["top"]),
                "bottom": float(line["bottom"]),
                "left_words": left,
                "right_words": right,
                "country": country or "",
            }
            page_candidates.append(candidate)

        for line in page_candidates:
            label = _text(line["left_words"])
            per_year: dict[int, dict[str, Any]] = {}
            for year, column in columns.items():
                zone_left = float(column["x0"]) - 8.0
                next_x0 = min(
                    (float(other["x0"]) for other_year, other in columns.items() if other_year != year and float(other["x0"]) > float(column["x0"])),
                    default=float(column["x0"]) + 80.0,
                )
                zone_right = next_x0 - 8.0
                zone_words = [
                    word
                    for word in line["right_words"]
                    if zone_left <= float(word["x0"]) < zone_right
                ]
                status, status_text = _normalise_status(zone_words)
                candidates = _matching_rects(
                    bars[year], line, expected_offset=-1.8, tolerance=5.0
                )
                boxes = _matching_rects(
                    status_boxes[year], line, expected_offset=-3.2, tolerance=7.0
                )
                # A status phrase takes precedence over its background and
                # over any accidental rectangle from the same cell.
                if status:
                    if status == "ambiguous":
                        cell_status = "ambiguous"
                    else:
                        cell_status = status
                    status_box = min(boxes, key=lambda rect: abs(float(rect["top"]) - float(line["top"]))) if boxes else None
                    per_year[year] = {
                        "status": cell_status,
                        "status_text": status_text,
                        "bar": None,
                        "status_box": status_box,
                        "notes": f"PDF text status: {status_text}" if status_text else "Unrecognised status text",
                    }
                elif len(candidates) == 1:
                    per_year[year] = {
                        "status": "measured",
                        "status_text": None,
                        "bar": candidates[0],
                        "status_box": None,
                        "notes": "Vector rectangle measured in PDF points",
                    }
                elif len(candidates) > 1:
                    per_year[year] = {
                        "status": "ambiguous",
                        "status_text": None,
                        "bar": None,
                        "status_box": None,
                        "notes": f"Found {len(candidates)} vector rectangles in one year cell",
                    }
                else:
                    per_year[year] = {
                        "status": "bar_absent",
                        "status_text": None,
                        "bar": None,
                        "status_box": None,
                        "notes": "No vector bar or recognised text status in this cell",
                    }
            # Explanatory prose can appear beneath the table on a page.  A
            # real university row always has at least one numeric bar or one
            # recognised status across its annual cells.
            if not any(
                cell["status"] not in {"bar_absent", "ambiguous"}
                for cell in per_year.values()
            ):
                continue
            row_index += 1
            rows.append(
                {
                    "country": line["country"],
                    "university": label,
                    "raw_label": label,
                    "raw_university_text": label,
                    "page": page_index + 1,
                    "row_index": row_index,
                    "top": line["top"],
                    "bottom": line["bottom"],
                    "per_year": per_year,
                }
            )
    return rows


def _load_override(value: Mapping[str, Any] | str | Path | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, (str, Path)):
        with Path(value).open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    elif isinstance(value, Mapping):
        loaded = dict(value)
    else:
        raise CalibrationError("calibration override must be a JSON path or object")
    if not isinstance(loaded, dict):
        raise CalibrationError("calibration override must contain a JSON object")
    return loaded


def _columns_from_override(override: Mapping[str, Any] | None) -> dict[int, dict[str, float]] | None:
    """Read a stored year-column map before parsing rows.

    This lets a same-layout PDF with a missing/edited header reuse an explicit
    calibration file.  The map stores the numeric track x0, while header_x0
    and center remain optional provenance fields.
    """

    if not override or not isinstance(override.get("year_columns"), Mapping):
        return None
    result: dict[int, dict[str, float]] = {}
    for key, value in override["year_columns"].items():
        if not isinstance(value, Mapping):
            raise CalibrationError(f"year_columns[{key}] must be an object")
        try:
            year = int(key)
            x0 = float(value.get("track_x0", value.get("x0")))
        except (TypeError, ValueError) as exc:
            raise CalibrationError(f"invalid year column {key!r} in calibration override") from exc
        if not math.isfinite(x0):
            raise CalibrationError(f"invalid x0 for year column {key!r}")
        result[year] = {
            "x0": x0,
            "track_x0": x0,
            "x1": float(value.get("x1", x0 + 48.12)),
            "center": float(value.get("center", x0 + 24.06)),
            "header_x0": float(value.get("header_x0", x0)),
            "bar_height": float(value.get("bar_height", 8.4)),
        }
    if len(result) < 2:
        raise CalibrationError("year_columns override must contain at least two columns")
    return dict(sorted(result.items()))


def _validate_override_columns(
    live_columns: Mapping[int, Mapping[str, float]],
    override_columns: Mapping[int, Mapping[str, float]],
    *,
    tolerance_pt: float = 1.0,
) -> None:
    """Reject a calibration geometry copied from a different table layout.

    ``year_columns`` stores the numeric bar-track x0.  Header words are about
    15.36pt to the right of that track in this export, so comparing the track
    x0 values is the useful scale/layout check.  The year labels themselves
    must also match; a stale 2024/2025/2026 map must not silently relabel a
    new PDF.
    """

    live_years = set(int(year) for year in live_columns)
    override_years = set(int(year) for year in override_columns)
    if live_years != override_years:
        raise CalibrationError(
            "calibration override year columns "
            f"{sorted(override_years)} do not match live PDF columns {sorted(live_years)}"
        )
    for year in sorted(live_years):
        live_x0 = float(live_columns[year].get("track_x0", live_columns[year]["x0"]))
        override_x0 = float(override_columns[year].get("track_x0", override_columns[year]["x0"]))
        if abs(live_x0 - override_x0) > tolerance_pt:
            raise CalibrationError(
                f"calibration override geometry for {year} is stale: "
                f"track x0 {override_x0:.3f}pt vs live {live_x0:.3f}pt "
                f"(tolerance {tolerance_pt:g}pt)"
            )


def _calibration_fingerprint(
    widths: Mapping[int, float],
    *,
    method: str,
    formula: str,
    tolerance_pt: float,
) -> str:
    payload = {
        "widths": {str(year): _rounded(float(width)) for year, width in sorted(widths.items())},
        "method": method,
        "formula": formula,
        "tolerance_pt": _rounded(tolerance_pt),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def _widths_from_override(override: Mapping[str, Any], years: Iterable[int]) -> dict[int, float]:
    raw = override.get("full_width_pt_by_year") or override.get("full_width_by_year")
    widths: dict[int, float] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            widths[int(key)] = _number(value, name=f"full width for {key}")
    if not widths and override.get("full_width_pt") is not None:
        width = _number(override["full_width_pt"], name="full_width_pt")
        widths = {year: width for year in years}
    missing = [year for year in years if year not in widths]
    if missing:
        raise CalibrationError(
            "calibration override lacks full_width_pt_by_year values for "
            + ", ".join(str(year) for year in missing)
        )
    return widths


def _build_calibration(
    rows: Sequence[Mapping[str, Any]],
    columns: Mapping[int, Mapping[str, float]],
    *,
    source_sha256: str,
    source_filename: str,
    source_document_id: str,
    source_release_year: int,
    reference_country: str,
    reference_year: int,
    reference_university: str | None,
    tolerance_pt: float,
    override_value: Mapping[str, Any] | str | Path | None,
    allow_cross_source_calibration: bool,
) -> dict[str, Any]:
    override = _load_override(override_value)
    years = sorted(columns)
    if override is not None:
        override_source_sha256 = override.get("source_sha256")
        if override_source_sha256 and str(override_source_sha256) != source_sha256:
            if not allow_cross_source_calibration:
                raise CalibrationError(
                    "calibration override belongs to source hash "
                    f"{override_source_sha256}, but the current PDF has {source_sha256}; "
                    "pass --allow-cross-source-calibration only after checking the same layout"
                )
        widths = _widths_from_override(override, years)
        calibration = copy.deepcopy(dict(override))
        calibration["full_width_pt_by_year"] = {str(year): _rounded(widths[year]) for year in years}
        calibration.setdefault("method", "explicit calibration override")
        calibration.setdefault("formula", "bar_fraction = raw_width_pt / full_width_pt; score_percent = 100 * bar_fraction")
        calibration.setdefault("units", "PDF points (pt)")
        # The current invocation controls the tolerance used for snapping and
        # validation.  Carrying a stale tolerance out of an older calibration
        # would make an apparently identical override produce different scores.
        calibration["tolerance_pt"] = tolerance_pt
        calibration["calibration_source_sha256"] = override_source_sha256
        calibration["source_sha256"] = source_sha256
        calibration["source_filename"] = source_filename
        calibration["source_document_id"] = source_document_id
        calibration["source_release_year"] = source_release_year
        calibration["extraction_version"] = EXTRACTION_VERSION
        calibration["year_columns"] = {str(year): dict(columns[year]) for year in years}
        calibration["geometry"] = {
            **dict(calibration.get("geometry") or {}),
            "coordinate_system": "pdfplumber top-left page coordinates",
            "column_x0_pt": {
                str(year): _rounded(float(columns[year].get("track_x0", columns[year]["x0"])))
                for year in years
            },
            "reference_bar_height_pt": float(
                (calibration.get("geometry") or {}).get(
                    "reference_bar_height_pt",
                    columns[years[0]].get("bar_height", 8.4),
                )
            ),
            "numeric_bar_rectangles_only": True,
            "status_box_geometry_excluded_from_numeric_calibration": True,
        }
        calibration["status_assumptions"] = {
            **dict(calibration.get("status_assumptions") or {}),
            "no_applications": "treated as 100% under the project assumption; its padded text box is not a numeric bar",
            "no_cooperation": "excluded as not available",
            "no_places": "excluded as not available",
            "bar_absent": "excluded as unavailable or unrecognised",
        }
        calibration["calibration_id"] = (
            f"calibration:{source_document_id}:override:"
            f"{_calibration_fingerprint(widths, method=str(calibration['method']), formula=str(calibration['formula']), tolerance_pt=tolerance_pt)}"
        )
        calibration["methodology"] = {
            **dict(calibration.get("methodology") or {}),
            "reference_selection": "explicit calibration override",
            "normalisation": "full widths supplied by override and checked against the current source geometry",
            "score_interpretation": "normalized bar length under the user's linear chance assumption, not an independently estimated admission probability",
        }
        return calibration

    target_country = _norm(reference_country)
    references = [
        row
        for row in rows
        if _norm(str(row["country"])) == target_country
        and row["per_year"].get(reference_year, {}).get("status") == "measured"
        and (reference_university is None or _norm(str(row["university"])) == _norm(reference_university))
    ]
    minimum_references = 1 if reference_university is not None else 3
    if len(references) < minimum_references:
        available = [str(row["university"]) for row in rows if _norm(str(row["country"])) == target_country]
        raise CalibrationError(
            f"Need at least {minimum_references} measured {reference_country} reference "
            f"{'bar' if minimum_references == 1 else 'bars'} for {reference_year}; "
            f"found {len(references)}. Available universities: {', '.join(available) or 'none'}. "
            "Use --calibration with an explicit full_width_pt_by_year map for another layout."
        )
    ref_widths = [float(row["per_year"][reference_year]["bar"]["width"]) for row in references]
    observed_min = min(ref_widths)
    observed_max = max(ref_widths)
    if observed_max - observed_min > tolerance_pt:
        detail = ", ".join(f"{row['university']}={row['per_year'][reference_year]['bar']['width']:.6f}pt" for row in references)
        raise CalibrationError(
            f"Reference bars are not equal within {tolerance_pt:g}pt: {detail}. "
            "Check the reference selection or provide an explicit calibration override."
        )
    full_width = statistics.median(ref_widths)

    # Annual columns can have different physical track widths (the 2026
    # release's 2023 track is 0.84pt shorter). Anchor the meaning of "full" in
    # the explicit known-full reference, then recover each column's design
    # width only when at least three independent numeric bars repeat at that
    # maximum. A unique longest observation is never accepted as calibration.
    widths: dict[int, float] = {}
    width_evidence: dict[str, Any] = {}
    for year in years:
        values = [
            float(row["per_year"][year]["bar"]["width"])
            for row in rows
            if row["per_year"][year]["status"] == "measured"
        ]
        if not values:
            raise CalibrationError(f"No measured numeric bars found for {year}")
        observed_column_max = max(values)
        repeated_full = [
            value for value in values
            if abs(value - observed_column_max) <= tolerance_pt
        ]
        if len(repeated_full) < 3:
            raise CalibrationError(
                f"Need at least three repeated full-width bars for {year}; "
                f"found {len(repeated_full)} at {observed_column_max:.6f}pt. "
                "Provide an explicit full_width_pt_by_year calibration."
            )
        widths[year] = statistics.median(repeated_full)
        # Coordinate serialization can differ by a few ten-thousandths of a
        # point between otherwise identical tracks. Preserve the explicit
        # reference width when the annual mode is within the declared
        # tolerance, while retaining materially different track widths.
        if abs(widths[year] - full_width) <= tolerance_pt:
            widths[year] = full_width
        width_evidence[str(year)] = {
            "full_width_pt": _rounded(widths[year]),
            "repeated_bar_count": len(repeated_full),
            "observed_max_pt": _rounded(observed_column_max),
        }
    if abs(widths[reference_year] - full_width) > tolerance_pt:
        raise CalibrationError(
            f"Known-full reference is {full_width:.6f}pt, but the repeated "
            f"{reference_year} track maximum is {widths[reference_year]:.6f}pt"
        )

    oversized: list[str] = []
    for row in rows:
        for year in years:
            cell = row["per_year"][year]
            if cell["status"] != "measured":
                continue
            width = float(cell["bar"]["width"])
            if width > widths[year] + tolerance_pt:
                oversized.append(f"{row['university']} {year}={width:.6f}pt")
    if oversized:
        raise CalibrationError(
            "Observed numeric bars exceed their annual full-bar tracks: "
            + ", ".join(oversized[:8])
        )

    reference_payload = []
    for row in references:
        bar = row["per_year"][reference_year]["bar"]
        reference_payload.append(
            {
                "country": row["country"],
                "university": row["university"],
                "year": reference_year,
                "page": row["page"],
                "row_index": row["row_index"],
                "raw_width_pt": _rounded(float(bar["width"])),
                "bar_bbox": _bbox(bar),
            }
        )
    status_examples = []
    for row in rows:
        for year in years:
            cell = row["per_year"][year]
            if cell["status"] == "no_applications" and cell.get("status_box"):
                rect = cell["status_box"]
                status_examples.append(
                    {
                        "country": row["country"],
                        "university": row["university"],
                        "year": year,
                        "status": "no_applications",
                        "status_box_width_pt": _rounded(float(rect["width"])),
                        "status_box_height_pt": _rounded(float(rect["height"])),
                        "status_box_bbox": _bbox(rect),
                    }
                )
    columns_payload = {str(year): dict(columns[year]) for year in years}
    return {
        "calibration_id": f"calibration:{source_document_id}:{_norm(reference_country).replace(' ', '-')}-{reference_year}",
        "method": "explicit known-full reference plus repeated annual track widths",
        "formula": "bar_fraction = raw_width_pt / full_width_pt; score_percent = 100 * bar_fraction; widths within tolerance of full are snapped to 1.0",
        "units": "PDF points (pt)",
        "full_width_pt": _rounded(full_width),
        "full_width_pt_by_year": {str(year): _rounded(widths[year]) for year in years},
        "reference_country": reference_country,
        "reference_year": reference_year,
        "reference_bars": reference_payload,
        "tolerance_pt": _rounded(tolerance_pt),
        "tolerance_verification": {
            "verified_equal": True,
            "reference_count": len(reference_payload),
            "observed_min_pt": _rounded(observed_min),
            "observed_max_pt": _rounded(observed_max),
            "spread_pt": _rounded(observed_max - observed_min),
        },
        "annual_track_width_evidence": width_evidence,
        "geometry": {
            "coordinate_system": "pdfplumber top-left page coordinates",
            "column_x0_pt": {str(year): _rounded(float(columns[year]["x0"])) for year in years},
            "reference_bar_height_pt": _rounded(
                float(columns[reference_year].get("bar_height", 8.4))
            ),
            "numeric_bar_rectangles_only": True,
            "status_box_geometry_excluded_from_numeric_calibration": True,
        },
        "status_assumptions": {
            "no_applications": "treated as 100% under the project assumption; its padded text box is not a numeric bar",
            "no_cooperation": "excluded as not available",
            "no_places": "excluded as not available",
            "bar_absent": "excluded as unavailable or unrecognised",
        },
        "status_box_examples": status_examples,
        "source_sha256": source_sha256,
        "source_filename": source_filename,
        "source_document_id": source_document_id,
        "source_release_year": source_release_year,
        "extraction_version": EXTRACTION_VERSION,
        "year_columns": columns_payload,
        "methodology": {
            "reference_selection": (
                f"measured row country={reference_country!r}, "
                f"university={reference_university!r}, year={reference_year}"
                if reference_university is not None
                else f"all measured rows under country={reference_country!r} and year={reference_year}"
            ),
            "normalisation": (
                "the known-full reference anchors the scale; each annual "
                "column uses its own width only when at least three bars "
                "repeat at that track maximum"
            ),
            "score_interpretation": "normalized bar length under the user's linear chance assumption, not an independently estimated admission probability",
        },
    }


def _finalise_records(
    rows: Sequence[Mapping[str, Any]],
    calibration: Mapping[str, Any],
    *,
    source_sha256: str,
    source_filename: str,
    source_document_id: str,
    source_release_year: int,
) -> list[dict[str, Any]]:
    widths = {int(key): float(value) for key, value in calibration["full_width_pt_by_year"].items()}
    tolerance = float(calibration.get("tolerance_pt", 0.01))
    calibration_id = str(calibration["calibration_id"])
    records: list[dict[str, Any]] = []
    for row in rows:
        for year in sorted(widths):
            cell = row["per_year"][year]
            status = str(cell["status"])
            if status not in VALID_STATUSES:
                status = "ambiguous"
            bar = cell.get("bar")
            status_box = cell.get("status_box")
            raw_width = _rounded(float(bar["width"])) if status == "measured" and bar else None
            full_width = _rounded(widths[year]) if status == "measured" else None
            bar_fraction: float | None = None
            score_percent: float | None = None
            score_basis = "not_available"
            bar_bbox = _bbox(bar) if status == "measured" and bar else None
            status_box_bbox = _bbox(status_box) if status_box else None
            status_box_width = _rounded(float(status_box["width"])) if status_box else None
            if status == "measured" and bar:
                fraction = float(bar["width"]) / widths[year]
                if abs(float(bar["width"]) - widths[year]) <= tolerance:
                    fraction = 1.0
                if fraction < -1e-9 or fraction > 1.0 + max(1e-9, tolerance / widths[year]):
                    raise CalibrationError(
                        f"Bar width {float(bar['width']):.6f}pt exceeds calibrated width "
                        f"{widths[year]:.6f}pt for {row['university']} {year}"
                    )
                fraction = max(0.0, min(1.0, fraction))
                bar_fraction = _rounded(fraction)
                score_percent = _rounded(fraction * 100)
                full_width = _rounded(widths[year])
                score_basis = "measured_bar"
            elif status == "no_applications":
                # The project owner explicitly treats this status as the full
                # chance under the working assumption.  The status box itself
                # is padded text geometry, so raw_width_pt remains null.
                bar_fraction = 1.0
                score_percent = 100.0
                full_width = _rounded(widths[year])
                score_basis = "no_applications_assumed_full"
            row_id = f"{source_document_id}:{int(row['row_index'])}:{year}"
            note = str(cell.get("notes") or "")
            if status == "no_applications":
                note += "; score set to 100% by explicit project assumption; status box excluded from raw bar width"
            records.append(
                {
                    "row_id": row_id,
                    "source_document_id": source_document_id,
                    "source_sha256": source_sha256,
                    "source_filename": source_filename,
                    "source_release_year": source_release_year,
                    "extraction_version": EXTRACTION_VERSION,
                    "country": str(row["country"]),
                    "university": str(row["university"]),
                    "year": int(year),
                    "page": int(row["page"]),
                    "row_index": int(row["row_index"]),
                    "status": status,
                    "score_percent": score_percent,
                    "score_basis": score_basis,
                    "bar_fraction": bar_fraction,
                    "raw_width_pt": raw_width,
                    "full_width_pt": full_width,
                    "calibration_id": calibration_id if score_basis != "not_available" else None,
                    "bar_bbox": bar_bbox,
                    "status_box_bbox": status_box_bbox,
                    "status_box_width_pt": status_box_width,
                    "notes": note,
                    "raw_label": str(row["raw_label"]),
                    "raw_university_text": str(row["raw_university_text"]),
                    "column_label": str(year),
                    "units": "pt",
                    "geometry": {
                        "coordinate_system": "pdfplumber top-left page coordinates",
                        "bar_height_pt": _rounded(float(bar["height"])) if bar else None,
                    },
                }
            )
    return records


def extract_pdf(
    pdf_path: str | Path,
    *,
    calibration_override: Mapping[str, Any] | str | Path | None = None,
    reference_country: str = "Brazil",
    reference_year: int = 2025,
    reference_university: str | None = "Universidade Federal do Rio de Janeiro (UFRJ)",
    tolerance_pt: float = 0.001,
    allow_incomplete: bool = False,
    allow_cross_source_calibration: bool = False,
    source_release_year: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract all university-year cells and return ``(records, calibration)``.

    The default calibration is deliberately explicit: UFRJ's measured Brazil
    cell in the 2025 column is the known full-bar reference.  A JSON
    ``calibration_override`` may supply ``full_width_pt_by_year`` for another
    same-layout export.  Overrides carrying a different source hash are
    rejected unless ``allow_cross_source_calibration`` is explicit; an
    override without a source hash is accepted only after its live column
    geometry is checked when the PDF has a readable header.
    """

    path = Path(pdf_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise ExtractionError(f"PDF does not exist: {path}")
    source_sha256 = _sha256(path)
    source_document_id = f"sha256:{source_sha256[:16]}"
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - exercised by packaging users
        raise ExtractionError("pdfplumber is required; install the project dependencies first") from exc
    try:
        with pdfplumber.open(str(path)) as pdf:
            pages = list(pdf.pages)
            if not pages:
                raise UnsupportedLayoutError("PDF has no pages")
            has_vector_objects = any(page.rects or page.curves for page in pages)
            if not has_vector_objects:
                raise UnsupportedLayoutError(
                    "No vector rectangle/curve objects found. Scanned or raster PDFs are not supported."
                )
            override = _load_override(calibration_override)
            override_columns = _columns_from_override(override)
            live_columns: dict[int, dict[str, float]] | None = None
            try:
                live_columns = _find_year_columns(pages)
            except UnsupportedLayoutError:
                # An explicit override can carry the year map for a same-layout
                # PDF whose text header was removed.  When a live header exists,
                # however, it is always authoritative for geometry validation.
                if override_columns is None:
                    raise
            if live_columns is not None and override_columns is not None:
                _validate_override_columns(live_columns, override_columns)
            columns = live_columns or override_columns
            if columns is None:  # defensive; both paths above raise normally
                raise UnsupportedLayoutError("Could not determine year columns")
            release_year = _source_release_year(path, columns, source_release_year)
            rows = _prepare_rows(pages, columns)
            if not rows:
                raise UnsupportedLayoutError("No university rows were found in the supported vector layout")
            measured = sum(
                1
                for row in rows
                for cell in row["per_year"].values()
                if cell["status"] == "measured"
            )
            statuses = sum(
                1
                for row in rows
                for cell in row["per_year"].values()
                if cell["status"] in STATUS_TEXT.values()
            )
            if measured == 0 and statuses == 0:
                raise UnsupportedLayoutError(
                    "No numeric vector bars or recognised table statuses were found; "
                    "the PDF may use an unsupported layout"
                )
            incomplete = [
                f"page {row['page']} row {row['row_index']} {row['university']!r} {year}: {cell['status']}"
                for row in rows
                for year, cell in row["per_year"].items()
                if cell["status"] in {"ambiguous", "bar_absent"}
            ]
            if incomplete and not allow_incomplete:
                preview = "; ".join(incomplete[:8])
                suffix = f" (and {len(incomplete) - 8} more)" if len(incomplete) > 8 else ""
                raise UnsupportedLayoutError(
                    "Extraction found ambiguous or missing cells and stopped before writing data: "
                    + preview
                    + suffix
                    + ". Use --allow-incomplete only after reviewing the source layout."
                )
            calibration = _build_calibration(
                rows,
                columns,
                source_sha256=source_sha256,
                source_filename=path.name,
                source_document_id=source_document_id,
                source_release_year=release_year,
                reference_country=reference_country,
                reference_year=int(reference_year),
                reference_university=reference_university,
                tolerance_pt=float(tolerance_pt),
                override_value=calibration_override,
                allow_cross_source_calibration=allow_cross_source_calibration,
            )
            records = _finalise_records(
                rows,
                calibration,
                source_sha256=source_sha256,
                source_filename=path.name,
                source_document_id=source_document_id,
                source_release_year=release_year,
            )
            calibration["record_count"] = len(records)
            calibration["measured_count"] = sum(1 for record in records if record["status"] == "measured")
            calibration["no_applications_count"] = sum(1 for record in records if record["status"] == "no_applications")
            calibration["unavailable_count"] = sum(1 for record in records if record["score_percent"] is None)
            calibration["university_count"] = len(rows)
            return records, calibration
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"Failed to parse vector PDF {path.name}: {exc}") from exc


def extract_document(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Convenience wrapper returning a serialisable document object."""

    records, calibration = extract_pdf(*args, **kwargs)
    return {
        "source_document_id": records[0]["source_document_id"] if records else None,
        "source_sha256": records[0]["source_sha256"] if records else calibration.get("source_sha256"),
        "source_filename": records[0]["source_filename"] if records else calibration.get("source_filename"),
        "extraction_version": EXTRACTION_VERSION,
        "records": records,
        "calibration": calibration,
    }


__all__ = [
    "EXTRACTION_VERSION",
    "CalibrationError",
    "ExtractionError",
    "UnsupportedLayoutError",
    "extract_document",
    "extract_pdf",
]
