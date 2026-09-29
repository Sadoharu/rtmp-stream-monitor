from rtmp_monitor.correlation import diagnose_observations


def report(role, name, status="OK", events=None, network=None):
    return {
        "role": role,
        "name": name,
        "status": status,
        "events": events or [],
        "metrics": {"network": network or {}},
    }


def broken(code="DECODE_ERROR", severity="CRITICAL"):
    return [{"code": code, "severity": severity, "details": {}}]


def test_source_failure_is_located_at_source():
    result = diagnose_observations([
        report("SOURCE", "encoder", "CRITICAL", broken()),
        report("SERVER_EGRESS", "server", "CRITICAL", broken()),
        report("CLIENT", "client", "CRITICAL", broken()),
    ])
    assert result["diagnosis"] == "SOURCE_OR_INGEST_PROBLEM"
    assert "SOURCE" in result["probable_location"]


def test_ingress_stall_correlated_with_egress_and_client_is_source_or_ingest_failure():
    ingress = report("SERVER_INGRESS", "ingress", "STREAM_STALLED", broken("STREAM_STALL"))
    ingress["metrics"].update({
        "srs_api_available": True,
        "ingress_quality": "PUBLISHER_COUNTERS_ONLY",
        "ingress_active": True,
    })
    result = diagnose_observations([
        ingress,
        report("SERVER_EGRESS", "server", "CRITICAL", broken("KEYFRAME_GAP")),
        report("CLIENT", "client", "CRITICAL", broken("KEYFRAME_GAP")),
    ])

    assert result["diagnosis"] == "SOURCE_OR_INGEST_PROBLEM"
    assert result["affected_agents"] == ["ingress", "server", "client"]


def test_server_restream_failure_requires_explicit_media_validated_ingress():
    ingress = report("SERVER_INGRESS", "ingress")
    ingress["metrics"]["ingress_quality"] = "MEDIA_VALIDATED"
    result = diagnose_observations([
        ingress,
        report("SERVER_EGRESS", "egress", "CRITICAL", broken("KEYFRAME_GAP")),
        report("CLIENT", "client", "CRITICAL", broken("KEYFRAME_GAP")),
    ])
    assert result["diagnosis"] == "RTMP_SERVER_RESTREAM_PROBLEM"


def test_ingress_without_explicit_quality_does_not_confirm_server_restream():
    result = diagnose_observations([
        report("SERVER_INGRESS", "ingress"),
        report("SERVER_EGRESS", "egress", "CRITICAL", broken("DECODE_ERROR")),
        report("CLIENT", "client", "CRITICAL", broken("DECODE_ERROR")),
    ])

    assert result["diagnosis"] == "RTMP_SERVER_RESTREAM_UNCONFIRMED"
    assert "no explicit decoded-media/GOP validation" in result["probable_location"]


def test_egress_only_failure_is_not_silently_ignored():
    ingress = report("SERVER_INGRESS", "ingress")
    ingress["metrics"]["ingress_quality"] = "MEDIA_VALIDATED"
    result = diagnose_observations([
        ingress,
        report("SERVER_EGRESS", "egress", "CRITICAL", broken("DECODE_ERROR")),
        report("CLIENT", "client"),
    ])
    assert result["diagnosis"] == "RTMP_SERVER_RESTREAM_PROBLEM"
    assert result["affected_agents"] == ["egress"]


def test_client_failure_with_retransmits_points_to_network():
    result = diagnose_observations([
        report("SERVER_INGRESS", "ingress"),
        report("SERVER_EGRESS", "egress"),
        report("CLIENT", "client", "CRITICAL", broken("FREEZE_START"), {"tcp_retransmissions": 47, "rtt_ms": 182}),
    ])
    assert result["diagnosis"] == "NETWORK_PATH_PROBLEM"
    assert "NETWORK" in result["probable_location"]


def test_windows_hostwide_retransmits_alone_do_not_prove_client_network_fault():
    result = diagnose_observations([
        report("SERVER_EGRESS", "egress"),
        report("CLIENT", "client", "CRITICAL", broken("FREEZE_START"), {
            "provider": "windows", "tcp_retransmissions": 1, "rtt_ms": 4,
            "packet_loss_percent": 0, "tcp_state": "ESTABLISHED",
        }),
    ])
    assert result["diagnosis"] == "NETWORK_PATH_UNCONFIRMED"
    assert "HOST-WIDE" in result["probable_location"]


def test_windows_retransmits_with_independent_rtt_spike_support_network_fault():
    result = diagnose_observations([
        report("SERVER_EGRESS", "egress"),
        report("CLIENT", "client", "CRITICAL", broken("FREEZE_START"), {
            "provider": "windows", "tcp_retransmissions": 1, "rtt_ms": 182,
            "packet_loss_percent": 0, "tcp_state": "ESTABLISHED",
        }),
    ])
    assert result["diagnosis"] == "NETWORK_PATH_PROBLEM"


