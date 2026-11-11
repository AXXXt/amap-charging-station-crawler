"""Read-only shared tariff cache. Failure always falls back to full collection."""
import json
import math
import re
from datetime import datetime, timezone, timedelta

BEIJING = timezone(timedelta(hours=8))
RECHECK_SECONDS = 6 * 3600


def seconds(value):
    try:
        number = float(value)
        return int(number / 1000 if number > 1e11 else number) if math.isfinite(number) and number > 0 else 0
    except (TypeError, ValueError):
        return 0


def price_number(value):
    text = re.sub(r"\s*元\s*[/／]\s*(?:度|kWh)$", "", str(value if value is not None else "").strip(), flags=re.I)
    return float(text) if re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)", text) else None


def complete_periods(periods):
    if not isinstance(periods, list) or not periods:
        return False
    ranges = set()
    for row in periods:
        if not isinstance(row, dict):
            return False
        if str(row.get("time") or "").strip() == "当前时段":
            continue
        match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*[-–—~～至]\s*(\d{1,2}):(\d{2})", str(row.get("time") or "").replace("：", ":").strip())
        if not match:
            return False
        h1, m1, h2, m2 = map(int, match.groups())
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if m1 >= 60 or m2 >= 60 or start >= 1440 or end > 1440 or start == end:
            return False
        if end == 1439:
            end = 1440
        prices = tuple(price_number(row.get(k)) for k in ("elecFee", "serviceFee", "totalPrice"))
        if any(p is None or not math.isfinite(p) for p in prices) or abs(prices[0] + prices[1] - prices[2]) > .015:
            return False
        if start < end:
            ranges.add((start, end, prices))
        else:
            ranges.add((start, 1440, prices))
            if end:
                ranges.add((0, end, prices))
    end = 0
    for start, stop, _ in sorted(ranges):
        if start != end:
            return False
        end = stop
    return end == 1440


def cached_tariff(rows, platform, now):
    """Only trust the latest observation from this platform with explicit provenance."""
    for row in rows:
        price = row.get("price_json") or {}
        if isinstance(price, (str, bytes)):
            try:
                price = json.loads(price)
            except (TypeError, ValueError):
                continue
        if not isinstance(price, dict) or price.get("priceSchedulePlatform") != platform:
            continue
        captured = seconds(price.get("priceScheduleCapturedAt"))
        observed = seconds(row.get("captured_at"))
        if not captured or captured > observed + 60 or captured > now or now - captured >= RECHECK_SECONDS:
            return None
        if datetime.fromtimestamp(captured, BEIJING).date() != datetime.fromtimestamp(now, BEIJING).date():
            return None
        fast, slow = price.get("fastPrices") or [], price.get("slowPrices") or []
        if not (fast or slow) or any(not complete_periods(p) for p in (fast, slow) if p):
            return None
        return {"capturedAt": captured * 1000, "platform": platform, "fastPrices": fast, "slowPrices": slow}
    return None
