"""Resources are READ tools in JSON clothing; prompts carry the process; both are audited."""

import json

import anyio
import pytest

mcp = pytest.importorskip("mcp")
from mcp import Client  # noqa: E402

from jobagent.assistant.operator_tools import OperatorDeps  # noqa: E402
from jobagent.config import Settings  # noqa: E402
from jobagent.mcp import build_server  # noqa: E402
from jobagent.mcp.operator import Operator  # noqa: E402
from jobagent.mcp.prompts import INSTRUCTIONS  # noqa: E402
from jobagent.store.db import Store  # noqa: E402


def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAGENT_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOBAGENT_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("JOBAGENT_CV_PATH", str(tmp_path / "cv.md"))
    return Settings(_env_file=None)


def _deps(tmp_path):
    return OperatorDeps(db_path=str(tmp_path / "t.db"), local_path=str(tmp_path / "none.toml"),
                        overlay_path=str(tmp_path / "profile.json"), cv_loader=lambda: "",
                        llm_factory=lambda: None, env_path=str(tmp_path / ".env"),
                        spawn=lambda fn: fn())


@pytest.fixture
def rig(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    op = Operator(settings, deps=_deps(tmp_path))
    server = build_server(settings, operator=op)
    yield server, op, str(tmp_path / "t.db")
    op.close()


def test_every_resource_is_listed_and_the_static_ones_read_as_json(rig):
    server, _, _ = rig

    async def main():
        async with Client(server, raise_exceptions=True) as c:
            uris = {str(r.uri) for r in (await c.list_resources()).resources}
            templates = {t.uri_template for t in (await c.list_resource_templates()).resource_templates}
            lifecycle = await c.read_resource("personalagent://lifecycle")
            status = await c.read_resource("personalagent://status")
            return uris, templates, lifecycle, status

    uris, templates, lifecycle, status = anyio.run(main)
    assert {"personalagent://status", "personalagent://profile", "personalagent://settings",
            "personalagent://lifecycle", "personalagent://matches", "personalagent://applications",
            "personalagent://runs"} <= uris
    assert {"personalagent://runs/{run_id}", "personalagent://jobs/{job_id}"} <= templates
    graph = json.loads(lifecycle.contents[0].text)
    assert graph["matched"] == ["drafting", "skipped"]
    state = json.loads(status.contents[0].text)
    assert state["jobs"] == 0 and state["last_ingest"] is None and state["next"]


def test_resource_reads_are_audited_like_tool_calls(rig):
    server, op, db = rig

    async def main():
        async with Client(server, raise_exceptions=True) as c:
            await c.read_resource("personalagent://settings")

    anyio.run(main)
    s = Store(db)
    try:
        intents = [e for e in s.events_for_run(op.run_id) if e["kind"] == "tool_intent"]
    finally:
        s.close()
    assert any(e["tool"] == "current_config" for e in intents)


def test_a_missing_run_reads_as_a_plain_message_not_an_exception(rig):
    server, _, _ = rig

    async def main():
        async with Client(server, raise_exceptions=True) as c:
            return await c.read_resource("personalagent://runs/doesnotexist")

    out = anyio.run(main)
    assert "No events for that run id" in out.contents[0].text


def test_the_prompts_carry_the_process_and_the_boundaries(rig):
    server, _, _ = rig

    async def main():
        async with Client(server, raise_exceptions=True) as c:
            names = {p.name for p in (await c.list_prompts()).prompts}
            operate = await c.get_prompt("operate")
            onboard = await c.get_prompt("onboard")
            return names, operate, onboard

    names, operate, onboard = anyio.run(main)
    assert names == {"onboard", "operate"}
    text = operate.messages[0].content.text
    assert "request_human_action" in text and "set_application_status" in text
    assert "setup_status" in onboard.messages[0].content.text
    assert "cannot send" in INSTRUCTIONS and "never ask the operator for a credential" in INSTRUCTIONS.lower()
