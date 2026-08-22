from shopsteward.editing import config


def test_correction_knobs_present_and_typed():
    knobs = config.load_correction_knobs()
    assert knobs["exposure_max_stops"] == 1.5
    assert knobs["shadow_range_high"] == 35
    assert knobs["auto_white_balance"] is True


def test_look_prompt_has_description_slot():
    assert "{description}" in config.load_look_prompt()


def test_seed_looks_exist():
    names = {p.stem for p in config.LOOKS_DIR.glob("*.json")}
    assert {"bright-and-true", "national-geographic"} <= names
