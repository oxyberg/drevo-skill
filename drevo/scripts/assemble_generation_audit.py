#!/usr/bin/env python3
"""Assemble a privacy-aware structural draft for a generation evidence audit."""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


LINE_RE = re.compile(r"^(\d+)\s+(.+)$")
YEAR_RE = re.compile(r"(?<!\d)(\d{3,4})(?!\d)")
XREF_RE = re.compile(r"^@[-A-Za-z0-9_.:+]+@$")
DEFAULT_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "project-template"
    / "research"
    / "reports"
    / "generation-audit.template.md"
)


@dataclass
class Record:
    xref: str
    kind: str
    fields: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    event_fields: dict[str, dict[str, list[str]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(list))
    )


@dataclass(frozen=True)
class Transition:
    generation: int
    child: str
    parent: str
    role: str
    family: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Assemble a structural Markdown draft for a generation audit."
    )
    parser.add_argument("gedcom", help="Canonical GEDCOM file")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--root", help="Root individual xref for an all-ancestor audit")
    scope.add_argument(
        "--path",
        help="Comma-separated direct line from descendant to ancestor",
    )
    parser.add_argument(
        "--generations",
        type=int,
        default=6,
        help="Ancestor depth for --root, from 1 through 12 (default: 6)",
    )
    parser.add_argument("--output", required=True, help="Markdown report path")
    parser.add_argument("--title", help="Report title without the Markdown marker")
    parser.add_argument(
        "--template",
        default=str(DEFAULT_TEMPLATE),
        help="Markdown template path",
    )
    parser.add_argument(
        "--include-potentially-living",
        action="store_true",
        help="show names of potentially living people in a private local report",
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing output file"
    )
    return parser.parse_args()


def split_payload(payload: str) -> tuple[str, str]:
    tag, separator, value = payload.partition(" ")
    return tag, value if separator else ""


def parse_gedcom(path: Path) -> dict[str, Record]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read GEDCOM: {exc}") from exc

    records: dict[str, Record] = {}
    current: Record | None = None
    current_event: str | None = None

    for number, line in enumerate(text.splitlines(), 1):
        match = LINE_RE.match(line)
        if not match:
            raise ValueError(f"line {number}: invalid GEDCOM line")
        level = int(match.group(1))
        first, remainder = split_payload(match.group(2))

        if level == 0:
            current = None
            current_event = None
            if XREF_RE.match(first):
                kind, _ = split_payload(remainder)
                if kind:
                    if first in records:
                        raise ValueError(f"line {number}: duplicate xref {first}")
                    current = Record(xref=first, kind=kind)
                    records[first] = current
            continue

        if current is None:
            continue
        if level == 1:
            current.fields[first].append(remainder)
            current_event = first if first in {"BIRT", "DEAT"} else None
        elif level == 2 and current_event:
            current.event_fields[current_event][first].append(remainder)

    return records


def clean_name(value: str) -> str:
    return " ".join(value.replace("/", " ").split()) or "[без имени]"


def event_year(record: Record, event: str) -> int | None:
    values = record.event_fields.get(event, {}).get("DATE", [])
    years = [int(match.group(1)) for value in values for match in YEAR_RE.finditer(value)]
    return years[-1] if years else None


def potentially_living(record: Record, current_year: int) -> bool:
    if record.event_fields.get("DEAT") or record.fields.get("DEAT"):
        return False
    birth_year = event_year(record, "BIRT")
    return birth_year is None or current_year - birth_year < 110


def display_person(
    xref: str,
    records: dict[str, Record],
    include_potentially_living: bool,
    current_year: int,
) -> str:
    record = records.get(xref)
    if record is None or record.kind != "INDI":
        return f"{xref} [не найден]"
    if potentially_living(record, current_year) and not include_potentially_living:
        return f"{xref} [имя скрыто]"
    names = record.fields.get("NAME", [])
    return f"{xref} {clean_name(names[0]) if names else '[без имени]'}"


def parent_links(
    child_xref: str, records: dict[str, Record]
) -> list[tuple[str, str, str]]:
    child = records.get(child_xref)
    if child is None or child.kind != "INDI":
        return []
    links: list[tuple[str, str, str]] = []
    for family_xref in child.fields.get("FAMC", []):
        family = records.get(family_xref)
        if family is None or family.kind != "FAM":
            continue
        for parent in family.fields.get("HUSB", []):
            links.append((parent, "HUSB / отец", family_xref))
        for parent in family.fields.get("WIFE", []):
            links.append((parent, "WIFE / мать", family_xref))
    return links


