"""Bounded claim queries over an immutable, user-confirmed material snapshot."""
from __future__ import annotations

import hashlib
import re
from collections import deque

from .retrieval import ProjectRetriever

PLAN = re.compile(r"计划|拟|预计|目标|预期|将|后续|后再|尚未|未开展|未采集|估算")
COMPLETED = re.compile(r"(?:已|已经)(?:经)?(?:完成|获得|取得|实现|采集|达到)|准确率达到|召回率达到")
POLICY = re.compile(r"学校.{0,15}(?:要求|规定|批准|审批|同意)|根据.{0,35}通知|必须|不得申报|申报资格|同意立项")


def claim_type(text):
    if POLICY.search(text):
        return "policy"
    # A sentence may contain both a future target and a completed result.
    for match in COMPLETED.finditer(text):
        prefix = re.split(r"[，,；;。]", text[:match.start()])[-1]
        if not re.search(r"尚未|没有|不预设|未开展|未采集|(?:计划|拟|预计|预期|目标|将).{0,35}$", prefix):
            return "completed_result"
    if re.search(r"尚未|未开展|未采集|没有.{0,15}(?:授权|实验|实测)", text):
        return "not_completed"
    if PLAN.search(text):
        return "plan"
    return "fact"


def extract_claims(project):
    """Round-robin chapters so a long opening chapter cannot consume the budget."""
    queues = []
    for section in project.get("outline", [])[:30]:
        sid = section["id"]
        body = str(project.get("sections", {}).get(sid) or "")
        sentences = [s.strip() for s in re.split(r"(?<=[。！？；\n])", body) if len(s.strip()) >= 4]
        # Long sentences are queried by bounded windows, never replaced by a title.
        pieces = [s[i:i + 240] for s in sentences for i in range(0, len(s), 240)]
        ranked = sorted(enumerate(pieces), key=lambda p: (
            -(3 * bool(POLICY.search(p[1])) + 2 * bool(COMPLETED.search(p[1])) + bool(re.search(r"\d", p[1]))), p[0]))[:8]
        queues.append(deque({"id": hashlib.sha256((sid + "\0" + quote).encode()).hexdigest()[:20],
            "section_id": sid, "quote": quote, "type": claim_type(quote),
            "query": f"{section.get('title', '')} {quote}"} for _, quote in ranked))
    claims = []
    while any(queues) and len(claims) < 80:
        for queue in queues:
            if queue and len(claims) < 80:
                claims.append(queue.popleft())
    return claims


def retrieve_claim_evidence(snapshot, store, embedder):
    claims = extract_claims(snapshot["project"])
    retriever = ProjectRetriever(store, embedder)
    documents = {d["id"]: d for d in snapshot["documents"]}
    candidates, evidence = [], {}
    for claim in claims:
        hits = retriever.search(snapshot["project"]["id"], claim["query"], top_k=3)["results"]
        candidates.append((claim, hits))
        claim["evidence_ids"] = []
    # One first hit per claim before any second hit; preserve links even for duplicates.
    for rank in range(3):
        for claim, hits in candidates:
            if rank >= len(hits):
                continue
            hit = hits[rank]
            doc = documents.get(hit["document_id"], {})
            if (not doc.get("confirmed") or doc.get("confirmation_origin") != "user"
                    or doc.get("kind") == "template" or hit["text"] not in doc.get("extracted_text", "")):
                continue
            if hit["chunk_id"] not in evidence and len(evidence) >= 12:
                continue
            item = evidence.setdefault(hit["chunk_id"], {"id": hit["chunk_id"],
                "document_id": hit["document_id"], "source_name": hit["source_name"],
                "source_ref": hit["source_ref"], "content": hit["text"],
                "source_kind": doc.get("kind", "source"), "claim_ids": []})
            if claim["id"] not in item["claim_ids"]:
                item["claim_ids"].append(claim["id"])
                claim["evidence_ids"].append(item["id"])
    return claims, list(evidence.values())
