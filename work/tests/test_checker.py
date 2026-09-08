"""checker.py 가 규칙을 제대로 잡는지 본다.

이 테스트는 지금 하나가 실패한다. checker.py 가 필수 항목 하나를
빠뜨리고 있기 때문이다. 하네스가 그것을 고치면 통과한다. (ACCEPTANCE A03)
"""

from __future__ import annotations

import sys
from pathlib import Path

# work/ 를 import 경로에 넣는다. 테스트는 work/tests 에서 돈다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checker import check_dir, check_text, slugs_in  # noqa: E402

POSTS = Path(__file__).resolve().parent.parent / "posts"


def rules_of(violations) -> set[str]:
    return {violation.rule for violation in violations}


def test_normal_post_has_no_violation() -> None:
    """정상 글에서는 아무것도 찾지 않아야 한다.

    없는 문제를 지어내면 여기서 걸린다.
    """
    text = (POSTS / "2026-01-01-ok.md").read_text(encoding="utf-8")
    assert check_text(text, slugs_in(POSTS)) == []


def test_missing_description_is_reported() -> None:
    """description 이 없으면 잡아야 한다.

    지금은 실패한다. checker.py 의 필수 항목 목록에 description 이 빠져 있다.
    """
    text = (POSTS / "2026-01-02-no-desc.md").read_text(encoding="utf-8")
    assert "required_field:description" in rules_of(check_text(text))


def test_date_without_timezone_is_reported() -> None:
    text = (POSTS / "2026-01-03-bad-date.md").read_text(encoding="utf-8")
    assert "date_format" in rules_of(check_text(text))


def test_dead_link_is_reported() -> None:
    text = (POSTS / "2026-01-04-dead-link.md").read_text(encoding="utf-8")
    assert "dead_link" in rules_of(check_text(text, slugs_in(POSTS)))


def test_live_link_is_not_reported() -> None:
    """있는 글로 가는 링크는 잡지 않아야 한다."""
    text = (POSTS / "2026-01-01-ok.md").read_text(encoding="utf-8")
    assert "dead_link" not in rules_of(check_text(text, slugs_in(POSTS)))


def test_violation_carries_rule_and_source_line() -> None:
    """위반에는 규칙 이름과 원문 줄이 함께 담긴다."""
    text = (POSTS / "2026-01-03-bad-date.md").read_text(encoding="utf-8")
    violation = next(v for v in check_text(text) if v.rule == "date_format")
    assert "date:" in violation.line
    assert violation.line_no > 1


def test_check_dir_covers_every_post() -> None:
    result = check_dir(POSTS)
    assert len(result) == 4
    assert result["2026-01-01-ok.md"] == []
