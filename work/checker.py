"""블로그 글의 front matter 와 내부 링크를 검사한다.

posts/ 의 마크다운을 읽어 규칙 위반을 찾는다.
위반마다 어느 규칙을 어겼는지, 문제된 원문 줄이 무엇인지 함께 돌려준다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# front matter 에 반드시 있어야 하는 항목
REQUIRED_FIELDS = ("title", "date", "categories", "tags", "description")

# date 는 시간대까지 적는다. +0900 이 없으면 빌드하는 기계의
# 시간대에 따라 발행일이 하루 밀릴 수 있다.
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}$")

# 본문 안의 내부 링크. 예: /blog/posts/2026-01-02-no-desc/
INTERNAL_LINK_PATTERN = re.compile(r"\]\(/blog/posts/([^)/]+)/?\)")


@dataclass(frozen=True)
class Violation:
    """위반 하나. 규칙 이름과 원문 줄을 함께 담는다."""

    rule: str
    line_no: int
    line: str
    message: str

    def describe(self) -> str:
        return f"[{self.rule}] {self.line_no}번 줄 — {self.message}\n    원문: {self.line}"


def split_front_matter(text: str) -> tuple[dict[str, tuple[int, str]], int]:
    """front matter 를 읽어 {항목: (줄번호, 원문줄)} 로 돌려준다.

    두 번째 값은 front matter 가 끝난 줄 번호다. 본문 검사에 쓴다.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, 0

    fields: dict[str, tuple[int, str]] = {}
    end = 0
    for index, line in enumerate(lines[1:], start=2):
        if line.strip() == "---":
            end = index
            break
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
        if match:
            fields[match.group(1)] = (index, line)
    return fields, end


def check_text(text: str, known_slugs: set[str] | None = None) -> list[Violation]:
    """글 하나를 검사한다. known_slugs 를 주면 내부 링크도 본다."""
    violations: list[Violation] = []
    fields, end = split_front_matter(text)

    if not fields:
        return [Violation("front_matter_missing", 1, "", "front matter 를 찾지 못했습니다.")]

    # --- 필수 항목 -------------------------------------------------------
    # TODO: 이 목록이 REQUIRED_FIELDS 와 어긋나 있다.
    for name in REQUIRED_FIELDS:
        if name not in fields:
            violations.append(
                Violation(
                    rule=f"required_field:{name}",
                    line_no=1,
                    line="---",
                    message=f"필수 항목 {name} 이(가) 없습니다.",
                )
            )

    # --- date 형식 -------------------------------------------------------
    if "date" in fields:
        line_no, line = fields["date"]
        value = line.split(":", 1)[1].strip()
        if not DATE_PATTERN.match(value):
            violations.append(
                Violation(
                    rule="date_format",
                    line_no=line_no,
                    line=line,
                    message="date 는 'YYYY-MM-DD HH:MM:SS +0900' 형식이어야 합니다.",
                )
            )

    # --- 내부 링크 -------------------------------------------------------
    if known_slugs is not None:
        for index, line in enumerate(text.splitlines()[end:], start=end + 1):
            for slug in INTERNAL_LINK_PATTERN.findall(line):
                if slug not in known_slugs:
                    violations.append(
                        Violation(
                            rule="dead_link",
                            line_no=index,
                            line=line.strip(),
                            message=f"'{slug}' 라는 글이 posts/ 에 없습니다.",
                        )
                    )

    return violations


def slugs_in(posts_dir: Path) -> set[str]:
    """posts/ 안의 글 이름 모음. 파일명에서 .md 를 뗀 것이다."""
    return {path.stem for path in posts_dir.glob("*.md")}


def check_dir(posts_dir: Path) -> dict[str, list[Violation]]:
    """폴더 전체를 검사한다. 위반이 없는 글은 빈 목록을 갖는다."""
    known = slugs_in(posts_dir)
    return {
        path.name: check_text(path.read_text(encoding="utf-8"), known)
        for path in sorted(posts_dir.glob("*.md"))
    }
