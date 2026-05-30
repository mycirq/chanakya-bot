"""Standalone Kite session for backtest fetcher.

Doesn't depend on the bot's DB or app context. Reads creds from env:
  KITE_API_KEY       (required)
  KITE_ACCESS_TOKEN  (required — get from `bin/get_kite_token.sh` or Kite login)
  PROXY_URL          (optional — required for prod since Kite is IP-whitelisted
                      to the Oracle VM; not needed if you ssh-tunnel from the VM)
"""
import os
import logging
from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)

_kite: KiteConnect | None = None


def get_kite() -> KiteConnect:
    global _kite
    if _kite is None:
        api_key = os.environ["KITE_API_KEY"]
        token   = os.environ["KITE_ACCESS_TOKEN"]
        _kite = KiteConnect(api_key=api_key)
        proxy = os.environ.get("PROXY_URL")
        if proxy:
            _kite.reqsession.proxies.update({"http": proxy, "https": proxy})
        _kite.set_access_token(token)
        # Sanity check the session early — Kite raises TokenException on bad token
        try:
            _kite.profile()
        except Exception as e:
            raise RuntimeError(
                f"Kite session failed: {type(e).__name__}: {e}\n"
                "Check KITE_ACCESS_TOKEN is valid for today and PROXY_URL is reachable."
            ) from e
    return _kite
