from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from audio_memory.content.clear import HistoryBusyError


router = APIRouter(prefix="/api", tags=["content"])


class TodoUpdate(BaseModel):
    text: str | None = None
    due_at: str | None = None
    completed: bool | None = None


class QuestionInput(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class FeedbackInput(BaseModel):
    rating: str
    explanation: str | None = None


class ClearInput(BaseModel):
    confirm: bool


@router.get("/feed")
async def feed(request: Request):
    return await request.app.state.content_service.feed()


@router.get("/history")
async def history(request: Request):
    return await request.app.state.content_service.history()


@router.patch("/todos/{todo_id}")
async def update_todo(todo_id: str, payload: TodoUpdate, request: Request):
    try:
        return await request.app.state.content_service.update_todo(
            todo_id,
            text=payload.text,
            completed=payload.completed,
            due_at=payload.due_at,
            update_due_at="due_at" in payload.model_fields_set,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/todos/{todo_id}", status_code=204)
async def delete_todo(todo_id: str, request: Request) -> Response:
    try:
        await request.app.state.content_service.delete_todo(todo_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=204)


@router.post("/cards/{card_id}/questions")
async def ask_card(card_id: str, payload: QuestionInput, request: Request):
    try:
        messages = await request.app.state.content_service.ask(
            card_id, payload.question
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"messages": messages}


@router.post("/cards/{card_id}/feedback", status_code=201)
async def submit_feedback(
    card_id: str, payload: FeedbackInput, request: Request
):
    try:
        context = await request.app.state.content_service.feedback_context(card_id)
        record = await request.app.state.feedback_writer.write(
            card_id=card_id,
            rating=payload.rating,
            explanation=payload.explanation,
            **context,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": record.id, "created_at": record.created_at}


@router.delete("/history", status_code=204)
async def clear_history(payload: ClearInput, request: Request) -> Response:
    try:
        await request.app.state.history_cleaner.clear(confirm=payload.confirm)
    except HistoryBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "history_reanalysis_active", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(status_code=204)
