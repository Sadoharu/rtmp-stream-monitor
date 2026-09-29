from rtmp_monitor.analyzer import FrameAnalyzer, parse_diagnostic_line


def frame(n, pts, key=0, frame_type="P"):
    return f"[Parsed_showinfo_0 @ 0xabc] n: {n} pts: {n} pts_time:{pts:.6f} iskey:{key} type:{frame_type} checksum:00000000"


def test_keyframe_metadata_and_adaptive_gap():
    analyzer = FrameAnalyzer(configured_gap=2.0)
    analyzer.feed(frame(0, 0.0, 1, "I"), 100.0)
    analyzer.feed(frame(1, 0.04), 100.04)
    event = analyzer.check_keyframe_gap(102.1)
    assert event[0]["code"] == "KEYFRAME_GAP"
    recovered = analyzer.feed(frame(70, 2.8, 1, "I"), 102.8)
    assert any(item["code"] == "KEYFRAME_GAP_END" for item in recovered)
    assert analyzer.keyframe_count == 2


def test_pts_regression_is_reported():
    analyzer = FrameAnalyzer()
    analyzer.feed(frame(0, 2.0, 1, "I"), 1.0)
    events = analyzer.feed(frame(1, 1.0), 1.1)
    assert events[0]["code"] == "PTS_REGRESSION"


def test_i_picture_without_key_flag_is_not_counted_as_keyframe():
    analyzer = FrameAnalyzer()
    analyzer.feed(frame(0, 0.0, 0, "I"), 1.0)

    assert analyzer.last_frame_type == "I"
    assert analyzer.last_frame_is_keyframe is False
    assert analyzer.i_frame_count == 1
    assert analyzer.i_frames_without_key_flag == 1
    assert analyzer.keyframe_count == 0
    assert analyzer.current_gop_frames is None

    gap = analyzer.check_keyframe_gap(6.1)
    assert gap[0]["code"] == "KEYFRAME_GAP"
    assert gap[0]["details"]["no_keyframe_seen"] is True


def test_new_epoch_ignores_timestamp_reset_after_reconnect():
    analyzer = FrameAnalyzer()
    analyzer.feed(frame(0, 100.0, 1, "I"), 1.0)
    analyzer.feed(frame(1, 100.04), 1.04)

    analyzer.begin_new_epoch()
    events = analyzer.feed(frame(0, 0.0, 1, "I"), 2.0)

    assert events == []
    assert analyzer.frame_count == 3
    assert analyzer.keyframe_count == 2
    assert analyzer.current_gop_frames == 0
    assert analyzer.expected_gop_seconds is None


def test_ffmpeg_decode_diagnostics_are_structured():
    event = parse_diagnostic_line("[h264 @ 0x123] concealing 12 DC, 12 AC, 12 MV errors in I frame")
    assert event["code"] == "DECODE_ERROR"


def test_freeze_and_silence_durations_are_structured_from_ffmpeg_logs():
    freeze_start = parse_diagnostic_line("[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 2")
    freeze_duration = parse_diagnostic_line("[freezedetect @ 0x1] lavfi.freezedetect.freeze_duration: 3")
    freeze_end = parse_diagnostic_line("[freezedetect @ 0x1] lavfi.freezedetect.freeze_end: 5")
    silence_end = parse_diagnostic_line("[silencedetect @ 0x1] silence_end: 8 | silence_duration: 4")

    assert freeze_start["code"] == "FREEZE_START"
    assert freeze_start["details"]["value"] == 2.0
    assert freeze_duration["code"] == "FREEZE_DURATION"
    assert freeze_duration["details"]["duration_seconds"] == 3.0
    assert freeze_end["code"] == "FREEZE_END"
    assert freeze_end["details"]["value"] == 5.0
    assert silence_end["code"] == "SILENCE_END"
    assert silence_end["details"]["value"] == 8.0
    assert silence_end["details"]["duration_seconds"] == 4.0