def source_candidates(
    transition: Transition, records: dict[str, Record], hide_titles: bool
) -> str:
    candidates: list[tuple[str, str]] = []
    family = records.get(transition.family)
    child = records.get(transition.child)
    parent = records.get(transition.parent)
    if family:
        candidates.extend((xref, "семья") for xref in family.fields.get("SOUR", []))
    if child:
        candidates.extend((xref, "ребёнок") for xref in child.fields.get("SOUR", []))
        candidates.extend(
            (xref, "рождение ребёнка")
            for xref in child.event_fields.get("BIRT", {}).get("SOUR", [])
        )
    if parent:
        candidates.extend((xref, "родитель") for xref in parent.fields.get("SOUR", []))

    rendered: list[str] = []
    seen: set[tuple[str, str]] = set()
    for xref, context in candidates:
        if (xref, context) in seen:
            continue
        seen.add((xref, context))
        source = records.get(xref)
        titles = [] if source is None else source.fields.get("TITL", [])
        title = "[название скрыто]" if hide_titles and titles else (titles[0] if titles else "")
        label = f"`{xref}`"
        if title:
            label += f" {escape_cell(title)}"
        rendered.append(f"{label} ({context})")
    return "; ".join(rendered) if rendered else "—"


def escape_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def all_ancestor_scope(
    root: str, generations: int, records: dict[str, Record]
) -> tuple[list[Transition], list[tuple[int, int, int, int]]]:
    slots: list[str | None] = [root]
    transitions: dict[tuple[str, str, str, str], Transition] = {}
    summary: list[tuple[int, int, int, int]] = []

    for generation in range(generations + 1):
        linked = [xref for xref in slots if xref is not None]
        summary.append((generation, len(slots), len(linked), len(set(linked))))
        if generation == generations:
            break
        next_slots: list[str | None] = []
        for child in slots:
            if child is None:
                next_slots.extend((None, None))
                continue
            child_record = records.get(child)
            parent_families = [] if child_record is None else child_record.fields.get("FAMC", [])
            if len(set(parent_families)) > 1:
                raise ValueError(
                    f"multiple parent families for {child}; use --path for an explicit line"
                )
            links = parent_links(child, records)
            fathers = [item for item in links if item[1].startswith("HUSB")]
            mothers = [item for item in links if item[1].startswith("WIFE")]
            if len(fathers) > 1 or len(mothers) > 1:
                raise ValueError(
                    f"ambiguous parent families for {child}; use --path for an explicit line"
                )
            for choices in (fathers, mothers):
                if not choices:
                    next_slots.append(None)
                    continue
                parent, role, family = choices[0]
                next_slots.append(parent)
                key = (child, parent, role, family)
                transitions.setdefault(
                    key,
                    Transition(generation + 1, child, parent, role, family),
                )
        slots = next_slots
    return sorted(transitions.values(), key=lambda item: (item.generation, item.child, item.role)), summary


def explicit_path_scope(
    path: list[str], records: dict[str, Record]
) -> tuple[list[Transition], list[tuple[int, int, int, int]]]:
    transitions: list[Transition] = []
    for generation, (child, parent) in enumerate(zip(path, path[1:]), 1):
        matches = [link for link in parent_links(child, records) if link[0] == parent]
        if not matches:
            raise ValueError(f"{parent} is not a GEDCOM parent of {child}")
        if len(matches) > 1:
            raise ValueError(
                f"multiple parent links connect {child} to {parent}; resolve the ambiguity first"
            )
        _, role, family = matches[0]
        transitions.append(Transition(generation, child, parent, role, family))
    summary = [(generation, 1, 1, 1) for generation in range(len(path))]
    return transitions, summary


def generation_table(summary: list[tuple[int, int, int, int]]) -> str:
    lines = [
        "| Поколение | Мест | Заполнено | Уникальных персон | Пробелов |",
        "|---:|---:|---:|---:|---:|",
    ]
    for generation, slots, linked, unique in summary:
        lines.append(
            f"| {generation} | {slots} | {linked} | {unique} | {slots - linked} |"
        )
    return "\n".join(lines)


