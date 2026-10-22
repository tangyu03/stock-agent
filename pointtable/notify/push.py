from __future__ import annotations

import threading
import time
from pathlib import Path

import requests
import yaml


class PushPlusSender:
    MIN_INTERVAL = 1.2
    MAX_CONTENT_LEN = 18000

    def __init__(self, config_path: str | Path | None = None, token: str | None = None):
        path = config_path or Path(__file__).parents[1] / 'config' / 'push.yaml'
        raw = yaml.safe_load(Path(path).read_text(encoding='utf-8')) or {}
        push = raw.get('pushplus') or {}
        self.mode = str(push.get('mode') or 'formal')
        if token is None:
            token = push.get('test_token') if self.mode == 'shadow' else push.get('token')
        self.token = str(token or '')
        self.daily_limit = int(push.get('daily_limit') or 200)
        self.url = 'https://www.pushplus.plus/send'
        self._sent_count = 0
        self._sent_date = time.strftime('%Y-%m-%d')
        self._last_sent = 0.0
        self._lock = threading.Lock()

    def send(self, title: str, content: str, template: str = 'html', level: str = '常规') -> bool:
        with self._lock:
            today = time.strftime('%Y-%m-%d')
            if today != self._sent_date:
                self._sent_count = 0
                self._sent_date = today
            if self._sent_count >= self.daily_limit or not self.token:
                return False
            chunks = self._chunk(content)
            ok = True
            for index, chunk in enumerate(chunks):
                chunk_title = title if len(chunks) == 1 else f'{title} ({index + 1}/{len(chunks)})'
                if not self._post(chunk_title, chunk, template, level):
                    ok = False
            if ok:
                self._sent_count += len(chunks)
            return ok

    def _post(self, title: str, content: str, template: str, level: str) -> bool:
        elapsed = time.monotonic() - self._last_sent
        if elapsed < self.MIN_INTERVAL:
            time.sleep(self.MIN_INTERVAL - elapsed)
        try:
            response = requests.post(
                self.url,
                json={'token': self.token, 'title': title, 'content': content, 'template': template, 'channel': 'wechat'},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            ok = int(payload.get('code') or 0) == 200
        except Exception:
            ok = False
        finally:
            self._last_sent = time.monotonic()
        return ok

    def _chunk(self, content: str) -> list[str]:
        if len(content) <= self.MAX_CONTENT_LEN:
            return [content]
        return [content[index:index + self.MAX_CONTENT_LEN] for index in range(0, len(content), self.MAX_CONTENT_LEN)]
