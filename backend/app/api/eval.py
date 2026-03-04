"""
Eval Pipeline API — manage evaluation datasets and runs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db import database as db

logger = logging.getLogger("jarvis.api.eval")
router = APIRouter()


# --- Datasets ---

class CreateDatasetRequest(BaseModel):
    name: str
    description: str = ""
    sample_ids: List[int] = []
    created_by: str = ""


@router.post("/datasets")
async def create_dataset(req: CreateDatasetRequest):
    """Create an evaluation dataset."""
    record = await db.create_eval_dataset(req.model_dump())
    return {
        "id": record.id,
        "name": record.name,
        "status": "created",
    }


@router.get("/datasets")
async def list_datasets():
    """List all evaluation datasets."""
    return await db.list_eval_datasets()


@router.get("/datasets/{dataset_id}")
async def get_dataset(dataset_id: int):
    """Get a specific evaluation dataset."""
    ds = await db.get_eval_dataset(dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return ds


# --- Runs ---

class StartRunRequest(BaseModel):
    dataset_id: int
    config: dict = {}
    created_by: str = ""


@router.post("/run")
async def start_eval_run(req: StartRunRequest):
    """Start an evaluation run (executes in background)."""
    ds = await db.get_eval_dataset(req.dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")

    record = await db.create_eval_run(req.model_dump())

    # Run in background
    from app.services.eval_runner import run_eval
    asyncio.create_task(run_eval(record.id))

    return {"id": record.id, "status": "started"}


@router.get("/runs")
async def list_runs(dataset_id: Optional[int] = Query(None)):
    """List evaluation runs."""
    return await db.list_eval_runs(dataset_id=dataset_id)


@router.get("/runs/{run_id}")
async def get_run(run_id: int):
    """Get a specific evaluation run with full details."""
    run = await db.get_eval_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run
