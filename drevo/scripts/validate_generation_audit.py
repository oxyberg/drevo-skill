#!/usr/bin/env python3
"""Validate that a generation audit follows Drevo's evidence-report format."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


TITLE_RE = re.compile(r"^# Аудит доказательств (?:ветви|линии|главной ветви)\s+\S", re.MULTILINE)
DATE_RE = re.compile(r"^Дата проверки: \d{4}-\d{2}-\d{2}\.$", re.MULTILINE)
TABLE_HEADER = "| Переход | Что имеется | Статус | Что требуется |"
LINE_HEADINGS = {"## Проверка каждого звена", "## Проверка прямой линии"}
PRIORITY_HEADINGS = {
    "## Следующий приоритет",
    "## Что даст непрерывную доказательную цепочку",
}
FORBIDDEN_HEADINGS = {
    "## Область аудита",
    "## Автоматический итог",
    "## Покрытие поколений",
    "## Материалы",
    "## Следующие приоритеты",
}
PLACEHOLDERS = (
    "НЕ ОЦЕНЁН",
    "Служебные данные сборщика",
    "[минимально достаточный источник или проверка]",
    "[ссылки на фактически использованные",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the final Markdown format of a Drevo generation audit."
    )
    parser.add_argument("report", help="Final generation audit Markdown file")
    return parser.parse_args()


def validate(text: str) -> list[str]:
    errors: list[str] = []
    headings = {line.strip() for line in text.splitlines() if line.startswith("## ")}

    if not TITLE_RE.search(text):
        errors.append(
            "title must name a specific branch or line as '# Аудит доказательств ветви …'"
        )
    if not DATE_RE.search(text):
        errors.append("date must use 'Дата проверки: YYYY-MM-DD.'")
    if "## Итог" not in headings:
        errors.append("missing '## Итог'")
    if not headings.intersection(LINE_HEADINGS):
        errors.append("missing a supported lineage-check heading")
    if TABLE_HEADER not in text:
        errors.append(
            "lineage table must have exactly: Переход / Что имеется / Статус / Что требуется"
        )
    if "## Решение по дереву" not in headings:
        errors.append("missing '## Решение по дереву'")
    if not headings.intersection(PRIORITY_HEADINGS):
        errors.append("missing the next-priority or evidence-chain section")

    for heading in sorted(headings.intersection(FORBIDDEN_HEADINGS)):
        errors.append(f"forbidden machine-oriented heading remains: {heading}")
    for marker in PLACEHOLDERS:
        if marker in text:
            errors.append(f"unfinished scaffold marker remains: {marker}")
    return errors


def main() -> int:
    args = parse_args()
    path = Path(args.report)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"ERROR: cannot read report: {exc}", file=sys.stderr)
        return 2

    errors = validate(text)
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        print(f"Generation audit validation failed with {len(errors)} error(s).", file=sys.stderr)
        return 1
    print("Generation audit format is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
