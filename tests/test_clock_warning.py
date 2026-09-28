from rtmp_monitor.api import _clock_warning


def test_clock_warning_ignores_skew_inside_http_date_precision():
    result = _clock_warning(
        [("probe", {"clock": {
            "ntp_synchronized": True,
            "central_offset_ms": -1469.9,
            "central_offset_uncertainty_ms": 1000,
        }})],
        threshold_ms=1000,
    )

    assert result["warning"] is False
    assert result["maximum_absolute_offset_ms"] == 1469.9
    assert result["maximum_absolute_offset_lower_bound_ms"] == 469.9


def test_clock_warning_detects_skew_outside_http_date_precision():
    result = _clock_warning(
        [("probe", {"clock": {
            "ntp_synchronized": True,
            "central_offset_ms": -2501,
            "central_offset_uncertainty_ms": 1000,
        }})],
        threshold_ms=1000,
    )

    assert result["warning"] is True
    assert result["maximum_absolute_offset_lower_bound_ms"] == 1501


def test_clock_warning_accounts_for_uncertainty_when_comparing_probes():
    result = _clock_warning(
        [
            ("probe-a", {"clock": {"central_offset_ms": 0, "central_offset_uncertainty_ms": 1000}}),
            ("probe-b", {"clock": {"central_offset_ms": 3200, "central_offset_uncertainty_ms": 1000}}),
        ],
        threshold_ms=1000,
    )

    assert result["offset_spread_ms"] == 3200
    assert result["offset_spread_lower_bound_ms"] == 1200
    assert result["warning"] is True


def test_clock_warning_still_trusts_explicit_unsynchronized_status():
    result = _clock_warning(
        [("probe", {"clock": {"ntp_synchronized": False, "central_offset_ms": 100}})],
        threshold_ms=1000,
    )

    assert result["warning"] is True
    assert result["unsynchronized_agents"] == ["probe"]
