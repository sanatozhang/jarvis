from unittest.mock import AsyncMock, patch


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
