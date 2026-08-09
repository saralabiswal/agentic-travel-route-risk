from apps.api.security import FixedWindowRateLimiter, redact


def test_redaction_and_rate_limit():
    assert redact({"token": "secret", "trip": "t1"}) == {"token": "[REDACTED]", "trip": "t1"}
    limiter = FixedWindowRateLimiter(limit=1)
    assert limiter.allow("acme:actor")
    assert not limiter.allow("acme:actor")
