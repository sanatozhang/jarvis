import base64
from unittest.mock import AsyncMock, patch


async def test_site_feedback_text_and_image(client):
    png_b64 = "data:image/png;base64," + base64.b64encode(b"\x89PNGfake").decode()
    with patch("app.services.feishu_cli.send_message", new=AsyncMock(return_value=True)) as send_msg, \
         patch("app.services.feishu_cli.upload_image", new=AsyncMock(return_value="img_1")) as up, \
         patch("app.services.feishu_cli.send_image_message", new=AsyncMock(return_value=True)) as send_img:
        resp = await client.post("/api/site-feedback", json={
            "message": "按钮点不动",
            "page_url": "http://x/tracking?detail=abc",
            "screenshot": png_b64,
            "user_email": "u@plaud.ai",
        })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "sent"
    assert body["image_sent"] is True
    # 文本消息含反馈内容与工单 URL
    sent_text = send_msg.call_args.kwargs.get("text", "")
    assert "按钮点不动" in sent_text and "tracking?detail=abc" in sent_text
    up.assert_awaited_once()
    send_img.assert_awaited_once()


async def test_site_feedback_text_only(client):
    with patch("app.services.feishu_cli.send_message", new=AsyncMock(return_value=True)), \
         patch("app.services.feishu_cli.upload_image", new=AsyncMock(return_value="img_1")) as up:
        resp = await client.post("/api/site-feedback", json={"message": "仅文字"})
    assert resp.status_code == 200
    assert resp.json()["image_sent"] is False
    up.assert_not_awaited()


async def test_site_feedback_requires_message(client):
    resp = await client.post("/api/site-feedback", json={"message": "   "})
    assert resp.status_code == 422
