"""Replay stored posts through the parser and count what it reads differently.

The parity gate for format profiles: a dump of stored posts goes in, each
row's raw text is parsed again with the formats of a config file, and every
row whose result differs from the stored one is a diff. Zero diffs over every
stored row is the gate; changing one word of a format and seeing diffs appear
is its positive control.

    python scripts/replay_posts.py posts.csv --config config.yaml
    python scripts/replay_posts.py posts.json --config config.yaml --format cards --show 20

The dump is CSV with a header row, or JSON (a list of objects). Columns:
``id``, ``raw_text``, ``media_type``, ``title``, ``year``, ``quality``,
``season``, ``external_links`` (a JSON list), and when present ``source``,
``size_text`` and ``release_group``. A column the dump does not carry is not
compared. Empty and NULL are the same value.

A dump that also carries ``non_media``, the reason the message was finished
with or empty when it stayed reviewable, has that compared too: a mistake in
``media_words`` or ``ignore_hosts`` changes no parsed field and shows only
there. Every row is read with one format list, so a source's own ``formats``
is not exercised: replay each source's rows with ``--format``.

Only the ``formats`` block of the config file is read, so a full config with
secrets in ``${VAR}`` references needs none of them set.

Exit code: 0 with no diffs, 1 with diffs, 2 on bad input.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

FIELDS = (
    "media_type",
    "title",
    "year",
    "quality",
    "source",
    "season",
    "size_text",
    "release_group",
    "external_links",
)
# What classify_non_media answered for the row; compared when the dump has it.
NON_MEDIA = "non_media"


def read_dump(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json" or text.lstrip().startswith("["):
        rows = json.loads(text)
        if not isinstance(rows, list):
            raise ValueError(f"{path}: a JSON dump is a list of objects")
        return rows
    csv.field_size_limit(sys.maxsize)
    return list(csv.DictReader(text.splitlines(keepends=True)))


def _norm(field: str, value: Any) -> Any:
    if value is None or value == "":
        return None
    if field == "media_type":
        value = getattr(value, "value", value)
        return str(value).lower()
    if field == "year":
        return int(value)
    if field == "external_links":
        if isinstance(value, str):
            value = json.loads(value)
        return list(value) or None
    return str(value)


def compare(
    rows: Iterable[dict[str, Any]],
    parse: Callable[[str], Any],
    classify: Callable[[str], str | None] | None = None,
) -> tuple[int, list[tuple[Any, str, Any, Any]]]:
    """Parse every row; return the row count and one entry per differing field.

    With ``classify``, a row that carries ``non_media`` has that compared too.
    """
    count = 0
    diffs: list[tuple[Any, str, Any, Any]] = []
    for row in rows:
        count += 1
        text = row.get("raw_text")
        if text is None:
            raise ValueError(f"row {row.get('id')!r} has no raw_text")
        parsed = parse(text)
        for field in FIELDS:
            if field not in row:
                continue
            stored = _norm(field, row[field])
            new = _norm(field, getattr(parsed, field))
            if stored != new:
                diffs.append((row.get("id"), field, stored, new))
        if classify is not None and NON_MEDIA in row:
            stored = _norm(NON_MEDIA, row[NON_MEDIA])
            new = _norm(NON_MEDIA, classify(text))
            if stored != new:
                diffs.append((row.get("id"), NON_MEDIA, stored, new))
    return count, diffs


def report(count: int, diffs: Sequence[tuple[Any, str, Any, Any]], show: int) -> str:
    rows_with_diffs = len({d[0] for d in diffs})
    lines = [f"rows: {count}", f"diffs: {rows_with_diffs}"]
    for field, n in sorted(Counter(d[1] for d in diffs).items()):
        lines.append(f"  {field}: {n}")
    for row_id, field, stored, new in diffs[:show]:
        lines.append(f"  id={row_id} {field}: stored={stored!r} new={new!r}")
    return "\n".join(lines)


def load_formats(config_path: Path, keys: Sequence[str]) -> tuple[Any, ...]:
    from ruamel.yaml.comments import CommentedMap

    from vettarr.config.loader import read_document, validate_document
    from vettarr.parser.formats import compile_formats

    doc = read_document(config_path)
    only = CommentedMap()
    only["formats"] = doc.get("formats") or []
    profiles = validate_document(only, str(config_path), env={}).formats
    if keys:
        by_key = {p.key: p for p in profiles}
        missing = [k for k in keys if k not in by_key]
        if missing:
            raise ValueError(f"no format {', '.join(missing)} in {config_path}")
        profiles = [by_key[k] for k in keys]
    return compile_formats(profiles)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dump", type=Path, help="CSV or JSON dump of stored posts")
    ap.add_argument("--config", type=Path, required=True, help="config file whose formats are used")
    ap.add_argument("--format", action="append", default=[], metavar="KEY",
                    help="format to use, in order; repeat it. Default: every format, in file order")
    ap.add_argument("--show", type=int, default=10, help="differing fields to print (default 10)")
    args = ap.parse_args(argv)

    from vettarr.config.loader import ConfigError
    from vettarr.parser.regex_parser import classify_non_media, parse_message

    try:
        formats = load_formats(args.config, args.format)
        rows = read_dump(args.dump)
    except (ConfigError, ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 2
    count, diffs = compare(
        rows,
        lambda text: parse_message(text, formats),
        lambda text: classify_non_media(text, formats),
    )
    print(report(count, diffs, args.show))
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
