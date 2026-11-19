"""Retrieval, briefs and chat: hybrid ranking, scoping, citation verification and abstention."""

import json

from doc_intelligence.briefing import RawBriefing, generate_briefing, select_passages, verify
from doc_intelligence.chat import NOT_FOUND, verify_citations
from doc_intelligence.rag.bm25 import BM25, reciprocal_rank_fusion
from doc_intelligence.rag.retriever import Passage

TRAVEL_MD = b"""# Travel Policy

## Hotels
Hotel limits are Rs. 6,000 per night in metro cities and Rs. 4,000 elsewhere.

## Claims
Expense claims must be submitted within 45 days of the trip ending.
"""


def passage(i: int, text: str = "text") -> Passage:
    return Passage(chunk_id=f"c{i}", document_id="d", title="Doc", file_format="md", page_number=1,
                   page_label="Part 1", heading=None, text=text, start=0, end=len(text), score=0.0)


def test_bm25_and_rank_fusion():
    index = BM25([("a", "security deposit two months rent"), ("b", "notice period"), ("c", "deposit refund")])
    assert [d for d, _ in index.search("security deposit")][:1] == ["a"]
    assert reciprocal_rank_fusion([["a", "b"], ["c", "a"]])[0][0] == "a"


async def test_search_is_scoped_to_collection_and_document(client, register_and_login, upload, collection):
    headers = await register_and_login()
    hr = await collection(headers)
    travel = await collection(headers, "Travel")
    await upload(headers, collection_id=hr)
    travel_doc = (await upload(headers, TRAVEL_MD, "travel.md", collection_id=travel)).json()

    hits = (await client.post("/search", headers=headers, json={"query": "expense claims days"})).json()
    assert {h["title"] for h in hits} == {"leave policy", "travel"}  # no scope: all of the user's documents
    hits = (await client.post("/search", headers=headers,
                              json={"query": "expense claims days", "collection_id": travel})).json()
    assert {h["title"] for h in hits} == {"travel"}
    hits = (await client.post("/search", headers=headers,
                              json={"query": "expense claims", "document_id": travel_doc["id"]})).json()
    assert hits[0]["heading"] == "Claims" and hits[0]["page_label"] == "Part 1"


async def test_search_never_crosses_users(client, register_and_login, upload):
    alice = await register_and_login("alice@example.com")
    bob = await register_and_login("bob@example.com")
    await upload(alice)
    assert (await client.post("/search", headers=bob, json={"query": "annual leave"})).json() == []


def test_verify_drops_items_with_invalid_citations():
    passages = [passage(1, "Leave is 18 days."), passage(2, "Claims within 30 days.")]
    raw = RawBriefing(summary="A leave policy.", summary_sources=["S1", "S9"],
                      key_points=[{"text": "18 days leave", "sources": ["S1"]},
                                  {"text": "invented", "sources": ["S7"]}],
                      facts=[{"type": "Amount", "value": "30 days", "sources": ["[S2]"]},
                             {"type": "date", "value": "made up", "sources": []}])
    out = verify(raw, passages)
    assert [k["text"] for k in out["key_points"]] == ["18 days leave"]
    assert [f["value"] for f in out["facts"]] == ["30 days"] and out["facts"][0]["type"] == "amount"
    assert len(out["summary_citations"]) == 1


def test_long_documents_are_sampled_and_coverage_reported():
    passages = [passage(i, "x" * 1000) for i in range(100)]
    chosen, coverage = select_passages(passages, budget=20_000)
    assert len(chosen) == 20 and coverage == 0.2
    assert chosen[0].chunk_id == "c0" and chosen[-1].chunk_id == "c95"  # spread across the document


class FakeModel:
    model_name = "fake"
    offline = False

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls = 0

    async def complete(self, messages, max_tokens=800, json_mode=False):
        self.calls += 1
        return self.replies.pop(0)


