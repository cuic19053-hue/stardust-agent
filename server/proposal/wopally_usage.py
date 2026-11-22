from __future__ import annotations

from .billing_diagnostics import UsageOnlyClient, inspect_receipt
from .wopally_client import WopallyUnavailable


class UsageUnavailable(RuntimeError):
    """The provider's authoritative charge cannot currently be confirmed."""


class WopallyUsage:
    """Settle only after the same complete, unique receipt check as the worker."""

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 *, timeout: float = 8, max_pages: int = 1):
        self.client = UsageOnlyClient(base_url=base_url, access_token=token, timeout=timeout)
        self.timeout = timeout
        self.max_pages = max(1, min(int(max_pages), 5))

    def find(self, request_id: str, api_key_id: int, model: str) -> tuple[int, str] | None:
        try:
            receipt = inspect_receipt(request_id, api_key_id, self.client, max_pages=self.max_pages)
        except (WopallyUnavailable, ValueError, TypeError) as exc:
            raise UsageUnavailable("中转站用量暂不可查询") from exc
        status = receipt.get("status")
        if status == "receipt_not_found":
            return None
        if status == "ownership_mismatch" or status == "confirmed" and receipt.get("model") != model:
            raise UsageUnavailable("中转站用量归属不符")
        if status != "confirmed":
            raise UsageUnavailable("无法确认唯一且完整的中转站收据")
        return receipt["actual_micro_cny"], receipt["provider_request_id"]
