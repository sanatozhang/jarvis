from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest


async def test_send_image_message_uses_email_receiver():
    from app.services import feishu_cli
    with patch.object(feishu_cli, "_feishu_api", new=AsyncMock(return_value={"code": 0, "data": {}})) as m:
        ok = await feishu_cli.send_image_message(image_key="img_xxx", email="sanato.zhang@plaud.ai")
    assert ok is True
    # 校验调用了 im/v1/messages、receive_id_type=email、msg_type=image
    args, kwargs = m.call_args
    assert args[0] == "POST"
    assert args[1] == "/im/v1/messages"
    assert kwargs["params"]["receive_id_type"] == "email"
    assert kwargs["body"]["msg_type"] == "image"
    assert '"image_key": "img_xxx"' in kwargs["body"]["content"] or '"image_key":"img_xxx"' in kwargs["body"]["content"]


async def test_upload_image_raises_on_http_error():
    """Verify upload_image calls raise_for_status before json()."""
    from app.services import feishu_cli

    # Mock the token getter
    with patch.object(feishu_cli, "_get_tenant_token", new=AsyncMock(return_value="fake_token")):
        # Create a mock response that raises HTTPStatusError on raise_for_status()
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server error",
            request=MagicMock(),
            response=MagicMock(status_code=500),
        )

        # Mock httpx.AsyncClient context manager
        mock_client = MagicMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(feishu_cli.httpx, "AsyncClient", return_value=mock_client):
            with pytest.raises(httpx.HTTPStatusError):
                await feishu_cli.upload_image(b"fake_image_data")

            # Verify raise_for_status was called before json()
            mock_response.raise_for_status.assert_called_once()
            mock_response.json.assert_not_called()
