"""The shared live gain: anything that runs ffmpeg can use it."""
from twiddle.gain import LiveGain


class Stdin:
    def __init__(self, fail=None):
        self.data, self.fail = b"", fail

    def write(self, b):
        if self.fail:
            raise self.fail
        self.data += b

    def flush(self):
        pass


class Proc:
    def __init__(self, stdin):
        self.stdin = stdin


def test_unity_never_boosts_and_the_curve_is_squared():
    g = LiveGain()
    assert g.args() == ["-af", "volume@v=1.0000"]
    g.volume = 50
    assert g.factor == 0.25


def test_mute_is_silence_but_remembers_the_level():
    g = LiveGain(60)
    g.muted = True
    assert g.factor == 0.0 and g.volume == 60
    g.muted = False
    assert g.factor == 0.36


def test_push_writes_one_command_line():
    s = Stdin()
    assert LiveGain(50).push(Proc(s)) and s.data == b"cvolume@v -1 volume 0.2500\n"


def test_push_to_nothing_or_a_dead_process_is_not_an_error():
    g = LiveGain(50)
    assert not g.push(None) and not g.push(Proc(None))
    assert not g.push(Proc(Stdin(fail=BrokenPipeError())))
    assert not g.push(Proc(Stdin(fail=ValueError("closed"))))
