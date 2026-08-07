#!/usr/bin/env python3
"""Validate the structural invariants of a UTF-8 GEDCOM 5.5.1 file."""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


LINE_RE = re.compile(r"^(\d+)\s+(.+)$")
POINTER_RE = re.compile(r"(?<!@)@[-A-Za-z0-9_.:+]+@(?!@)")


@dataclass
class Record:
    xref: str
    kind: str
    fields: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate UTF-8, CRLF, xrefs, pointers, and reciprocal family links."
    )
    parser.add_argument("gedcom", help="Path to family-tree.ged")
    parser.add_argument("--expect-individuals", type=int)
    parser.add_argument("--expect-families", type=int)
    return parser.parse_args()


def split_payload(payload: str) -> tuple[str, str]:
    tag, separator, value = payload.partition(" ")
    return tag, value if separator else ""


def main() -> int:
    args = parse_args()
    path = Path(args.gedcom)
    errors: list[str] = []
    warnings: list[str] = []

    try:
        raw = path.read_bytes()
    except OSError as exc:
        print(f"ERROR: cannot read {path}: {exc}", file=sys.stderr)
        return 2

    if raw.startswith(b"\xef\xbb\xbf"):
        errors.append("UTF-8 BOM is not allowed")

    non_crlf = raw.replace(b"\r\n", b"")
    if b"\n" in non_crlf or b"\r" in non_crlf:
        errors.append("file contains a line ending other than CRLF")
    if raw and not raw.endswith(b"\r\n"):
        errors.append("file must end with CRLF")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        print(f"ERROR: file is not valid UTF-8: {exc}", file=sys.stderr)
        return 1

    lines = text.splitlines()
    if not lines:
        errors.append("file is empty")

    records: dict[str, Record] = {}
    definitions: dict[str, int] = {}
    references: list[tuple[str, int]] = []
    current: Record | None = None

    for number, line in enumerate(lines, 1):
        match = LINE_RE.match(line)
        if not match:
            errors.append(f"line {number}: invalid GEDCOM line")
            current = None
            continue

        level = int(match.group(1))
        payload = match.group(2)
        first, remainder = split_payload(payload)

        if level == 0:
            current = None
            if first.startswith("@") and first.endswith("@"):
                xref = first
                kind, _ = split_payload(remainder)
                if not kind:
                    errors.append(f"line {number}: xref record has no type")
                    continue
                if xref in definitions:
                    errors.append(
                        f"line {number}: duplicate xref {xref}; first defined on line {definitions[xref]}"
                    )
                    continue
                definitions[xref] = number
                current = Record(xref=xref, kind=kind)
                records[xref] = current
                for pointer in POINTER_RE.findall(remainder[len(kind) :]):
                    references.append((pointer, number))
            continue

        if current is None:
            continue

        tag = first
        value = remainder
        current.fields[tag].append(value)
        for pointer in POINTER_RE.findall(value):
            references.append((pointer, number))

    for pointer, number in references:
        if pointer not in definitions:
            errors.append(f"line {number}: dangling pointer {pointer}")

    if lines:
        if lines[0] != "0 HEAD":
            errors.append("first record must be exactly '0 HEAD'")
        if lines[-1] != "0 TRLR":
            errors.append("last record must be exactly '0 TRLR'")

    individuals = {xref: rec for xref, rec in records.items() if rec.kind == "INDI"}
    families = {xref: rec for xref, rec in records.items() if rec.kind == "FAM"}

    def require_kind(pointer: str, expected: str, context: str) -> Record | None:
        record = records.get(pointer)
        if record is None:
            return None
        if record.kind != expected:
            errors.append(f"{context}: {pointer} is {record.kind}, expected {expected}")
            return None
        return record

    for person_xref, person in individuals.items():
        for family_xref in person.fields.get("FAMC", []):
            family = require_kind(family_xref, "FAM", f"{person_xref} FAMC")
            if family and person_xref not in family.fields.get("CHIL", []):
                errors.append(f"{person_xref} FAMC {family_xref} lacks reciprocal CHIL")
        for family_xref in person.fields.get("FAMS", []):
            family = require_kind(family_xref, "FAM", f"{person_xref} FAMS")
            spouses = [] if family is None else family.fields.get("HUSB", []) + family.fields.get("WIFE", [])
            if family and person_xref not in spouses:
                errors.append(f"{person_xref} FAMS {family_xref} lacks reciprocal spouse link")

    for family_xref, family in families.items():
        for tag in ("HUSB", "WIFE"):
            for person_xref in family.fields.get(tag, []):
                person = require_kind(person_xref, "INDI", f"{family_xref} {tag}")
                if person and family_xref not in person.fields.get("FAMS", []):
                    errors.append(f"{family_xref} {tag} {person_xref} lacks reciprocal FAMS")
        for person_xref in family.fields.get("CHIL", []):
            person = require_kind(person_xref, "INDI", f"{family_xref} CHIL")
            if person and family_xref not in person.fields.get("FAMC", []):
                errors.append(f"{family_xref} CHIL {person_xref} lacks reciprocal FAMC")

    if args.expect_individuals is not None and len(individuals) != args.expect_individuals:
        errors.append(
            f"individual count is {len(individuals)}, expected {args.expect_individuals}"
        )
    if args.expect_families is not None and len(families) != args.expect_families:
        errors.append(f"family count is {len(families)}, expected {args.expect_families}")

    if not individuals:
        warnings.append("tree contains no individuals")

    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)

    print(
        f"GEDCOM summary: {len(individuals)} individuals, {len(families)} families, "
        f"{len(definitions)} xrefs"
    )
    if errors:
        print(f"Validation failed with {len(errors)} error(s).", file=sys.stderr)
        return 1

    print("Validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

