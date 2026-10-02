"""Tests for the two additive methods modulehub needs on the shared Jenkins client."""
import httpx
import pytest

from app.services.jenkins_client import JenkinsClient, JenkinsError, JenkinsServerCreds

SERVER = "http://jenkins:8080"


def client_with(handler):
    jk = JenkinsClient([JenkinsServerCreds(SERVER, "u", "t")])
    jk._client = lambda creds: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return jk


async def test_console_tail_is_truncated_to_the_end():
    jk = client_with(lambda req: httpx.Response(200, text="x" * 100 + "STEP=tag"))
    assert (await jk.fetch_console_tail(SERVER, SERVER + "/job/j/1/", max_bytes=8)) == "STEP=tag"


async def test_artifact_text_uses_artifact_path():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, text="{}")

    jk = client_with(handler)
    assert await jk.fetch_artifact_text(SERVER, SERVER + "/job/j/1", "a/b.json") == "{}"
    assert seen == [SERVER + "/job/j/1/artifact/a/b.json"]


async def test_non_200_and_transport_errors_raise_jenkins_error():
    jk = client_with(lambda req: httpx.Response(404))
    with pytest.raises(JenkinsError):
        await jk.fetch_console_tail(SERVER, SERVER + "/job/j/1/")
    with pytest.raises(JenkinsError):
        await jk.fetch_artifact_text(SERVER, SERVER + "/job/j/1/", "x")

    def boom(req):
        raise httpx.ConnectError("down")

    jk = client_with(boom)
    with pytest.raises(JenkinsError):
        await jk.fetch_console_tail(SERVER, SERVER + "/job/j/1/")
    with pytest.raises(JenkinsError):
        await jk.fetch_artifact_text(SERVER, SERVER + "/job/j/1/", "x")
