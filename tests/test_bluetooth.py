"""`dial`'s Bluetooth output: finding the headphones by name, connecting
them, and the ffmpeg line that plays to them. No Bluetooth, no ffmpeg."""
import json

import pytest

from twiddle.dial import bluetooth, state
from twiddle.dial.bluetooth import BluetoothOutput, Paired
from twiddle.dial.output import Media, OutputState, Outputs, PlaybackError, bare
from twiddle.stations import STATIONS


@pytest.fixture(autouse=True)
def _dial_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "CACHE_DIR", tmp_path / "dial")


PROFILER = json.dumps({"SPBluetoothDataType": [{
    "device_connected": [
        {"Sam Rivera’s Mouse": {"device_address": "02:00:5E:00:00:01",
                                  "device_minorType": "Mouse"}},
        {"Bose QC45": {"device_address": "02:00:5E:00:00:04",
                       "device_minorType": "Headphones"}}],
    "device_not_connected": [
        {"Sam’s AirPods Pro": {"device_address": "02:00:5E:00:00:02",
                                  "device_minorType": "Headphones"}},
        {"iPad (9)": {"device_address": "02:00:5E:00:00:03"}}]}]})

# as ffmpeg 7 prints it on this Mac mini (2026-09-25), plus a Bluetooth line
DEVICES = """\
[AudioToolbox @ 0x600000910a50] CoreAudio devices:
[AudioToolbox @ 0x600000910a50] [0]                         (null), 4C2D1971-0000
[AudioToolbox @ 0x600000910a50] [1] NexiGo N930AF FHD Webcam Audio, AppleUSBAudioEngine:SHENZHEN AONI ELECTRONIC CO., LTD:NexiGo:3
[AudioToolbox @ 0x600000910a50] [2]              Mac mini Speakers, BuiltInSpeakerDevice
[AudioToolbox @ 0x600000910a50] [3]                    Bose QC45, 02-00-5E-00-00-04:output
"""


def fake_run(devices=DEVICES, calls=None):
    def run(argv, timeout=15):
        if calls is not None:
            calls.append(argv)
        if argv[0] == "system_profiler":
            return PROFILER
        if "-list_devices" in argv:
            return devices
        return ""
    return run


class Proc:
    def __init__(self):
        self.done = None

    def poll(self):
        return self.done

    def terminate(self):
        self.done = 0

    def wait(self, timeout=None):
        return 0


def test_only_paired_audio_devices_are_offered_connected_or_not():
    got = bluetooth.paired(fake_run())
    assert got == [Paired("Bose QC45", "02:00:5E:00:00:04", True),
                   Paired("Sam’s AirPods Pro", "02:00:5E:00:00:02", False)]
    labels = [label for _o, label, _d in bluetooth.choices(fake_run())]
    assert labels == ["Bose QC45  (Bluetooth)",
                      "Sam’s AirPods Pro  (Bluetooth, not connected)"]


def test_coreaudio_names_end_at_the_first_comma_and_compare_loosely():
    found = bluetooth.coreaudio_devices("ffmpeg", fake_run())
    assert found == {"nexigo n930af fhd webcam audio": 1, "mac mini speakers": 2,
                     "bose qc45": 3}
    assert bluetooth.norm("Sam’s  AirPods Pro") == bluetooth.norm("sam's airpods pro")


def test_plays_to_the_headphones_index_without_touching_the_macs_output():
    spawned = []
    out = BluetoothOutput("Bose QC45", "02:00:5E:00:00:04", run=fake_run(),
                          spawn=lambda argv, log: spawned.append(argv) or Proc(),
                          ffmpeg="/bin/ffmpeg", blueutil="")
    out.play(Media.of(STATIONS["kexp"]))
    argv = spawned[0]
    assert argv[argv.index("-f") + 1] == "audiotoolbox"
    assert argv[argv.index("-audio_device_index") + 1] == "3"
    assert STATIONS["kexp"].url in argv
    assert out.state() == OutputState(tuned="kexp", playing=True,
                                      volume=100, uri=bare(STATIONS["kexp"].url))
    out.stop()
    assert not out.state().playing


