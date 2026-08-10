from shopsteward.editing import config


def test_look_llm_block_present():
    llm = config.load_look_llm()
    assert llm["provider"] == "openrouter"
    assert llm["model"] == "anthropic/claude-sonnet-4.5"
    assert llm["structured_output"] is False
    assert llm["monthly_soft_cap_usd"] > 0


def test_look_guard_block_present():
    g = config.load_look_guard()
    assert g["fallback_look"] == "bright-and-true"
    assert g["max_saturation_load"] > 0
