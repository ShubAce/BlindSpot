"""One figure, one value on every page: the headline's scar totals round as /api/scars/summary does."""

from __future__ import annotations

from typing import Any

import pytest

from blindspot.analytics import headline


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar(self) -> Any:
        return self.value

    def mappings(self) -> _Result:
        return self

    def one(self) -> Any:
        return self.value


class _Session:
    """Answers unseen_burns' two queries: the table exists, then the season's sums."""

    def __init__(self, sums: dict[str, Any]) -> None:
        self.results = iter([_Result(True), _Result(sums)])

    def execute(self, *_args: Any, **_kwargs: Any) -> _Result:
        return next(self.results)


def test_headline_scar_areas_keep_two_decimals(monkeypatch: pytest.MonkeyPatch) -> None:
    # 911,372.48 ha: rounded to 0.1 first it became 911,372.5, which a page rounds to 9,11,373 while
    # GET /api/scars/summary (0.01) gives 911,372.48 and the live map 9,11,372.
    monkeypatch.setattr(headline, "latest_refs", lambda *_a, **_k: [])
    sums = {"clusters": 455825, "area_ha": 911372.4812, "unseen_clusters": 369194, "unseen_area_ha": 669500.8449,
            "checked_area_ha": 911372.4812, "first_pass": None, "last_pass": None, "districts": 46,
            "versions": [headline.RULE.version]}
    block, _ = headline.unseen_burns(_Session(sums))  # type: ignore[arg-type]
    assert block["area_ha"] == 911372.48 and block["checked_area_ha"] == 911372.48
    assert block["unseen_area_ha"] == 669500.84
    assert block["unseen_share"] == round(669500.8449 / 911372.4812, 4)
