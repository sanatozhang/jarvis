import pytest

from app.modulehub.adapters.jenkins_runner import JenkinsBuildRunner


class FakeJk:
    def __init__(self):
        self.item = {}
        self.build = {}
        self.console = "STEP=tag\n"
        self.artifact = '{"ok": true}'
        self.params = None
        self.console_error = False

    async def pick_least_busy_server(self):
        return "http://jenkins:8080"

    async def trigger_build(self, server, job, params):
        self.params = (server, job, params)
        return 42, "loc"

    async def fetch_queue_item(self, server, qid):
        return self.item

    async def fetch_build_status(self, server, url):
        self.last_build_url = url
        return self.build

    async def fetch_console_tail(self, server, url):
        if self.console_error:
            raise RuntimeError("console gone")
        return self.console

    async def fetch_artifact_text(self, server, url, rel):
        self.artifact_rel = rel
        return self.artifact


@pytest.fixture
def jk():
    return FakeJk()


async def test_trigger_sends_contract_parameters(jk):
    h = await JenkinsBuildRunner(jk, "module-publish").trigger(repo="Plaud-AI/r", branch="release/1", platforms=["android", "ios"],
                                                              major=False, dry_run=True, resume=False)
    assert h.ref == "http://jenkins:8080|42"
    assert jk.params == ("http://jenkins:8080", "module-publish", {
        "REPO": "Plaud-AI/r", "BRANCH": "release/1", "PLATFORMS": "android,ios", "MAJOR": "false", "DRY_RUN": "true", "RESUME": "false"})


async def test_status_queue_running_and_gone(jk):
    r = JenkinsBuildRunner(jk, "j")
    assert (await r.status("http://jenkins:8080|42")).state == "queued"
    jk.item = {"_gone": True}
    assert (await r.status("http://jenkins:8080|42")).state == "failure"
    jk.item = {"executable": {"url": "http://localhost:8080/job/j/7/"}}
    jk.build = {"building": True}
    st = await r.status("http://jenkins:8080|42")
    assert st.state == "running" and st.url == "http://jenkins:8080/job/j/7/" and jk.last_build_url == st.url


async def test_status_success_reads_result_artifact(jk):
    jk.item = {"executable": {"url": "http://jenkins:8080/job/j/7/"}}
    jk.build = {"building": False, "result": "SUCCESS", "artifacts": [
        {"fileName": "other.txt", "relativePath": "other.txt"},
        {"fileName": "publish-result.json", "relativePath": "module/logger/build/module/publish-result.json"}]}
    st = await JenkinsBuildRunner(jk, "j").status("http://jenkins:8080|42")
    assert st.state == "success" and st.result_json == '{"ok": true}'
    assert jk.artifact_rel == "module/logger/build/module/publish-result.json"


async def test_success_without_artifact_has_no_result(jk):
    jk.item = {"executable": {"url": "http://jenkins:8080/job/j/7/"}}
    jk.build = {"building": False, "result": "SUCCESS"}
    assert (await JenkinsBuildRunner(jk, "j").status("http://jenkins:8080|42")).result_json is None


@pytest.mark.parametrize("result,state", [("FAILURE", "failure"), ("ABORTED", "aborted"), (None, "failure")])
async def test_failures_carry_console_tail(jk, result, state):
    jk.item = {"executable": {"url": "http://jenkins:8080/job/j/7/"}}
    jk.build = {"building": False, "result": result}
    st = await JenkinsBuildRunner(jk, "j").status("http://jenkins:8080|42")
    assert st.state == state and "STEP=tag" in st.log_tail


async def test_console_errors_are_not_fatal(jk):
    jk.console_error = True
    jk.item = {"executable": {"url": "http://jenkins:8080/job/j/7/"}}
    jk.build = {"building": False, "result": "FAILURE"}
    assert (await JenkinsBuildRunner(jk, "j").status("http://jenkins:8080|42")).log_tail == ""


def test_rewrite_helper_handles_empty_url():
    from app.modulehub.adapters.jenkins_runner import _rewrite

    assert _rewrite("http://s", "") == ""
    assert _rewrite("http://s/", "http://localhost/job/x/1/?a=b") == "http://s/job/x/1/?a=b"


async def test_pinned_server_is_used_without_load_balancing(jk):
    # publishing needs the build machine's Xcode: a pinned server must never be swapped for the least busy one
    async def boom():
        raise AssertionError("must not load-balance when a server is pinned")
    jk.pick_least_busy_server = boom
    h = await JenkinsBuildRunner(jk, "module-publish", server="http://10.0.52.101:8080/").trigger(
        repo="Plaud-AI/r", branch="main", platforms=["ios"], major=False, dry_run=False, resume=False)
    assert h.ref == "http://10.0.52.101:8080|42"
    assert jk.params[0] == "http://10.0.52.101:8080"
