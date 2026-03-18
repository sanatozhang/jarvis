"""
Tools API — utility endpoints for support staff.

POST /api/tools/lost-file-finder
    Upload a .plaud or .log file + specify a problem date and timezone.
    Returns a Markdown analysis report showing which recordings were synced
    after that date and which ones may be "lost" due to timestamp anomalies.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse

from app.services.decrypt import process_log_file
from app.services.lost_file_finder import analyze_log

router = APIRouter()


@router.post("/lost-file-finder")
async def lost_file_finder(
    file: UploadFile = File(..., description=".plaud 或 .log 设备日志文件"),
    problem_date: str = Form(..., description="排查起始日期，格式 YYYY-MM-DD"),
    tz_offset: float = Form(8.0, description="UTC 时区偏移（小时），例如 8 表示 CST +8"),
):
    """
    Analyze a Plaud device log for lost recordings after a given date.

    Returns JSON:
      - markdown: full analysis report in Markdown
      - total_records: number of sync records found
      - anomaly_count: number of records with timestamp anomalies
      - problem_date_text: the parsed problem date (YYYY-MM-DD)
      - timezone_label: human-readable timezone string
    """
    # Parse problem date
    try:
        problem_dt = datetime.strptime(problem_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="problem_date 格式无效，应为 YYYY-MM-DD")

    # Write uploaded file to a temp dir
    suffix = Path(file.filename or "upload").suffix or ".bin"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / f"upload{suffix}"
        tmp_path.write_bytes(await file.read())

        work_dir = Path(tmp) / "work"
        work_dir.mkdir()

        # Decrypt / extract log (reuses existing pipeline)
        log_path, parse_error, reason = process_log_file(tmp_path, work_dir)

        if log_path is None:
            raise HTTPException(
                status_code=422,
                detail=f"无法解析上传的文件：{reason or '未知错误'}。请上传 .plaud 或 .log 格式的设备日志。",
            )

        try:
            log_content = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"读取日志文件失败：{e}")

        # Run the analysis
        try:
            result = analyze_log(log_content, problem_dt, tz_offset_hours=tz_offset)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    return JSONResponse({
        "markdown": result.markdown,
        "total_records": result.total_records,
        "anomaly_count": result.anomaly_count,
        "problem_date_text": result.problem_date_text,
        "timezone_label": result.timezone_label,
    })