def test_not_connected_without_blueutil_says_how():
    out = BluetoothOutput("Bose QC45", "02:00:5E:00:00:04", run=fake_run(devices=""),
                          spawn=lambda *a: Proc(), ffmpeg="/bin/ffmpeg", blueutil="")
    with pytest.raises(PlaybackError) as e:
        out.play(Media.of(STATIONS["kexp"]))
    assert "isn't connected" in e.value.message and "blueutil" in e.value.hint


def test_blueutil_connects_then_it_plays():
    calls, answers = [], iter(["", "", DEVICES])

    def run(argv, timeout=15):
        calls.append(argv)
        return next(answers) if "-list_devices" in argv else ""
    spawned = []
    out = BluetoothOutput("Bose QC45", "02:00:5E:00:00:04", run=run,
                          spawn=lambda argv, log: spawned.append(argv) or Proc(),
                          ffmpeg="/bin/ffmpeg", blueutil="/bin/blueutil", sleep=lambda s: None)
    out.play(Media.of(STATIONS["kexp"]))
    assert ["/bin/blueutil", "--connect", "02-00-5E-00-00-04"] in calls
    assert spawned and "3" in spawned[0]


def test_bluetooth_has_its_own_live_volume_and_mute():
    """Issue #1 asked for the fix on every sink, not only the Mac's speakers."""
    class Pipe(Proc):
        def __init__(self):
            super().__init__()
            self.stdin = self
            self.written = []

        def write(self, data):
            self.written.append(data.decode())

        def flush(self):
            pass
    spawned, procs = [], []

    def spawn(argv, log):
        spawned.append(argv)
        procs.append(Pipe())
        return procs[-1]
    out = BluetoothOutput("Bose QC45", run=fake_run(), spawn=spawn, ffmpeg="/bin/ffmpeg")
    out.set_volume(40)                                  # before it plays: kept
    out.play(Media.of(STATIONS["kexp"]))
    assert "-nostdin" not in spawned[0]
    assert spawned[0][spawned[0].index("-af") + 1] == "volume@v=0.1600"
    out.set_volume(100)
    out.set_mute(True)
    assert procs[0].written == ["cvolume@v -1 volume 1.0000\n",
                                "cvolume@v -1 volume 0.0000\n"]
    assert out.state().volume == 100 and out.state().muted


def test_bluetooth_shows_up_in_the_picker_with_its_address():
    outs = Outputs(household_factory=lambda: type("H", (), {"groups": []})(),
                   bluetooth=lambda: bluetooth.choices(fake_run()))
    ids = [oid for oid, _l in outs.choices()]
    assert ids == ["mac", "bt:Bose QC45", "bt:Sam’s AirPods Pro"]
    bt = outs.get("bt:Bose QC45")
    assert isinstance(bt, BluetoothOutput) and bt.address == "02:00:5E:00:00:04"


def test_a_headset_listed_as_input_and_output_plays_to_the_output():
    """Seen on the author's Mac 2026-10-01: the Bose is two CoreAudio devices,
    and the microphone's index (first) made AudioQueueStart fail with -66637,
    so nothing ever played on the Bluetooth output."""
    devices = (
        "[AudioToolbox @ 0x1] CoreAudio devices:\n"
        "[AudioToolbox @ 0x1] [2]              Mac mini Speakers, BuiltInSpeakerDevice\n"
        "[AudioToolbox @ 0x1] [3]             Bose QC Headphones, E4-58-BC-77-5F-A8:input\n"
        "[AudioToolbox @ 0x1] [4]             Bose QC Headphones, E4-58-BC-77-5F-A8:output\n")
    assert bluetooth.coreaudio_devices("ffmpeg", fake_run(devices))["bose qc headphones"] == 4


def test_closing_and_the_sleep_timer_work_on_bluetooth():
    """BluetoothOutput's injected `sleep` once shadowed ProcessOutput's sleep
    timer, so close() raised AttributeError on real hardware."""
    out = BluetoothOutput("Bose QC45", run=fake_run(), spawn=lambda *a: Proc(),
                          ffmpeg="/bin/ffmpeg")
    out.play(Media.of(STATIONS["kexp"]))
    assert out.set_sleep_timer(600) == 600
    out.close()
    assert not out.state().playing and out.state().sleep_s is None
