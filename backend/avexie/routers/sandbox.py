"""Authenticated API surface for the self-hosted Judge0 sandbox."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from avexie.tools.run_code import run_code
from avexie.utils.auth import get_verified_user


router = APIRouter()


class SandboxLimits(BaseModel):
    cpu_time: float | None = Field(default=None, gt=0)
    wall_time: float | None = Field(default=None, gt=0)
    memory_kb: int | None = Field(default=None, gt=0)
    max_processes: int | None = Field(default=None, gt=0)


class RunCodeForm(BaseModel):
    language: str = Field(min_length=1, max_length=32)
    source: str = Field(min_length=1)
    stdin: str = ''
    limits: SandboxLimits | None = None


@router.post('/run')
async def execute_code(
    form_data: RunCodeForm,
    _user=Depends(get_verified_user),
) -> dict[str, Any]:
    """Execute one source file with deployment-enforced, no-egress limits."""

    try:
        return await run_code(
            language=form_data.language,
            source=form_data.source,
            stdin=form_data.stdin,
            limits=form_data.limits.model_dump(exclude_none=True) if form_data.limits else None,
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