def transition_table(
    transitions: list[Transition],
    records: dict[str, Record],
    include_potentially_living: bool,
    current_year: int,
) -> str:
    lines = [
        "| Поколение родителя | Ребёнок → родитель | Роль и семья | Источники-кандидаты | Автоматическая проверка | Статус | Что требуется |",
        "|---:|---|---|---|---|---|---|",
    ]
    for item in transitions:
        child_record = records[item.child]
        parent_record = records[item.parent]
        child_hidden = potentially_living(child_record, current_year) and not include_potentially_living
        parent_hidden = potentially_living(parent_record, current_year) and not include_potentially_living
        child = display_person(item.child, records, include_potentially_living, current_year)
        parent = display_person(item.parent, records, include_potentially_living, current_year)
        sources = source_candidates(item, records, child_hidden or parent_hidden)
        automatic = "есть кандидаты; прочитать содержание" if sources != "—" else "источник связи в GEDCOM не найден"
        lines.append(
            f"| {item.generation} | {escape_cell(child)} → {escape_cell(parent)} | "
            f"{escape_cell(item.role)}; `{item.family}` | {sources} | {automatic} | "
            "`НЕ ОЦЕНЁН` | [минимально достаточный источник или проверка] |"
        )
    if not transitions:
        lines.append("| — | — | — | — | переходы не найдены | `НЕ ОЦЕНЁН` | проверить структуру дерева |")
    return "\n".join(lines)


def render_report(args: argparse.Namespace, records: dict[str, Record]) -> str:
    current_year = date.today().year
    if args.root:
        if args.generations < 1 or args.generations > 12:
            raise ValueError("--generations must be from 1 through 12")
        root = args.root
        if root not in records or records[root].kind != "INDI":
            raise ValueError(f"root individual {root} was not found")
        transitions, summary = all_ancestor_scope(root, args.generations, records)
        mode = "все предки"
        depth = str(args.generations)
    else:
        path = [item.strip() for item in args.path.split(",") if item.strip()]
        if len(path) < 2:
            raise ValueError("--path must contain at least two individual xrefs")
        for xref in path:
            if xref not in records or records[xref].kind != "INDI":
                raise ValueError(f"path individual {xref} was not found")
        transitions, summary = explicit_path_scope(path, records)
        root = path[0]
        mode = "выбранная прямая линия"
        depth = str(len(path) - 1)

    missing = sum(slots - linked for _, slots, linked, _ in summary[1:])
    first_gap = next(
        (generation for generation, slots, linked, _ in summary[1:] if linked < slots),
        None,
    )
    automatic_summary = (
        f"Собрано переходов: {len(transitions)}. Структурных пустых мест в выбранной области: {missing}. "
        + (
            f"Ближайший структурный разрыв начинается в поколении {first_gap}."
            if first_gap is not None
            else "Структурных разрывов в выбранной области не найдено."
        )
        + " Доказательная непрерывность автоматически не оценивалась."
    )

    try:
        template = Path(args.template).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read template: {exc}") from exc

    privacy = (
        "имена потенциально живых включены для приватного локального отчёта"
        if args.include_potentially_living
        else "имена потенциально живых скрыты"
    )
    title = args.title or "Аудит поколений"
    replacements = {
        "{{TITLE}}": title,
        "{{DATE}}": date.today().isoformat(),
        "{{ROOT}}": display_person(
            root, records, args.include_potentially_living, current_year
        ),
        "{{MODE}}": mode,
        "{{DEPTH}}": depth,
        "{{PRIVACY}}": privacy,
        "{{AUTOMATIC_SUMMARY}}": automatic_summary,
        "{{GENERATION_TABLE}}": generation_table(summary),
        "{{TRANSITION_TABLE}}": transition_table(
            transitions, records, args.include_potentially_living, current_year
        ),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    unresolved = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", template)))
    if unresolved:
        raise ValueError(f"unresolved template markers: {', '.join(unresolved)}")
    return template.rstrip() + "\n"


def main() -> int:
    args = parse_args()
    output = Path(args.output)
    if output.exists() and not args.force:
        print(f"ERROR: output already exists: {output}; use --force", file=sys.stderr)
        return 2
    try:
        records = parse_gedcom(Path(args.gedcom))
        report = render_report(args, records)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Generation audit draft created: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
