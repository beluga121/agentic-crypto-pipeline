"""Unit tests for the pandas transformations. No network, LLM or GCS calls."""

import json
from pathlib import Path

import pytest

from pipeline.tools import transform as t
from pipeline.tools.ingest import validate_markets

FIXTURE = Path(__file__).parent / "fixtures" / "sample_markets.json"


@pytest.fixture
def cleaned():
    return t.clean(t.load_raw(FIXTURE))


@pytest.fixture
def coins(cleaned):
    return t.add_derived(t.filter_stablecoins(cleaned))


def test_validate_markets_accepts_fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(validate_markets(data)) == 15


@pytest.mark.parametrize("bad", [[], {}, "text", [{"id": "x"}], [1, 2]])
def test_validate_markets_rejects_bad_payloads(bad):
    with pytest.raises(ValueError):
        validate_markets(bad)


def test_clean_drops_unusable_rows_and_coerces_types(cleaned):
    # no-price (null), zero-cap (0) and bad-number ("not-a-number") are dropped.
    assert set(cleaned["id"]) & {"no-price", "zero-cap", "bad-number"} == set()
    assert len(cleaned) == 12
    assert cleaned["price_usd"].dtype.kind == "f"
    assert cleaned.loc[cleaned["id"] == "bitcoin", "symbol"].item() == "BTC"


def test_filter_removes_known_and_pegged_stablecoins(cleaned):
    kept = t.filter_stablecoins(cleaned)
    removed = set(cleaned["id"]) - set(kept["id"])
    # tether and usd-coin are on the list; new-dollar is caught by the peg rule.
    assert removed == {"tether", "usd-coin", "new-dollar"}
    # XRP has 0% change but trades far from $1, so it is not a stablecoin.
    assert "ripple" in set(kept["id"])


@pytest.mark.parametrize(
    "cap, tier",
    [(100e9, "Mega"), (99.9e9, "Large"), (10e9, "Large"), (1e9, "Mid"), (999e6, "Small"), (1.0, "Small")],
)
def test_market_cap_tier_boundaries(cap, tier):
    assert t.market_cap_tier(cap) == tier


def test_add_derived_ratio(coins):
    btc = coins[coins["id"] == "bitcoin"].iloc[0]
    assert btc["volume_to_mcap_ratio"] == pytest.approx(3.0e10 / 1.30e12, abs=1e-4)
    assert btc["market_cap_tier"] == "Mega"


def test_summarize_by_tier(coins):
    tiers = t.summarize_by_tier(coins)
    assert list(tiers["tier"]) == ["Mega", "Large", "Mid", "Small"]
    assert list(tiers["coin_count"]) == [2, 3, 2, 2]
    assert tiers["market_cap_share_pct"].sum() == pytest.approx(100, abs=0.05)
    large = tiers[tiers["tier"] == "Large"].iloc[0]
    assert large["median_change_24h_pct"] == pytest.approx(0.0)  # median of 2.5, -3.2, 0.0


def test_top_movers_split_by_sign(coins):
    movers = t.top_movers(coins, n=10)
    gainers = movers[movers["direction"] == "gainer"]
    losers = movers[movers["direction"] == "loser"]
    assert list(gainers["id"]) == ["smallcoin", "chainlink", "bnb", "bitcoin"]
    assert list(losers["id"]) == ["tinycoin", "aave", "solana", "ethereum"]
    assert (gainers["change_24h_pct"] > 0).all() and (losers["change_24h_pct"] < 0).all()
    assert list(gainers["position"]) == [1, 2, 3, 4]


def test_top_movers_respects_n(coins):
    movers = t.top_movers(coins, n=2)
    assert len(movers) == 4


def test_compute_kpis(coins):
    kpis = t.compute_kpis(coins, stablecoins_excluded=3)
    assert kpis["coin_count"] == 9
    assert kpis["coins_up"] == 4 and kpis["coins_down"] == 4
    expected = (coins["market_cap_usd"] * coins["change_24h_pct"]).sum() / coins["market_cap_usd"].sum()
    assert kpis["weighted_change_24h_pct"] == pytest.approx(expected, abs=1e-3)
    assert kpis["stablecoins_excluded"] == 3


def test_run_transform_writes_all_outputs(tmp_path):
    result = t.run_transform(FIXTURE, out_dir=tmp_path)
    for name in ("coins_clean.csv", "tier_summary.csv", "movers.csv", "kpis.json"):
        assert (tmp_path / name).is_file()
    assert result["kpis"]["stablecoins_excluded"] == 3