async def test_briefing_repairs_invalid_json_once():
    good = json.dumps({"summary": "Leave rules.", "summary_sources": ["S1"],
                       "key_points": [{"text": "18 days", "sources": ["S1"]}]})
    model = FakeModel(["not json", good])
    result = await generate_briefing(model, "Leave", [passage(1, "Leave is 18 days.")])
    assert model.calls == 2 and result.content["key_points"][0]["text"] == "18 days"


def test_chat_citation_verification_removes_invented_labels():
    answer, used = verify_citations("Leave is 18 days [S1]. Also [S7].", [passage(1)])
    assert "[S7]" not in answer and "[S1]" in answer
    assert [u["label"] for u in used] == ["S1"] and used[0]["start"] == 0
    assert answer.endswith("Also.")  # the gap left by the removed label is tidied
    kept, _ = verify_citations("Set the key in .env [S1].", [passage(1)])
    assert "in .env" in kept  # ordinary text is never altered


async def test_chat_answers_with_citations_and_follow_ups(client, register_and_login, upload, collection):
    headers = await register_and_login()
    cid = await collection(headers)
    await upload(headers, collection_id=cid)
    chat = (await client.post("/chat/sessions", headers=headers, json={"collection_id": cid})).json()
    r = await client.post(f"/chat/sessions/{chat['id']}/messages", headers=headers,
                          json={"content": "How many days of annual leave do I get?"})
    assert r.status_code == 200, r.text
    reply = r.json()
    assert reply["citations"] and reply["grounded"] is True
    assert {"document_id", "page", "start", "end"} <= set(reply["citations"][0])
    detail = (await client.get(f"/chat/sessions/{chat['id']}", headers=headers)).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["title"] == "How many days of annual leave do I get?"
    r = await client.post(f"/chat/sessions/{chat['id']}/messages", headers=headers, json={"content": "And sick leave?"})
    assert r.status_code == 200 and r.json()["search_query"]


async def test_chat_with_empty_collection_says_not_found(client, register_and_login, collection):
    headers = await register_and_login()
    cid = await collection(headers, "Empty")
    chat = (await client.post("/chat/sessions", headers=headers, json={"collection_id": cid})).json()
    reply = (await client.post(f"/chat/sessions/{chat['id']}/messages", headers=headers,
                               json={"content": "What is the leave policy?"})).json()
    assert reply["content"].startswith(NOT_FOUND) and reply["grounded"] is False


async def test_chat_scoped_to_one_document(client, register_and_login, upload, collection):
    headers = await register_and_login()
    cid = await collection(headers)
    await upload(headers, collection_id=cid)
    travel = (await upload(headers, TRAVEL_MD, "travel.md", collection_id=cid)).json()
    chat = (await client.post("/chat/sessions", headers=headers,
                              json={"collection_id": cid, "document_id": travel["id"]})).json()
    assert chat["document_title"] == "travel"
    reply = (await client.post(f"/chat/sessions/{chat['id']}/messages", headers=headers,
                               json={"content": "expense claims deadline"})).json()
    assert {c["title"] for c in reply["citations"]} == {"travel"}


async def test_chat_is_private(client, register_and_login, collection):
    alice = await register_and_login("alice@example.com")
    bob = await register_and_login("bob@example.com")
    alice_col = await collection(alice)
    assert (await client.post("/chat/sessions", headers=bob, json={"collection_id": alice_col})).status_code == 404
    chat = (await client.post("/chat/sessions", headers=alice, json={"collection_id": alice_col})).json()
    assert (await client.get(f"/chat/sessions/{chat['id']}", headers=bob)).status_code == 404
    assert (await client.post(f"/chat/sessions/{chat['id']}/messages", headers=bob,
                              json={"content": "hi"})).status_code == 404


async def test_chat_without_ai_provider_explains_setup(client, register_and_login, collection, app):
    app.state.chat_model = None
    headers = await register_and_login()
    chat = (await client.post("/chat/sessions", headers=headers, json={"collection_id": await collection(headers)})).json()
    r = await client.post(f"/chat/sessions/{chat['id']}/messages", headers=headers, json={"content": "hello"})
    assert r.status_code == 503 and "DI_OPENAI_API_KEY" in r.json()["detail"]
