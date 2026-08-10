from shopsteward.adapters.look.fake import FakeLookAdapter, FixtureLookAdapter
from shopsteward.adapters.look.interface import LookProfile, LookResult
from shopsteward.core.db import connect, migrate
from shopsteward.editing import looks
from shopsteward.editing.config import LOOKS_DIR

USER = 1


def _conn():
    c = connect(":memory:")
    migrate(c)
    return c


def test_seed_and_get():
    c = _conn()
    n = looks.seed(c, USER, LOOKS_DIR)
    assert n >= 2
    lp = looks.get_look(c, USER, "national-geographic")
    assert lp.contrast == 18


def test_seed_is_idempotent():
    c = _conn()
    looks.seed(c, USER, LOOKS_DIR)
    assert looks.seed(c, USER, LOOKS_DIR) == 0


def test_save_is_last_write_wins():
    c = _conn()
    looks.save_look(c, USER, LookProfile(name="x", contrast=1))
    looks.save_look(c, USER, LookProfile(name="x", contrast=9))
    assert looks.get_look(c, USER, "x").contrast == 9


def test_resolve_named_look_does_not_call_llm():
    c = _conn()
    looks.seed(c, USER, LOOKS_DIR)
    adapter = FakeLookAdapter([])  # would raise if called
    lp = looks.resolve_look(c, USER, "bright-and-true", adapter, model="m", regenerate=False)
    assert lp.name == "bright-and-true"


def test_resolve_description_generates_then_reloads():
    c = _conn()
    adapter = FixtureLookAdapter()
    first = looks.resolve_look(c, USER, "cinematic mexico", adapter, model="m", regenerate=False)
    reload_adapter = FakeLookAdapter([])
    again = looks.resolve_look(c, USER, "cinematic mexico", reload_adapter, model="m", regenerate=False)
    assert again.model_dump() == first.model_dump()


def test_regenerate_forces_new_call():
    c = _conn()
    looks.resolve_look(c, USER, "cinematic mexico", FixtureLookAdapter(), model="m", regenerate=False)
    forced = LookResult(profile=LookProfile(name="forced", contrast=77))
    out = looks.resolve_look(c, USER, "cinematic mexico", FakeLookAdapter([forced]), model="m", regenerate=True)
    assert out.contrast == 77
