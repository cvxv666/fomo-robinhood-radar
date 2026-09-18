"""Every link to fomo.family, in one place, with the referral on it.

A push names a token; the reader's next move is to open it on fomo and buy. That link, the
trader's profile link and the plain "join fomo" link all go through here so the referral code is on
each of them exactly once, and so the day fomo changes a path there is one line to change.
"""
from __future__ import annotations

from urllib.parse import quote

from .config import settings

FOMO = "https://fomo.family"


def _ref(url: str) -> str:
    code = settings.fomo_ref_code
    if not code:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{quote(settings.fomo_ref_param)}={quote(code)}"


def fomo_home() -> str:
    """The invite link itself, for the help and the site footer."""
    return settings.fomo_ref_url or _ref(FOMO + "/")


def fomo_token(mint: str, chain: str = "robinhood") -> str:
    return _ref(f"{FOMO}/tokens/{chain}/{mint}")


def fomo_profile(handle: str) -> str:
    return _ref(f"{FOMO}/profile/{quote(handle)}")


def site_token(mint: str) -> str | None:
    return f"{settings.public_site_url}/token/{mint}" if settings.public_site_url else None


def site_trader(who: str) -> str | None:
    return f"{settings.public_site_url}/trader/{quote(who)}" if settings.public_site_url else None


def token_links(mint: str, chain: str = "robinhood") -> dict:
    """What an API consumer gets beside a token: where to read it and where to buy it."""
    return {"fomo": fomo_token(mint, chain), "site": site_token(mint)}
