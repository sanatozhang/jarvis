"""Tests for deterministic cloud sync parsing."""

from pathlib import Path

from app.services.cloud_sync_parser import parse_cloud_sync_summary


def test_cloud_sync_parser_ignores_non_upload_dio_errors(tmp_path: Path):
    log_path = tmp_path / "plaud.log"
    log_path.write_text(
        "\n".join([
            "INFO: 2026-01-30 21:15:31.000000: CloudSyncTrigger: 开始执行云同步",
            "INFO: 2026-01-30 21:15:31.100000: uploadChunk partNumber/partCount = 1/3",
            "INFO: 2026-01-30 21:15:32.000000: Dio Error unknown: error:HttpException: Connection reset by peer, uri = https://s3.amazonaws.com/upload",
            "INFO: 2026-01-30 21:15:33.000000: Dio Error unknown: error:HttpException: Bad file descriptor, uri = https://api.plaud.ai/config/init?platform=ios&version=3.7.0",
        ]),
        encoding="utf-8",
    )

    result = parse_cloud_sync_summary([log_path])

    assert result["failure_modes"]["s3_connection_reset"] == 1
    assert "upload_error" not in result["failure_modes"]
    assert len(result["upload_failures"]) == 1
