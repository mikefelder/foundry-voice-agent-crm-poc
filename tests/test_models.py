from datetime import date, datetime

import pytest

from crm_companion.crm.models import (
    Opportunity,
    StageResolution,
    UserRef,
    UserResolution,
    WriteOutcome,
    WriteResult,
)

TODAY = date(2026, 8, 21)


def _opp(**overrides) -> Opportunity:
    base = {
        "id": "006A000001abcDE",
        "account_id": "001A000001abcDE",
        "name": "Northgate Commons Phase 2",
        "stage": "Bidding",
        "close_date": date(2026, 4, 30),
        "created_date": datetime(2025, 3, 11, 9, 0),
        "is_closed": False,
    }
    return Opportunity(**{**base, **overrides})


class TestPastDue:
    def test_open_with_elapsed_close_date_is_past_due(self):
        assert _opp().is_past_due(TODAY) is True

    def test_future_close_date_is_not_past_due(self):
        assert _opp(close_date=date(2026, 12, 1)).is_past_due(TODAY) is False

    def test_closed_is_never_past_due(self):
        assert _opp(is_closed=True).is_past_due(TODAY) is False

    def test_missing_close_date_is_not_past_due(self):
        assert _opp(close_date=None).is_past_due(TODAY) is False

    def test_close_date_today_is_not_yet_past_due(self):
        assert _opp(close_date=TODAY).is_past_due(TODAY) is False


class TestUserResolution:
    def test_single_match_is_unique(self):
        res = UserResolution(query="Dana", matches=(UserRef(id="005A000001abcDE", name="Dana W"),))
        assert res.is_unique
        assert not res.is_ambiguous
        assert not res.is_unresolved
        assert res.only.name == "Dana W"

    def test_multiple_matches_are_ambiguous_and_refuse_to_pick(self):
        res = UserResolution(
            query="Dana",
            matches=(
                UserRef(id="005A000001abcDE", name="Dana W"),
                UserRef(id="005A000001abcDF", name="Dana X"),
            ),
        )
        assert res.is_ambiguous
        with pytest.raises(ValueError, match="expected exactly 1"):
            _ = res.only

    def test_no_matches_is_unresolved(self):
        res = UserResolution(query="Nobody")
        assert res.is_unresolved
        with pytest.raises(ValueError, match="expected exactly 1"):
            _ = res.only


class TestStageResolution:
    def test_unique_match_resolves(self):
        res = StageResolution(spoken="proposal", matches=("Proposal/Price Quote",))
        assert res.is_unique
        assert res.only == "Proposal/Price Quote"

    def test_ambiguous_match_refuses_to_pick(self):
        res = StageResolution(spoken="neg", matches=("Negotiation/Review", "Negotiating"))
        assert res.is_ambiguous
        with pytest.raises(ValueError, match="expected exactly 1"):
            _ = res.only


class TestWriteResult:
    def test_created_is_new(self):
        assert WriteResult(outcome=WriteOutcome.CREATED, record_id="00TA00001").is_new

    def test_replayed_is_not_new(self):
        assert not WriteResult(outcome=WriteOutcome.REPLAYED, record_id="00TA00001").is_new

    def test_updated_is_not_new(self):
        assert not WriteResult(outcome=WriteOutcome.UPDATED, record_id="006A00001").is_new


class TestModelStrictness:
    def test_unknown_fields_are_rejected(self):
        with pytest.raises(ValueError, match="extra"):
            _opp(unexpected_field="boom")

    def test_models_are_immutable(self):
        opp = _opp()
        with pytest.raises(ValueError, match="frozen"):
            opp.stage = "Closed Won"
