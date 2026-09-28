from tourism_backend.modules.admin.presentation.difficulty_admin import summarize


def test_levels_and_routes_found_harder() -> None:
    rows = [("a", "Демерджи", 3, "harder")] * 3 + [("a", "Демерджи", 3, "as_expected")] * 2
    rows += [("b", "Дворцы", 1, "as_expected")] * 6 + [("c", "Старое", None, "easier")]
    report = summarize(rows)
    assert report["answers"] == 12
    assert [level["level"] for level in report["levels"]] == [None, 1, 3]
    assert report["levels"][2] == {
        "level": 3,
        "total": 5,
        "easier": 0,
        "as_expected": 2,
        "harder": 3,
    }
    assert [item["name"] for item in report["flagged"]] == ["Демерджи"]


def test_few_answers_are_not_flagged() -> None:
    assert summarize([("a", "Кошка", 2, "harder")] * 4)["flagged"] == []
