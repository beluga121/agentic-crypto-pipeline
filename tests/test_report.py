"""Tests for HTML rendering, including escaping of hostile API data."""

import re
from pathlib import Path

import pytest

from pipeline.tools import report as r
from pipeline.tools import transform as t

FIXTURE = Path(__file__).parent / "fixtures" / "sample_markets.json"
RUN_TS = "20261117T153000Z"


def render(raw_df):
    """Run the transforms on a raw DataFrame and render the page."""
    coins = t.add_derived(t.filter_stablecoins(t.clean(raw_df)))
    context = r.build_context(t.summarize_by_tier(coins), t.top_movers(coins), t.compute_kpis(coins, 3), RUN_TS)
    return r.render_html(context)


@pytest.fixture
def html():
    return render(t.load_raw(FIXTURE))


def test_report_has_required_sections(html):
    for text in (
        "Total market cap",
        "24h volume",
        "Coins up / down",
        "Weighted 24h change",
        "Market-cap share by tier",
        "Tier summary",
        "Top 10 gainers",
        "Top 10 losers",
        "2026-11-17 15:30 UTC",
    ):
        assert text in html
    assert 'Data provided by <a href="https://www.coingecko.com/"' in html


def test_report_is_self_contained(html):
    assert "<script" not in html.lower()
    assert "Content-Security-Policy" in html and "default-src 'none'" in html
    assert 'name="viewport"' in html
    # The only external URL is the CoinGecko attribution link (a plain link, not a resource).
    urls = set(re.findall(r"https?://[^\s\"'<>]+", html))
    assert urls == {"https://www.coingecko.com/"}
    assert not re.search(r"<(link|img|iframe|object|embed)\b", html, re.IGNORECASE)


def test_signs_do_not_rely_on_color(html):
    assert "+12.00%" in html  # Small Coin gain
    assert "−9.10%" in html  # Tiny Coin loss uses a real minus sign


def test_malicious_coin_name_is_escaped():
    raw = t.load_raw(FIXTURE)
    payload = "<script>alert(1)</script>"
    raw.loc[raw["id"] == "smallcoin", "name"] = payload  # the top gainer
    raw.loc[raw["id"] == "smallcoin", "symbol"] = '"><img src=x onerror=alert(1)>'
    html = render(raw)
    assert payload not in html
    assert "<img" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize(
    "value, expected",
    [(2.31e12, "$2.31T"), (4.567e11, "$456.7B"), (1.23e7, "$12.3M"), (12345, "$12,345"), (None, "n/a")],
)
def test_fmt_usd(value, expected):
    assert r.fmt_usd(value) == expected


@pytest.mark.parametrize(
    "value, expected", [(1.234, "+1.23%"), (-4.5, "−4.50%"), (0.0, "0.00%"), (float("nan"), "n/a")]
)
def test_fmt_pct(value, expected):
    assert r.fmt_pct(value) == expected


def test_bar_rows_scale_and_position():
    rows = r.bar_rows([{"magnitude": 5.0}, {"magnitude": 10.0}, {"magnitude": 0.0}], 10.0)
    assert [row["width_pct"] for row in rows] == [50.0, 100.0, 0.5]
    assert [row["y"] for row in rows] == [0, r.ROW_HEIGHT, 2 * r.ROW_HEIGHT]
