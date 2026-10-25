from castlab.io import load_session
from castlab.signals import resample


def test_load_sorts_interleaved_rows_and_splits_markers(tmp_path):
    csv = tmp_path / "session_test.csv"
    csv.write_text(
        "timestamp_ns,type,x,y,z\n"
        "1040000000,ACCEL,0,0,9.8\n"
        "1000000000,ACCEL,0,0,9.8\n"
        "1020000000,ACCEL,1,0,9.8\n"
        "1010000000,GYRO,0,0,0\n"
        "1030000000,GYRO,0,1,0\n"
        "1025000000,MARK_CAST,,,\n"
        "1035000000,MARK_REEL,,,\n",
        encoding="utf-8",
    )
    (tmp_path / "session_test.meta.txt").write_text("start_elapsed_realtime_ns=1000000000\n", encoding="utf-8")
    s = load_session(csv)
    assert s.accel["t"].tolist() == [0.0, 0.02, 0.04]
    assert s.gyro["t"].tolist() == [0.01, 0.03]
    assert s.markers["type"].tolist() == ["CAST", "REEL"]
    assert s.markers["t"].tolist() == [0.025, 0.035]
    sig = resample(s, fs=100.0)
    # ACCEL と GYRO の重なる区間だけ
    assert sig.t[0] >= 0.01 and sig.t[-1] <= 0.03


def test_events_file_applies_undo_and_keeps_auto_cast_out_of_markers(tmp_path):
    csv = tmp_path / "session_20261101_120000.csv"
    rows = ["timestamp_ns,type,x,y,z"]
    for i in range(10):
        ts = 1_000_000_000 + i * 20_000_000
        rows += [f"{ts},ACCEL,0,0,9.8", f"{ts + 5_000_000},GYRO,0,0,0"]
    rows += ["1050000000,MARK_CAST,,,", "1100000000,MARK_REEL,,,"]
    csv.write_text("\n".join(rows) + "\n", encoding="utf-8")
    (tmp_path / "session_20261101_120000.meta.txt").write_text("start_elapsed_realtime_ns=1000000000\n", encoding="utf-8")
    (tmp_path / "session_20261101_120000.events.csv").write_text(
        "event_id,type,timestamp_ns,recorded_ns,ref_id,detail\n"
        "1,AUTO_CAST,1040000000,1090000000,,grid=52;accel_peak=30.0000\n"
        "2,CAST,1050000000,1050000000,,\n"
        "3,REEL,1100000000,1100000000,,\n"
        "4,UNDO,1120000000,1120000000,3,target_type=REEL\n"
        "5,FALSE,1130000000,1130000000,1,since_target_s=0.0900\n",
        encoding="utf-8",
    )
    s = load_session(csv)
    # 手動マーカーのみ。UNDO された REEL と AUTO_CAST は含まない
    assert s.markers["type"].tolist() == ["CAST"]
    assert s.markers["t"].tolist() == [0.05]
    auto = s.device_events("AUTO_CAST")
    assert auto["event_id"].tolist() == [1]
    assert auto["detail"].iloc[0]["accel_peak"] == "30.0000"
    assert s.device_events("FALSE")["ref_id"].tolist() == [1]
    assert s.device_events("REEL").empty
    assert len(s.device_events("REEL", active_only=False)) == 1


def test_find_sessions_skips_events_files(tmp_path):
    from castlab.io import find_sessions

    for name in ("session_20261101_120000.csv", "session_20261101_120000.events.csv", "other.csv"):
        (tmp_path / name).write_text("x\n", encoding="utf-8")
    assert [p.name for p in find_sessions([tmp_path])] == ["session_20261101_120000.csv"]


def test_device_params_are_restored_from_meta():
    from castlab.detector import DetectorParams
    from castlab.device import device_params

    meta = {f"detector_{k}": str(v) for k, v in DetectorParams().to_dict().items()}
    meta["detector_confirmation_delay_s"] = "8.0"  # パラメータ以外の detector_* 行は無視する
    assert device_params(meta) == DetectorParams()
    assert device_params({"start_elapsed_realtime_ns": "1"}) is None
