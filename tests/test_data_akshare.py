import time

from pointtable.data.akshare import call_ak_with_retry, call_ak_with_timeout


def test_call_returns_value():
    assert call_ak_with_timeout(lambda value: value * 2, timeout=1, value=21) == 42


def test_call_returns_none_on_timeout_and_retries():
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.05)

    assert call_ak_with_retry(slow, timeout=0.01, retries=1, backoff=0) is None
    assert len(calls) == 2
