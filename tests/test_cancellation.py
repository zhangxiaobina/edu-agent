from __future__ import annotations

import time

import pytest

from edu_agent.runtime.cancellation import (
    CancellationRequested,
    CancellationToken,
    call_with_cancellation,
)


def test_deadline_cancellation_is_idempotent_and_notifies_once():
    token = CancellationToken.with_timeout(0.01)
    notifications = []
    token.register(notifications.append)

    assert token.wait(0.5)
    cancellation = token.cancellation
    assert cancellation is not None
    assert cancellation.source == "deadline"
    assert token.cancel("duplicate", source="explicit") is False
    assert notifications == [cancellation]


def test_parent_cancellation_propagates_to_child_until_unlinked():
    parent = CancellationToken()
    child = CancellationToken(parent=parent)

    assert parent.cancel("client disconnected", source="client_disconnect")
    with pytest.raises(CancellationRequested, match="child.boundary") as captured:
        child.checkpoint("child.boundary")
    assert captured.value.cancellation.source == "client_disconnect"
    child.close()


def test_sync_call_result_is_rejected_when_token_expires_during_call():
    token = CancellationToken(deadline=time.monotonic() + 0.01)

    def blocking_call():
        time.sleep(0.02)
        return "late-result"

    with pytest.raises(CancellationRequested, match="provider.after_call"):
        call_with_cancellation(blocking_call, cancellation_token=token)
