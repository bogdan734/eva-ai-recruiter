"""The hiring-paused switch lives in the shared state file, like calls_paused, so
the API and the admin bot see one value and a toggle needs no restart."""
import json

from src.bot import admin


def test_hiring_paused_is_read_live_from_the_shared_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(admin, "STATE_PATH", path)
    monkeypatch.setattr(admin, "_state", {})

    path.write_text(json.dumps({"hiring_paused": True}))
    assert admin.hiring_paused() is True

    path.write_text(json.dumps({"hiring_paused": False}))
    assert admin.hiring_paused() is False
