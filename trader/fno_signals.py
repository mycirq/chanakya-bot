"""
Enhanced FnO signals: India VIX, Options Chain (PCR/MaxPain/OI walls), FII data.
Sources: yfinance (VIX), Kite Connect (options chain), NSE archives (FII).
All free — no additional subscriptions needed.

Scoring breakdown (100 pts total):
  Technical indicators  : 25 pts
  India VIX             : 20 pts
  Options chain (PCR/OI): 20 pts
  FII positioning       : 15 pts
  (normalized to 100)
"""
import logging
import requests
from datetime import datetime, date, timedelta

from trader.config import IST, KITE_INDICES

logger = logging.getLogger(__name__)


# ── India VIX ──────────────────────────────────────────────────────────────────

def get_india_vix():
    """
    Returns (long_pts, short_pts, vix_value, summary).
    Low VIX = cheap premiums = good to buy. High VIX = expensive = avoid buying.
    Max 20 pts.
    """
    try:
        import yfinance as yf
        hist = yf.Ticker("^INDIAVIX").history(period="5d")
        if hist.empty or len(hist) < 2:
            return 0, 0, None, "VIX: no data"

        vix  = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        trend  = "falling" if vix < prev else "rising"
        change = vix - prev

        # Base score by VIX level — lower = better for buying options
        if vix < 13:
            base = 18
        elif vix < 16:
            base = 14
        elif vix < 20:
            base = 9
        elif vix < 25:
            base = 4
        else:
            base = 0

        long_pts  = base
        short_pts = base

        # Trend bonus: falling VIX favors calls, rising favors puts
        if trend == "falling":
            long_pts  = min(long_pts + 2, 20)
        else:
            short_pts = min(short_pts + 2, 20)

        summary = f"India VIX {vix:.1f} ({trend} {change:+.2f}) — {'cheap premiums' if vix < 16 else 'expensive premiums' if vix > 20 else 'moderate cost'}"
        return long_pts, short_pts, vix, summary
    except Exception as e:
        logger.error(f"get_india_vix failed: {e}")
        return 0, 0, None, "VIX: fetch failed"


# ── Options Chain ───────────────────────────────────────────────────────────────

def get_options_chain_data(underlying: str):
    """
    Fetch live options chain for NIFTY or BANKNIFTY from Kite.
    Returns dict with pcr, max_pain, call_wall, put_wall, current_price, expiry.
    """
    from trader.kite import get_kite, get_nfo_instruments, get_nearest_weekly_expiry, get_ltp

    try:
        cfg = KITE_INDICES.get(underlying)
        if not cfg:
            return None

        expiry     = get_nearest_weekly_expiry(underlying)
        instruments = get_nfo_instruments()
        strike_gap  = cfg["strike_gap"]

        # Current index price
        ltp_data = get_ltp([cfg["quote"]])
        current_price = ltp_data.get(cfg["quote"])
        if not current_price:
            return None

        atm     = round(current_price / strike_gap) * strike_gap
        strikes = {atm + i * strike_gap for i in range(-12, 13)}

        # Collect instrument keys for this expiry + strike range
        quote_keys = []
        inst_map   = {}
        for inst in instruments:
            inst_expiry = inst.get("expiry")
            if isinstance(inst_expiry, datetime):
                inst_expiry = inst_expiry.date()
            elif isinstance(inst_expiry, str):
                try:
                    inst_expiry = date.fromisoformat(inst_expiry)
                except ValueError:
                    continue
            if (inst.get("name") == underlying
                    and inst_expiry == expiry
                    and float(inst.get("strike", 0)) in strikes
                    and inst.get("instrument_type") in ("CE", "PE")):
                key = f"NFO:{inst['tradingsymbol']}"
                quote_keys.append(key)
                inst_map[key] = inst

        if not quote_keys:
            logger.warning(f"No options found for {underlying} expiry {expiry}")
            return None

        # Fetch quotes (Kite allows 500/call)
        kite   = get_kite()
        quotes = {}
        for i in range(0, len(quote_keys), 500):
            try:
                quotes.update(kite.quote(quote_keys[i:i+500]))
            except Exception as e:
                logger.error(f"Options quote batch failed: {e}")

        # Build OI maps
        call_oi = {}
        put_oi  = {}
        for key, inst in inst_map.items():
            if key not in quotes:
                continue
            strike = float(inst["strike"])
            oi     = quotes[key].get("oi", 0) or 0
            if inst["instrument_type"] == "CE":
                call_oi[strike] = call_oi.get(strike, 0) + oi
            else:
                put_oi[strike]  = put_oi.get(strike, 0) + oi

        if not call_oi or not put_oi:
            return None

        total_call_oi = sum(call_oi.values())
        total_put_oi  = sum(put_oi.values())
        pcr = total_put_oi / total_call_oi if total_call_oi > 0 else 1.0

        all_strikes = sorted(set(list(call_oi.keys()) + list(put_oi.keys())))
        max_pain    = _calc_max_pain(all_strikes, call_oi, put_oi)
        call_wall   = max(call_oi, key=call_oi.get) if call_oi else None
        put_wall    = max(put_oi,  key=put_oi.get)  if put_oi  else None

        return {
            "current_price": current_price,
            "pcr":           round(pcr, 2),
            "max_pain":      max_pain,
            "call_wall":     call_wall,
            "put_wall":      put_wall,
            "total_call_oi": total_call_oi,
            "total_put_oi":  total_put_oi,
            "call_oi":       call_oi,
            "put_oi":        put_oi,
            "expiry":        expiry,
            "atm":           atm,
        }
    except Exception as e:
        logger.error(f"get_options_chain_data failed for {underlying}: {e}")
        return None


