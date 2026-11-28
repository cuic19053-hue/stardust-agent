"""HTTP adapter for recoverable interactive turns, without changing transports."""
from __future__ import annotations

import json

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from .agent_journal import AgentJournal, JournalConflict, active_journal
from .review_runs import digest


def trial_operation(store, user_id):
    journal = active_journal.get()
    return (journal.execute("trial", {}, lambda: store.consume_free_chat(user_id))
            if journal else store.consume_free_chat(user_id))


def refund_legacy_trial(store, user_id):
    # A durable turn keeps its original trial allocation while it is resumed.
    # Legacy callers retain their existing behavior.
    if active_journal.get() is None:
        store.refund_free_chat(user_id)


def durable_chat(store, project_id, payload, request, invoke, *, save_setup=None):
    cfg = payload.llm_config.model_dump() if payload.llm_config else {"provider": "default"}
    # Only a hash of the configuration is retained; the original key is supplied
    # again by the authenticated client when recovering the same request.
    profile = digest({"config": cfg, "private_rag": request.headers.get("x-private-rag-consent", "")})
    try:
        journal = AgentJournal(store, project_id, payload.idempotency_key, profile, payload.message.strip())
        task = journal.open(save_setup=save_setup, resume_turn_id=payload.resume_turn_id)
    except JournalConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except RuntimeError:
        raise HTTPException(503, "对话任务状态未确认，请保留原请求并稍后恢复。") from None
    stream = bool(payload.stream or request.headers.get("accept") == "text/event-stream" or request.query_params.get("stream") == "true")
    if task["status"] == "completed":
        result = {**task["result"], "turn_id": task["id"], "replayed": True}
        if not stream:
            return result
        def replay():
            yield f'event: delta\ndata: {json.dumps({"type": "delta", "text": result["message"]}, ensure_ascii=False)}\n\n'
            yield f'event: done\ndata: {json.dumps({"type": "done", **result}, ensure_ascii=False)}\n\n'
        return StreamingResponse(replay(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
    token = active_journal.set(journal)
    try:
        response = invoke()
    except BaseException as exc:
        try:
            journal.suspend()
        except (ValueError, RuntimeError):
            pass  # Lease expiry preserves the same conservative outcome.
        if isinstance(exc, JournalConflict):
            raise HTTPException(409, str(exc)) from None
        raise
    finally:
        active_journal.reset(token)
    if not isinstance(response, StreamingResponse):
        result = {**response, "turn_id": journal.task_id}
        try:
            journal.complete(result)
        except (JournalConflict, RuntimeError):
            raise HTTPException(503, "答复的保存状态尚未确认，请使用原请求恢复，系统不会自动重新调用模型。") from None
        return result

    async def events():
        token = active_journal.set(journal)
        complete = False
        try:
            async for chunk in response.body_iterator:
                decoded = chunk.decode() if isinstance(chunk, bytes) else chunk
                if decoded.startswith("event: done\n"):
                    event = json.loads(decoded.split("data: ", 1)[1].strip())
                    event["turn_id"] = journal.task_id
                    await run_in_threadpool(journal.complete, event)
                    complete = True
                    yield f'event: done\ndata: {json.dumps(event, ensure_ascii=False)}\n\n'
                else:
                    yield chunk
        except (JournalConflict, RuntimeError):
            event = {"type": "error", "turn_id": journal.task_id,
                     "error": "本轮状态尚未确认。请保留原输入，稍后恢复原任务；系统不会自动重发未知调用。"}
            yield f'event: error\ndata: {json.dumps(event, ensure_ascii=False)}\n\n'
        finally:
            close = getattr(response.body_iterator, "aclose", None)
            if close:
                await close()
            if not complete:
                try:
                    await run_in_threadpool(journal.suspend)
                except (ValueError, RuntimeError):
                    pass
            active_journal.reset(token)
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
