from pathlib import Path

from pointtable.notify.push import PushPlusSender


def test_sender_skips_without_token():
    path = Path.cwd() / 'test_push.yaml'
    path.write_text('pushplus:\n  token:\n  daily_limit: 1\n', encoding='utf-8')
    try:
        assert not PushPlusSender(path).send('t', 'c')
    finally:
        path.unlink(missing_ok=True)


def test_sender_chunks_long_content(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return {'code': 200}

    monkeypatch.setattr('pointtable.notify.push.requests.post', lambda *args, **kwargs: calls.append(kwargs) or Response())
    path = Path.cwd() / 'test_push.yaml'
    path.write_text('pushplus:\n  token: abc\n  daily_limit: 10\n', encoding='utf-8')
    try:
        sender = PushPlusSender(path)
        sender.MIN_INTERVAL = 0
        assert sender.send('t', 'x' * 18001)
        assert len(calls) == 2
    finally:
        path.unlink(missing_ok=True)
