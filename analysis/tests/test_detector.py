import numpy as np
from dataclasses import replace

import pytest

from castlab.detector import DetectorParams, detect
from castlab.signals import Signals

FS = 50.0


def _signals(duration_s=60.0, spikes=(), gyro_bumps=()):
    t = np.arange(0, duration_s, 1 / FS)
    accel = np.zeros((len(t), 3))
    accel[:, 2] = 9.8
    gyro = np.zeros((len(t), 3))
    for ts, amp in spikes:
        accel[int(ts * FS), 0] = amp
    for ts, amp in gyro_bumps:
        i = int(ts * FS)
        gyro[i - 5 : i + 5, 1] = amp
    return Signals(t, FS, accel, gyro)


def test_detects_spike_with_gyro():
    sig = _signals(spikes=[(10.0, 30.0)], gyro_bumps=[(9.5, 8.0)])
    d = detect(sig, DetectorParams(accel_peak_min=25, gyro_peak_min=4))
    assert d["t"].tolist() == pytest.approx([10.0])


def test_requires_gyro():
    sig = _signals(spikes=[(10.0, 30.0)])
    assert detect(sig, DetectorParams(accel_peak_min=25, gyro_peak_min=4)).empty
    assert len(detect(sig, DetectorParams(accel_peak_min=25, gyro_peak_min=0))) == 1


def test_min_interval_keeps_largest_peak():
    sig = _signals(spikes=[(10.0, 30.0), (12.0, 40.0), (30.0, 28.0)])
    d = detect(sig, DetectorParams(accel_peak_min=25, gyro_peak_min=0, min_interval_s=8))
    assert d["t"].tolist() == pytest.approx([12.0, 30.0])


def test_overrides():
    p = DetectorParams().with_overrides(["accel_peak_min=30", "min_interval_s=5"])
    assert p.accel_peak_min == 30 and p.min_interval_s == 5
    assert p.gyro_peak_min == DetectorParams().gyro_peak_min
    with pytest.raises(ValueError):
        DetectorParams().with_overrides(["no_such_param=1"])


def test_post_still_filter():
    sig = _signals(spikes=[(10.0, 30.0), (30.0, 30.0)], gyro_bumps=[(9.5, 8.0), (29.5, 8.0)])
    # 2 つ目のピークの後だけ腕が動き続ける
    i0, i1 = int(30.5 * FS), int(33.0 * FS)
    sig.gyro[i0:i1, 0] = 3.0
    base = DetectorParams(accel_peak_min=25, gyro_peak_min=4, post_gyro_mean_max=0.0)
    assert len(detect(sig, base)) == 2
    d = detect(sig, replace(base, post_gyro_mean_max=1.0))
    assert d["t"].tolist() == pytest.approx([10.0])
    assert d["post_gyro_mean"].iloc[0] < 1.0


def test_suppression_is_local():
    # A(30)@0, B(32)@4, C(35)@8, 間隔 5 秒: A は B に、B は C に負ける。C だけ残る
    # （全体を |a| 順に貪欲に選ぶ方式だと A と C が残るが、それはストリーミングで再現できない）
    sig = _signals(spikes=[(10.0, 30.0), (14.0, 32.0), (18.0, 35.0)])
    d = detect(sig, DetectorParams(accel_peak_min=25, gyro_peak_min=0, min_interval_s=5))
    assert d["t"].tolist() == pytest.approx([18.0])
