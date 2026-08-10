from shopsteward.editing.live_look import live_look_error, live_look_open


def test_gate_closed_without_flag(monkeypatch):
    monkeypatch.delenv("SHOPSTEWARD_LIVE_LOOK", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert live_look_open() is False


def test_gate_closed_without_key(monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_LIVE_LOOK", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert live_look_open() is False


def test_gate_open_with_both(monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_LIVE_LOOK", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert live_look_open() is True


def test_error_names_flag_and_env():
    msg = live_look_error()
    assert "SHOPSTEWARD_LIVE_LOOK" in msg and "OPENROUTER_API_KEY" in msg and "--live-look" in msg
