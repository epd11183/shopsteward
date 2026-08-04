from shopsteward.adapters.pod.interface import PodWriteError


def test_write_error_truncates_long_message() -> None:
    # mirrors tests/adapters/etsy/test_live.py's
    # test_write_error_truncates_long_body -- EtsyWriteError precedent.
    err = PodWriteError(400, "x" * 5000)
    assert len(str(err)) < 1000


def test_write_error_carries_status_code_and_none_error_is_fine() -> None:
    err = PodWriteError(404, None)
    assert err.status_code == 404
    assert "404" in str(err)