def test_one_lost_icmp_echo_does_not_overattribute_a_client_media_error():
    result = diagnose_observations([
        report("SERVER_EGRESS", "egress"),
        report("CLIENT", "client", "CRITICAL", broken("FREEZE_START"), {
            "provider": "windows", "tcp_retransmissions": 0, "rtt_ms": 4,
            "packet_loss_percent": 100 / 3, "icmp_probe_count": 3, "icmp_reply_count": 2,
            "tcp_state": "ESTABLISHED",
        }),
    ])

    assert result["diagnosis"] == "CLIENT_PROBLEM"


def test_substantial_partial_icmp_loss_supports_network_fault():
    result = diagnose_observations([
        report("SERVER_EGRESS", "egress"),
        report("CLIENT", "client", "CRITICAL", broken("FREEZE_START"), {
            "provider": "windows", "tcp_retransmissions": 0, "rtt_ms": None,
            "packet_loss_percent": 200 / 3, "icmp_probe_count": 3, "icmp_reply_count": 1,
            "tcp_state": "ESTABLISHED",
        }),
    ])

    assert result["diagnosis"] == "NETWORK_PATH_PROBLEM"


def test_stale_linux_network_sample_does_not_blame_current_client_fault():
    result = diagnose_observations([
        report("SERVER_EGRESS", "egress"),
        report("CLIENT", "client", "CRITICAL", broken("FREEZE_START"), {
            "provider": "linux", "tcp_retransmissions": 4, "tcp_retransmissions_total": 12,
            "rtt_ms": 182, "packet_loss_percent": 66.7,
            "sample_age_seconds": 21, "sample_interval_seconds": 10,
        }),
    ])

    assert result["diagnosis"] == "CLIENT_PROBLEM"


def test_stale_windows_retransmits_do_not_even_mark_path_unconfirmed():
    result = diagnose_observations([
        report("SERVER_EGRESS", "egress"),
        report("CLIENT", "client", "CRITICAL", broken("FREEZE_START"), {
            "provider": "windows", "tcp_retransmissions": 1,
            "sample_age_seconds": 31, "sample_interval_seconds": 10,
        }),
    ])

    assert result["diagnosis"] == "CLIENT_PROBLEM"


def test_no_icmp_replies_alone_do_not_prove_network_fault():
    result = diagnose_observations([
        report("SERVER_EGRESS", "egress"),
        report("CLIENT", "client", "CRITICAL", broken("FREEZE_START"), {
            "provider": "windows", "tcp_retransmissions": 0, "rtt_ms": None,
            "packet_loss_percent": None, "icmp_probe_count": 3, "icmp_reply_count": 0,
            "tcp_state": "ESTABLISHED",
        }),
    ])

    assert result["diagnosis"] == "CLIENT_PROBLEM"


def test_legacy_icmp_loss_without_reply_count_is_not_network_proof():
    result = diagnose_observations([
        report("SERVER_EGRESS", "egress"),
        report("CLIENT", "client", "CRITICAL", broken("FREEZE_START"), {
            "provider": "windows", "tcp_retransmissions": 0, "rtt_ms": None,
            "packet_loss_percent": 100, "tcp_state": "ESTABLISHED",
        }),
    ])

    assert result["diagnosis"] == "CLIENT_PROBLEM"


def test_tcp_state_sample_before_media_recovery_does_not_blame_network():
    client = report("CLIENT", "client", "WARNING", broken("PTS_REGRESSION", "WARNING"), {
        "tcp_state": "not-established",
        "sample_age_seconds": 3.2,
    })
    client["metrics"]["last_frame_age"] = 0.1
    result = diagnose_observations([report("SERVER_EGRESS", "egress"), client])

    assert result["diagnosis"] == "CLIENT_PROBLEM"


def test_tcp_not_established_after_last_frame_supports_network_fault():
    client = report("CLIENT", "client", "CRITICAL", broken("FREEZE_START"), {
        "tcp_state": "not-established",
        "sample_age_seconds": 3.2,
    })
    client["metrics"]["last_frame_age"] = 6.2
    result = diagnose_observations([report("SERVER_EGRESS", "egress"), client])

    assert result["diagnosis"] == "NETWORK_PATH_PROBLEM"


def test_client_only_media_error_without_network_evidence_points_to_client():
    ingress = report("SERVER_INGRESS", "ingress")
    egress = report("SERVER_EGRESS", "egress")
    client = report("CLIENT", "client", "CRITICAL", broken("FREEZE_START"), {"tcp_retransmissions": 0, "rtt_ms": 4, "packet_loss_percent": 0})
    ingress["metrics"]["last_media_pts"] = 20
    egress["metrics"]["last_media_pts"] = 20
    client["metrics"]["last_media_pts"] = 20
    result = diagnose_observations([ingress, egress, client])
    assert result["diagnosis"] == "CLIENT_PROBLEM"


