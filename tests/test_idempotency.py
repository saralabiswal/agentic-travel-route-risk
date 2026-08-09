from datetime import UTC, datetime

from apps.api.idempotency import IdempotencyStore, WebhookReplayStore


def test_idempotency_and_webhook_replay_protection():
    store = IdempotencyStore()
    store.put("acme", "key-1", {"status": "done"})
    assert store.get("acme", "key-1") == {"status": "done"}
    replay = WebhookReplayStore()
    assert replay.accept_once("message-1", datetime.now(UTC))
    assert not replay.accept_once("message-1", datetime.now(UTC))
