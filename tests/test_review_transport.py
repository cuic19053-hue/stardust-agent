from __future__ import annotations

import httpx
import pytest

pytest.importorskip("langgraph.checkpoint.sqlite")

from server.proposal.review_runs import ReviewNeedsReview
from server.proposal.review_worker import LocalReviewModel


@pytest.mark.parametrize("url", ["https://127.0.0.1:11434", "http://example.com", "http://localhost:11434/path",
    "http://user:password@localhost:11434", "http://localhost:11434?key=secret", "http://localhost:11434#fragment"])
def test_local_model_rejects_remote_or_credential_bearing_destinations(url):
    with pytest.raises(ValueError):
        LocalReviewModel(url)


def test_local_model_never_follows_redirects_or_inherits_proxy(monkeypatch):
    calls, options = [], []
    original = httpx.Client

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://example.com/leak"})

    def client(**kwargs):
        options.append(kwargs)
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "Client", client)
    with pytest.raises(ReviewNeedsReview):
        LocalReviewModel("http://127.0.0.1:11434")({"model": "test"}, {"private": "草稿"}, {})
    assert calls == ["http://127.0.0.1:11434/api/chat"]
    assert options[0]["follow_redirects"] is False and options[0]["trust_env"] is False
