from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from audio_memory.prompts.composer import PromptComposer
from audio_memory.prompts.store import PromptConflictError, PromptDocument, PromptStore


router = APIRouter(prefix="/api/prompts", tags=["prompts"])


class PromptView(BaseModel):
    scene_id: str
    version: int
    content: str
    label: str | None = None
    editable: bool = True
    source: str = "legacy-local-file"

    @classmethod
    def from_document(cls, document: PromptDocument) -> PromptView:
        return cls(
            scene_id=document.scene_id,
            version=document.version,
            content=document.content,
        )


class PromptUpdate(BaseModel):
    expected_version: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=50_000)


def store_from(request: Request) -> PromptStore:
    return request.app.state.prompt_store


@router.get("")
async def list_prompts(request: Request) -> dict[str, list[PromptView]]:
    # Initialize old files for read/write compatibility, but do not advertise them
    # as part of the active autonomous-analysis runtime.
    await asyncio.to_thread(store_from(request).initialize)
    return {
        "prompts": [
            PromptView(**item, editable=False, source="versioned-code")
            for item in PromptComposer.autonomous_prompt_documents()
        ]
    }


@router.get("/{scene_id}")
async def get_prompt(scene_id: str, request: Request) -> PromptView:
    try:
        document = await asyncio.to_thread(store_from(request).get, scene_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PromptView.from_document(document)


@router.put("/{scene_id}")
async def save_prompt(
    scene_id: str, payload: PromptUpdate, request: Request
) -> PromptView:
    try:
        document = await asyncio.to_thread(
            store_from(request).save,
            scene_id,
            expected_version=payload.expected_version,
            content=payload.content,
        )
    except PromptConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PromptView.from_document(document)
