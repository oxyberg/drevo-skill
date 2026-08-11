from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "drevo" / "scripts" / "assemble_generation_audit.py"
VALIDATOR = REPOSITORY / "drevo" / "scripts" / "validate_generation_audit.py"


GEDCOM = "\r\n".join(
    (
        "0 HEAD",
        "1 SOUR TEST",
        "1 GEDC",
        "2 VERS 5.5.1",
        "2 FORM LINEAGE-LINKED",
        "1 CHAR UTF-8",
        "0 @I1@ INDI",
        "1 NAME Текущий /Человек/",
        "1 BIRT",
        "2 DATE 1 JAN 2000",
        "2 SOUR @S1@",
        "1 FAMC @F1@",
        "0 @I2@ INDI",
        "1 NAME Молодой /Отец/",
        "1 BIRT",
        "2 DATE 1 JAN 1970",
        "1 FAMC @F2@",
        "1 FAMS @F1@",
        "0 @I3@ INDI",
        "1 NAME Историческая /Мать/",
        "1 BIRT",
        "2 DATE 1 JAN 1870",
        "1 FAMS @F1@",
        "0 @I4@ INDI",
        "1 NAME Старший /Предок/",
        "1 BIRT",
        "2 DATE ABT 1840",
        "1 FAMS @F2@",
        "0 @F1@ FAM",
        "1 HUSB @I2@",
        "1 WIFE @I3@",
        "1 CHIL @I1@",
        "1 SOUR @S2@",
        "0 @F2@ FAM",
        "1 HUSB @I4@",
        "1 CHIL @I2@",
        "0 @S1@ SOUR",
        "1 TITL Частное свидетельство о рождении",
        "0 @S2@ SOUR",
        "1 TITL Семейный документ",
        "0 TRLR",
        "",
    )
)


def run_script(*arguments: str) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(SCRIPT), *arguments]
    if "--title" not in arguments:
        command.extend(("--title", "Аудит доказательств тестовой ветви"))
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def validate_report(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


class GenerationAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.gedcom = self.root / "family-tree.ged"
        self.gedcom.write_bytes(GEDCOM.encode("utf-8"))
        self.output = self.root / "research" / "reports" / "audit.md"

    def tearDown(self):
        self.temporary.cleanup()

    def test_all_ancestors_builds_private_structural_draft(self):
        result = run_script(
            str(self.gedcom),
            "--root",
            "@I1@",
            "--generations",
            "2",
            "--output",
            str(self.output),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.output.read_text(encoding="utf-8")
        self.assertIn("[имя скрыто] (@I1@)", report)
        self.assertIn("Историческая Мать (@I3@)", report)
        self.assertNotIn("Текущий Человек", report)
        self.assertNotIn("Частное свидетельство", report)
        self.assertIn("`@S1@` [название скрыто]", report)
        self.assertIn("# Аудит доказательств тестовой ветви", report)
        self.assertIn("Дата проверки:", report)
        self.assertIn("| Переход | Что имеется | Статус | Что требуется |", report)
        self.assertIn("## Проверка каждого звена", report)
        self.assertEqual(report.count("`НЕ ОЦЕНЁН` | ["), 3)
        self.assertIn("Ближайший структурный разрыв начинается в поколении 2", report)
        self.assertNotIn("## Область аудита", report)
        self.assertNotIn("## Автоматический итог", report)
        self.assertNotIn("## Покрытие поколений", report)
        self.assertNotIn("## Материалы", report)

    def test_private_local_mode_can_show_names_and_source_titles(self):
        result = run_script(
            str(self.gedcom),
            "--path",
            "@I1@,@I2@,@I4@",
            "--include-potentially-living",
            "--output",
            str(self.output),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.output.read_text(encoding="utf-8")
        self.assertIn("Текущий Человек (@I1@)", report)
        self.assertIn("Частное свидетельство о рождении", report)
        self.assertIn("выбранная прямая линия", report)
        self.assertIn("Структурных разрывов в выбранной области не найдено", report)
        self.assertIn("## Проверка прямой линии", report)

    def test_invalid_explicit_path_is_rejected(self):
        result = run_script(
            str(self.gedcom),
            "--path",
            "@I1@,@I4@",
            "--output",
            str(self.output),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("is not a GEDCOM parent", result.stderr)
        self.assertFalse(self.output.exists())

    def test_specific_title_is_required(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(self.gedcom),
                "--root",
                "@I1@",
                "--output",
                str(self.output),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--title", result.stderr)

    def test_existing_report_is_not_overwritten_without_force(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_text("keep\n", encoding="utf-8")
        result = run_script(
            str(self.gedcom),
            "--root",
            "@I1@",
            "--output",
            str(self.output),
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "keep\n")

    def test_multiple_parent_families_require_an_explicit_path(self):
        ambiguous = GEDCOM.replace(
            "1 FAMC @F1@\r\n", "1 FAMC @F1@\r\n1 FAMC @F3@\r\n", 1
        )
        self.gedcom.write_bytes(ambiguous.encode("utf-8"))
        result = run_script(
            str(self.gedcom),
            "--root",
            "@I1@",
            "--output",
            str(self.output),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("multiple parent families", result.stderr)
        self.assertFalse(self.output.exists())

    def test_assembled_scaffold_is_not_accepted_as_a_final_report(self):
        result = run_script(
            str(self.gedcom),
            "--path",
            "@I1@,@I2@,@I4@",
            "--output",
            str(self.output),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        validation = validate_report(self.output)
        self.assertEqual(validation.returncode, 1)
        self.assertIn("unfinished scaffold marker", validation.stderr)

    def test_completed_house_format_is_accepted(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_text(
            "# Аудит доказательств ветви Тестовой\n\n"
            "Дата проверки: 2026-08-11.\n\n"
            "## Итог\n\nЛиния проверена.\n\n"
            "## Проверка каждого звена\n\n"
            "| Переход | Что имеется | Статус | Что требуется |\n"
            "|---|---|---|---|\n"
            "| Потомок → родитель | Первичный документ | `CONFIRMED` | — |\n\n"
            "## Решение по дереву\n\nИзменений не требуется.\n\n"
            "## Следующий приоритет\n\nПроверить следующее звено.\n\n"
            "Сохранённые материалы: [отчёт](report.md).\n",
            encoding="utf-8",
        )
        validation = validate_report(self.output)
        self.assertEqual(validation.returncode, 0, validation.stderr)


if __name__ == "__main__":
    unittest.main()