def _calc_max_pain(strikes, call_oi, put_oi):
    """Strike where total option buyer loss is maximum (sellers win most)."""
    min_loss      = float("inf")
    max_pain      = strikes[len(strikes) // 2]
    for test in strikes:
        loss = sum((test - s) * oi for s, oi in call_oi.items() if test > s) + \
               sum((s - test) * oi for s, oi in put_oi.items()  if test < s)
        if loss < min_loss:
            min_loss  = loss
            max_pain  = test
    return max_pain


def score_options_chain(chain):
    """
    Score PCR, max pain, OI walls. Returns (long_pts, short_pts, summary).
    Max 20 pts.
    """
    if not chain:
        return 0, 0, "Options chain: no data"

    pcr   = chain["pcr"]
    mp    = chain["max_pain"]
    cw    = chain["call_wall"]
    pw    = chain["put_wall"]
    price = chain["current_price"]
    reasons = []
    long_pts = short_pts = 0

    # PCR (0–8 pts)
    if pcr > 1.3:
        long_pts += 8;  reasons.append(f"PCR {pcr:.2f} — bullish (heavy put writing)")
    elif pcr > 1.1:
        long_pts += 5;  reasons.append(f"PCR {pcr:.2f} — mildly bullish")
    elif pcr < 0.7:
        short_pts += 8; reasons.append(f"PCR {pcr:.2f} — bearish (heavy call writing)")
    elif pcr < 0.9:
        short_pts += 5; reasons.append(f"PCR {pcr:.2f} — mildly bearish")
    else:
        reasons.append(f"PCR {pcr:.2f} — neutral")

    # Max pain vs price (0–6 pts)
    if price > mp * 1.005:
        short_pts += 6; reasons.append(f"Above max pain {mp:.0f} → gravitates down")
    elif price < mp * 0.995:
        long_pts  += 6; reasons.append(f"Below max pain {mp:.0f} → gravitates up")
    else:
        reasons.append(f"At max pain {mp:.0f}")

    # OI walls — price position in range (0–6 pts)
    if cw and pw and cw > pw:
        rng  = cw - pw
        pos  = (price - pw) / rng
        if pos < 0.25:
            long_pts  += 6; reasons.append(f"Near put wall {pw:.0f} (support)")
        elif pos > 0.75:
            short_pts += 6; reasons.append(f"Near call wall {cw:.0f} (resistance)")
        else:
            reasons.append(f"OI range {pw:.0f}–{cw:.0f} (mid)")

    return min(long_pts, 20), min(short_pts, 20), " | ".join(reasons)


# ── FII Data ────────────────────────────────────────────────────────────────────

def get_fii_data():
    """
    Fetch FII participant-wise OI from NSE archives.
    Returns (net_contracts, summary_str).
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer":    "https://www.nseindia.com",
        "Accept":     "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    today = datetime.now(IST).date()
    for offset in range(5):
        check = today - timedelta(days=offset)
        if check.weekday() >= 5:
            continue
        date_str = check.strftime("%d%m%Y")
        url = f"https://archives.nseindia.com/content/nsccl/fao_participant_oi_{date_str}.csv"
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                continue
            for line in resp.text.strip().split("\n"):
                if "FII" in line.upper() or "FOREIGN" in line.upper():
                    parts = [p.strip().replace(",", "") for p in line.split(",")]
                    try:
                        fut_long  = float(parts[1])
                        fut_short = float(parts[2])
                        net = fut_long - fut_short
                        bias = "bullish" if net > 0 else "bearish"
                        return net, f"FII net futures {net:+,.0f} contracts — {bias} (as of {check})"
                    except (ValueError, IndexError):
                        continue
        except Exception as e:
            logger.warning(f"FII fetch failed for {date_str}: {e}")
    return None, "FII data: unavailable"


def score_fii(net_fii):
    """Returns (long_pts, short_pts). Max 15 pts."""
    if net_fii is None:
        return 0, 0
    if net_fii > 50000:
        return 15, 0
    elif net_fii > 10000:
        return 10, 0
    elif net_fii > 0:
        return 5, 0
    elif net_fii > -10000:
        return 0, 5
    elif net_fii > -50000:
        return 0, 10
    else:
        return 0, 15


# ── Full Analysis ───────────────────────────────────────────────────────────────

def get_full_fno_analysis(underlying: str) -> dict:
    """
    Complete FnO analysis combining all signals.
    Returns a rich dict for Slack reporting and signal scoring.
    """
    from trader.kite import get_ohlcv, get_ltp
    from trader.strategy import compute_indicators, score_signal

    cfg = KITE_INDICES.get(underlying)
    if not cfg:
        return {"error": f"Unknown underlying: {underlying}"}

    now = datetime.now(IST)
    result = {
        "underlying": underlying,
        "timestamp":  now.strftime("%d %b %Y, %H:%M IST"),
    }

    # 1. Technical (max 25 pts)
    ohlcv = get_ohlcv(cfg["token"], interval="5minute", days=7)
    df    = compute_indicators(ohlcv) if ohlcv else None
    raw_score, tech_dir, tech_reason = score_signal(df)
    tech_long  = int(raw_score * 0.25) if tech_dir == "long"  else 0
    tech_short = int(raw_score * 0.25) if tech_dir == "short" else 0
    result["technical"] = {
        "raw_score": raw_score,
        "direction": tech_dir,
        "reason":    tech_reason,
        "long_pts":  tech_long,
        "short_pts": tech_short,
    }

    # 2. VIX (max 20 pts)
    vl, vs, vix_val, vix_sum = get_india_vix()
    result["vix"] = {
        "value":      vix_val,
        "long_pts":   vl,
        "short_pts":  vs,
        "summary":    vix_sum,
    }

    # 3. Options chain (max 20 pts)
    chain      = get_options_chain_data(underlying)
    ol, os_, oi_sum = score_options_chain(chain)
    result["options_chain"] = {
        "data":       chain,
        "long_pts":   ol,
        "short_pts":  os_,
        "summary":    oi_sum,
    }

    # 4. FII (max 15 pts)
    net_fii, fii_sum = get_fii_data()
    fl, fs = score_fii(net_fii)
    result["fii"] = {
        "net":        net_fii,
        "long_pts":   fl,
        "short_pts":  fs,
        "summary":    fii_sum,
    }

    # Totals — normalize against actually available points
    total_long  = tech_long  + vl + ol + fl
    total_short = tech_short + vs + os_ + fs

    # Only count components that had data
    max_possible = 0
    if tech_dir is not None:
        max_possible += 25
    if vix_val is not None:
        max_possible += 20
    if chain is not None:
        max_possible += 20
    if net_fii is not None:
        max_possible += 15
    max_possible = max(max_possible, 1)  # avoid division by zero

    if total_long > total_short:
        direction   = "long"
        final_score = min(int(total_long / max_possible * 100), 100)
    elif total_short > total_long:
        direction   = "short"
        final_score = min(int(total_short / max_possible * 100), 100)
    else:
        direction   = None
        final_score = 0

    result["direction"]   = direction
    result["final_score"] = final_score
    result["total_long"]  = total_long
    result["total_short"] = total_short

    # Current index price + ATM
    if chain:
        result["current_price"] = chain["current_price"]
        result["atm"]           = chain["atm"]
        result["expiry"]        = chain["expiry"]
    else:
        ltp_data = get_ltp([cfg["quote"]])
        result["current_price"] = ltp_data.get(cfg["quote"])
        result["atm"]           = None
        result["expiry"]        = None

    # Best entry window recommendation
    hour = now.hour
    if 9 <= hour < 10:
        entry_window = "9:15–10:00 AM — opening momentum (good)"
    elif 10 <= hour < 11:
        entry_window = "10:00–11:00 AM — trend confirmation (best)"
    elif 11 <= hour < 13:
        entry_window = "11:00–1:00 PM — mid-session (selective)"
    elif 13 <= hour < 14:
        entry_window = "1:00–2:00 PM — post-lunch drift (avoid)"
    elif 14 <= hour < 15:
        entry_window = "2:00–3:00 PM — afternoon momentum (good)"
    else:
        entry_window = "After 3:00 PM — avoid new positions"

    result["entry_window"] = entry_window
    return result
