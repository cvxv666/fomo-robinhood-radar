"""Every fomo.family link carries the referral, once, and the bot's buy link is the first one."""
from fomo_agent import links
from fomo_agent.bot import token_lines
from fomo_agent.config import settings

MINT = "0x" + "a" * 40


def test_the_referral_rides_on_every_fomo_link(monkeypatch):
    monkeypatch.setattr(settings, "fomo_ref_code", "radar42")
    monkeypatch.setattr(settings, "fomo_ref_param", "ref")
    monkeypatch.setattr(settings, "fomo_ref_url", "")
    assert links.fomo_token(MINT) == f"https://fomo.family/tokens/robinhood/{MINT}?ref=radar42"
    assert links.fomo_profile("unipcs") == "https://fomo.family/profile/unipcs?ref=radar42"
    assert links.fomo_home() == "https://fomo.family/?ref=radar42"
    monkeypatch.setattr(settings, "fomo_ref_url", "https://fomo.family/invite/radar42")
    assert links.fomo_home() == "https://fomo.family/invite/radar42", "an invite link of its own wins"


def test_without_a_code_the_links_are_plain(monkeypatch):
    monkeypatch.setattr(settings, "fomo_ref_code", "")
    monkeypatch.setattr(settings, "fomo_ref_url", "")
    assert links.fomo_token(MINT) == f"https://fomo.family/tokens/robinhood/{MINT}"
    assert links.fomo_home() == "https://fomo.family/"


def test_the_push_offers_the_buy_first(monkeypatch):
    monkeypatch.setattr(settings, "fomo_ref_code", "radar42")
    monkeypatch.setattr(settings, "public_site_url", "https://fomoradar.app")
    addr, line = token_lines(MINT)
    assert addr == f"<code>{MINT}</code>"
    assert line.index("buy on fomo") < line.index("breakdown")
    assert f"/tokens/robinhood/{MINT}?ref=radar42" in line and "fomoradar.app/token/" in line
