"""Read-only, content-free inspection of one provider receipt for operators."""
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import os
import re
import time
import urllib.parse

from .official_pricing import official_2x_charge, official_v1_charge
from .wopally_client import WopallyClient, WopallyUnavailable

REQUEST_ID = r"^(?:o2x1_|o90v1_|m12_)?[a-f0-9]{32}$"
NUMERIC_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "image_output_tokens",
    "image_input_tokens", "image_count", "input_cost", "output_cost",
    "cache_read_cost", "total_cost", "actual_cost",
)
PAGE_SIZE = 1000
MAX_PAGES = 5
SCAN_BUDGET_SECONDS = 20


class UsageOnlyClient(WopallyClient):
    """Only read usage; unlike redemption, does not depend on card endpoints."""

    @property
    def is_configured(self):
        url = urllib.parse.urlparse(self.base_url)
        has_login = bool(self.access_token or (
            os.environ.get("WOPALLY_SERVICE_EMAIL", "").strip()
            and os.environ.get("WOPALLY_SERVICE_PASSWORD", "")
        ))
        return has_login and url.scheme == "https" and bool(url.hostname)

    def _request(self, path, method="GET", body=None, *, retried_login=False):
        if method != "GET" or body is not None or not re.fullmatch(
            r"/api/v1/usage\?page=[1-5]&page_size=1000&api_key_id=[1-9][0-9]*"
            r"&sort_by=id&sort_order=asc", path
        ):
            raise ValueError("Only bounded read-only usage requests are allowed")
        return super()._request(path, method, body, retried_login=retried_login)


def inspect_receipt(request_id: str, api_key_id: int, client=None, *, max_pages=MAX_PAGES):
    """Never invokes inference, changes balances, or returns prompts/credentials."""
    if (not isinstance(request_id, str) or not re.fullmatch(REQUEST_ID, request_id)
            or type(api_key_id) is not int or api_key_id <= 0):
        raise ValueError("Invalid receipt lookup")
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES:
        raise ValueError("Invalid receipt scan limit")
    client = client or UsageOnlyClient(timeout=3)
    started = time.monotonic()
    pages_scanned = 0

    def incomplete():
        return {"status": "incomplete_scan", "pages_scanned": pages_scanned}

    def read_page(page, expected_total=None):
        nonlocal pages_scanned
        if time.monotonic() - started >= SCAN_BUDGET_SECONDS:
            return None
        data = client._request(
            f"/api/v1/usage?page={page}&page_size={PAGE_SIZE}&api_key_id={api_key_id}"
            "&sort_by=id&sort_order=asc"
        )
        pages_scanned += 1
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise WopallyUnavailable("中转站用量格式无法识别")
        total, size, actual_page = data.get("total"), data.get("page_size"), data.get("page")
        if (type(total) is not int or total < 0 or total > PAGE_SIZE * max_pages
                or expected_total is not None and total != expected_total
                or type(size) is not int or size != PAGE_SIZE
                or type(actual_page) is not int or actual_page != page
                or len(data["items"]) != min(PAGE_SIZE, max(total - (page - 1) * PAGE_SIZE, 0))):
            return None
        return data

    first = read_page(1)
    if first is None:
        return incomplete()
    total = first["total"]
    page_count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    rows = list(first["items"])
    for page in range(2, page_count + 1):
        data = read_page(page, total)
        if data is None:
            return incomplete()
        rows.extend(data["items"])

    ids = [str(row.get("id") or "") if isinstance(row, dict) and not isinstance(row.get("id"), bool) else ""
           for row in rows]
    if any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value) for value in ids) or len(set(ids)) != len(ids):
        return incomplete()
    if page_count > 1:
        # Multi-page offset scans are not a database snapshot. Read the whole
        # ordered result a second time, and defer if rows, ownership, markers,
        # or any price-relevant field changed. Silently ignored sorting also
        # fails closed. New or removed receipts must never be guessed away.
        if any(not value.isdecimal() or int(value) <= 0 for value in ids):
            return incomplete()
        numeric_ids = [int(value) for value in ids]
        if any(a >= b for a, b in zip(numeric_ids, numeric_ids[1:])):
            return incomplete()
        fields = ("id", "user_agent", "api_key_id", "model", *NUMERIC_FIELDS)

        def projection(row):
            return tuple((key in row, row.get(key)) for key in fields)

        for page in range(1, page_count + 1):
            data = read_page(page, total)
            expected = rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
            if (data is None or any(not isinstance(row, dict) for row in data["items"])
                    or [projection(row) for row in data["items"]] != [projection(row) for row in expected]):
                return incomplete()
    matches = [row for row in rows if isinstance(row, dict)
               and row.get("user_agent") == f"StardustWorkbench/{request_id}"]
    if not matches:
        return {"status": "receipt_not_found", "pages_scanned": pages_scanned}
    if len(matches) != 1:
        return {"status": "ambiguous_receipts", "match_count": len(matches)}
    row = matches[0]
    if type(row.get("api_key_id")) is not int or row["api_key_id"] != api_key_id:
        return {"status": "ownership_mismatch"}
    model = row.get("model")
    if model not in {"deepseek-v4-flash", "gpt-image-2"}:
        return {"status": "unsupported_model"}
    # Whitelist numeric fields only. Never return the arbitrary provider object.
    sample = {}
    for key in NUMERIC_FIELDS:
        if key not in row:
            continue
        value = row[key]
        try:
            number = Decimal(str(value))
            if not isinstance(value, bool) and number.is_finite() and 0 <= number <= 10**15:
                sample[key] = str(number)
        except (ValueError, InvalidOperation):
            pass
    result = {"model": model, "numeric_usage": sample}
    try:
        if request_id.startswith("o2x1_"):
            charge = official_2x_charge(row, model)
        elif request_id.startswith("o90v1_"):
            charge = official_v1_charge(row, model)
        else:
            cost = Decimal(str(row["actual_cost"]))
            if not cost.is_finite() or cost < 0:
                raise ValueError("invalid historical cost")
            multiplier = 12 if request_id.startswith("m12_") else 3
            charge = int((cost * multiplier * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return {**result, "status": "invalid_receipt_pricing"}
    provider_id = str(row.get("id") or row.get("request_id") or "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", provider_id):
        return {**result, "status": "invalid_provider_id"}
    return {**result, "status": "confirmed", "actual_micro_cny": charge,
            "provider_request_id": provider_id}
