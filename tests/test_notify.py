"""Tests for drobo.notify: a best-effort, dependency-free ntfy client."""

from __future__ import annotations

from unittest.mock import Mock

from drobo import notify as notify_mod


def test_notify_is_noop_without_ntfy_url(monkeypatch):
    monkeypatch.delenv("NTFY_URL", raising=False)
    mock_urlopen = Mock()
    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", mock_urlopen)

    notify_mod.notify("title", "message")

    mock_urlopen.assert_not_called()


def test_notify_is_noop_with_empty_ntfy_url(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "")
    mock_urlopen = Mock()
    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", mock_urlopen)

    notify_mod.notify("title", "message")

    mock_urlopen.assert_not_called()


def test_notify_posts_with_headers_when_configured(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "http://example.invalid/test-topic")
    mock_urlopen = Mock()
    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", mock_urlopen)

    notify_mod.notify("Drobo Dashboard", "hello", priority="5", tags="rotating_light")

    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "http://example.invalid/test-topic"
    assert req.data == b"hello"
    assert req.headers["Title"] == "Drobo Dashboard"
    assert req.headers["Priority"] == "5"
    assert req.headers["Tags"] == "rotating_light"


def test_notify_swallows_exceptions(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "http://example.invalid/test-topic")
    mock_urlopen = Mock(side_effect=OSError("boom"))
    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", mock_urlopen)

    notify_mod.notify("title", "message")  # must not raise

    mock_urlopen.assert_called_once()