def test_light_bitstream_parse_error_is_a_client_media_symptom_not_a_decode_claim():
    result = diagnose_observations([
        report("SERVER_EGRESS", "egress"),
        report("CLIENT", "client", "WARNING", broken("BITSTREAM_PARSE_ERROR", "WARNING"), {
            "provider": "linux", "tcp_state": "ESTABLISHED", "tcp_retransmissions": 0,
        }),
    ])

    assert result["diagnosis"] == "CLIENT_PROBLEM"
    assert result["probable_location"].startswith("CLIENT RECEIVE / DECODER")


def test_stale_ffmpeg_progress_is_included_in_client_symptoms():
    result = diagnose_observations([
        report("CLIENT", "client", "WARNING", broken("PROGRESS_STALE", "WARNING")),
    ])
    assert result["diagnosis"] == "CLIENT_PATH_UNCONFIRMED"
    assert "SERVER_EGRESS IS NOT OBSERVED" in result["probable_location"]
    assert result["symptoms"][0]["events"][0]["code"] == "PROGRESS_STALE"


def test_media_pts_lag_can_support_network_path_diagnosis():
    egress = report("SERVER_EGRESS", "egress")
    egress["metrics"].update({"last_media_pts": 100, "profile": "LIGHT"})
    client = report("CLIENT", "client", "CRITICAL", broken("FREEZE_START"), {"tcp_retransmissions": 0, "rtt_ms": 4})
    client["metrics"].update({"last_media_pts": 90, "profile": "LIGHT"})
    result = diagnose_observations([egress, client])
    assert result["diagnosis"] == "NETWORK_PATH_PROBLEM"
    assert result["media_lags"][0]["lag_seconds"] == 10


def test_client_pts_ahead_of_egress_does_not_support_network_path_diagnosis():
    egress = report("SERVER_EGRESS", "egress")
    egress["metrics"].update({"last_media_pts": 100, "profile": "LIGHT"})
    client = report("CLIENT", "client", "CRITICAL", broken("FREEZE_START"), {"tcp_retransmissions": 0, "rtt_ms": 4})
    client["metrics"].update({"last_media_pts": 110, "profile": "LIGHT"})

    result = diagnose_observations([egress, client])

    assert result["diagnosis"] == "CLIENT_PROBLEM"
    assert result["media_lags"][0]["lag_seconds"] == -10


def test_missing_ingress_is_explicitly_unconfirmed():
    result = diagnose_observations([
        report("SERVER_EGRESS", "egress", "CRITICAL", broken()),
        report("CLIENT", "client", "CRITICAL", broken()),
    ])
    assert result["diagnosis"] == "UPSTREAM_OR_SERVER_UNCONFIRMED"
    assert "INGRESS OBSERVATION IS MISSING" in result["probable_location"]


def test_srs_publisher_counters_do_not_confirm_clean_media_before_restream():
    ingress = report("SERVER_INGRESS", "srs-ingress")
    ingress["metrics"].update({
        "srs_api_available": True,
        "ingress_quality": "PUBLISHER_COUNTERS_ONLY",
        "ingress_active": True,
        "ingress_recv_bytes": 10000,
        "ingress_recv_kbps_30s": 5000,
    })
    egress = report("SERVER_EGRESS", "egress", "CRITICAL", broken())

    result = diagnose_observations([ingress, egress])

    assert result["diagnosis"] == "RTMP_SERVER_RESTREAM_UNCONFIRMED"
    assert "does not validate decoded frames or GOPs" in result["probable_location"]


def test_unavailable_srs_api_is_not_misdiagnosed_as_source_failure():
    ingress = report("SERVER_INGRESS", "srs-ingress", "WARNING", [{"code": "SRS_API_UNAVAILABLE", "severity": "WARNING"}])
    ingress["metrics"]["srs_api_available"] = False
    egress = report("SERVER_EGRESS", "egress", "CRITICAL", broken())

    result = diagnose_observations([ingress, egress])

    assert result["diagnosis"] == "UPSTREAM_OR_SERVER_UNCONFIRMED"
    assert "TRUE SERVER_INGRESS OBSERVATION IS MISSING" in result["probable_location"]


def test_unavailable_srs_api_alone_does_not_create_a_stream_incident():
    ingress = report("SERVER_INGRESS", "srs-ingress", "WARNING", [{"code": "SRS_API_UNAVAILABLE", "severity": "WARNING"}])
    ingress["metrics"]["srs_api_available"] = False

    assert diagnose_observations([ingress]) is None


def test_media_timestamp_mismatch_downgrades_source_correlation():
    source = report("SOURCE", "encoder", "CRITICAL", broken())
    egress = report("SERVER_EGRESS", "server", "CRITICAL", broken())
    client = report("CLIENT", "client", "CRITICAL", broken())
    source["metrics"]["last_media_pts"] = 100
    egress["metrics"]["last_media_pts"] = 140
    client["metrics"]["last_media_pts"] = 141
    result = diagnose_observations([source, egress, client])
    assert result["diagnosis"] == "SOURCE_OR_INGEST_UNCONFIRMED"
    assert result["media_correlation"]["spread_seconds"] == 41.0
