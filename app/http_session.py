from __future__ import annotations
import requests


class CountingSession(requests.Session):
    """A `requests.Session` that counts every HTTP request made through it.

    Used to measure how many calls we make to upstream (Leipzig) endpoints.
    `Session.get`/`post`/etc. all funnel through `request()`, so overriding it
    captures everything. Mailjet/Resend sends use their own `requests.post`,
    not this session, so they are deliberately not counted.
    """

    def __init__(self) -> None:
        super().__init__()
        self.request_count = 0

    def request(self, *args, **kwargs):  # type: ignore[override]
        self.request_count += 1
        return super().request(*args, **kwargs)
