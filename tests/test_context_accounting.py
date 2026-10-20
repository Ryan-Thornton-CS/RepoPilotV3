"""Verify the context ring's source metrics without pretending to measure remote usage."""

from pathlib import Path

from repopilotv3.manual.sessions import measure
from repopilotv3.workflow.service import WorkflowService


def test_context_totals_deduplicate_transfers_and_remain_session_scoped(ws, session):
    """Only recorded messages/transfers count; exports and other chats never inflate the numerator."""
    service = WorkflowService(ws)
    service.configure(session, context_limit=1000)
    outgoing, reply = "x" * 320, "Ω" * 320
    service.transfer(session, outgoing)
    service.transfer(session, outgoing)
    service.sessions.record(session, "model:test", "model_reply", reply)
    service.sessions.record(session, "model:test", "model_reply", reply)
    expected = measure(outgoing)["estimated_tokens"] + measure(reply)["estimated_tokens"]
    metrics = service.status(session)["metrics"]
    assert metrics["events"] == 2 and metrics["exchanges"] == 1
    assert metrics["observed_estimated_tokens"] == expected
    assert metrics["estimated_budget_ratio"] == round(expected / 1000, 4)
    package = service.prepare(session)
    assert service.status(session)["metrics"] == metrics
    name = "00_PROJECT_INSTRUCTIONS.md"
    fingerprint = package["manifest"]["artifacts"][name]["hash"]
    service.confirm(session, package["manifest"]["id"], {name: fingerprint})
    measured_upload = measure((Path(package["directory"]) / name).read_bytes().decode("utf-8"))["estimated_tokens"]
    confirmed = service.status(session)["metrics"]
    assert confirmed["observed_estimated_tokens"] == expected + measured_upload
    assert confirmed["estimated_budget_ratio"] > 1 and confirmed["handoff_recommended"]
    assert confirmed["confidence"] == "low" and not confirmed["tracking_complete"]
    service.confirm(session, package["manifest"]["id"], {name: fingerprint})
    assert service.status(session)["metrics"] == confirmed
    other = service.create()["session"]["id"]
    assert service.status(other)["metrics"]["observed_estimated_tokens"] == 0
    assert service.status(session)["metrics"] == confirmed
