"""The analysis rules, especially the ones that decide hardware vs setup."""
import json
from pathlib import Path

from twiddle import report, topology
from twiddle.devices import Device
from tests.test_topology import SAMPLE


def _devices():
    return {
        "192.168.1.4": Device(ip="192.168.1.4", model_number="S27",
                              zone_name="Sonos Roam (L)"),
        "192.168.1.8": Device(ip="192.168.1.8", model_number="S27",
                              zone_name="Sonos Roam (R)"),
    }


def test_ethernet_advice_demoted_when_no_cable_possible():
    topo = topology.parse(SAMPLE)
    on = report.analyse(topo, _devices(), ethernet_possible=True)
    off = report.analyse(topo, _devices(), ethernet_possible=False)
    sonosnet_on = next(f for f in on if "SonosNet" in f.title)
    sonosnet_off = next(f for f in off if "SonosNet" in f.title)
    # Telling someone to run a cable they cannot run should not be the headline.
    assert sonosnet_on.severity == "critical"
    assert sonosnet_off.severity == "info"


def test_similar_phy_rates_argue_against_a_failing_unit():
    topo = topology.parse(SAMPLE)
    rates = {"192.168.1.4": {"phy_per_sec": 75.0},
             "192.168.1.8": {"phy_per_sec": 68.0}}
    findings = report.analyse(topo, _devices(), rates)
    f = next(f for f in findings if "comparable radio conditions" in f.title)
    assert "argues against a hardware fault" in f.detail


def test_lopsided_phy_rates_prompt_a_swap_test():
    topo = topology.parse(SAMPLE)
    rates = {"192.168.1.4": {"phy_per_sec": 20.0},
             "192.168.1.8": {"phy_per_sec": 400.0}}
    findings = report.analyse(topo, _devices(), rates)
    f = next(f for f in findings if "Unequal radio conditions" in f.title)
    assert "SWAP" in f.discriminator


def test_phy_rates_are_not_compared_across_different_models():
    topo = topology.parse(SAMPLE)
    devices = {"192.168.1.2": Device(ip="192.168.1.2", model_number="S14"),
               "192.168.1.4": Device(ip="192.168.1.4", model_number="S27")}
    rates = {"192.168.1.2": {"phy_per_sec": 4000.0},
             "192.168.1.4": {"phy_per_sec": 70.0}}
    findings = report.analyse(topo, devices, rates)
    # A Beam and a Roam use different chipsets; 4000 vs 70 must not be read
    # as one unit being 57x worse than the other.
    assert not any("Unequal radio conditions" in f.title for f in findings)


def test_self_induced_drops_are_discounted(tmp_path: Path):
    mon = tmp_path / "monitor.jsonl"
    mon.write_text("\n".join([
        json.dumps({"kind": "sample", "samples": []}),
        json.dumps({"kind": "vanish", "ts": "2026-09-17T23:21:09.000Z",
                    "ip": "192.168.1.8", "name": "Sonos Roam",
                    "probe": {"http_ok": True}}),
    ]))
    (tmp_path / "interventions.jsonl").write_text(json.dumps(
        {"ts": "2026-09-17T23:21:12.122+00:00", "action": "set_uri",
         "ip": "192.168.1.4"}) + "\n")
    findings = report.summarise_log(mon)
    assert any("our own commands" in f.title for f in findings)
    assert not any("left the household" in f.title for f in findings)


def test_organic_drop_is_reported_as_a_fault(tmp_path: Path):
    mon = tmp_path / "monitor.jsonl"
    mon.write_text("\n".join([
        json.dumps({"kind": "sample", "samples": []}),
        json.dumps({"kind": "vanish", "ts": "2026-09-17T22:57:18.000Z",
                    "ip": "192.168.1.8", "name": "Sonos Roam",
                    "probe": {"http_ok": True}}),
    ]))
    findings = report.summarise_log(mon)
    f = next(f for f in findings if "left the household" in f.title)
    assert "lost the Sonos sync heartbeat" in f.detail


def test_high_tx_error_rate_is_flagged_and_prompts_swap():
    links = {
        "192.168.1.4": {"iface": "ath0", "tx_packets": 1_488_926,
                        "tx_errors": 231_527, "tx_error_pct": 15.55},
        "192.168.1.8": {"iface": "ath0", "tx_packets": 300_407,
                        "tx_errors": 5_918, "tx_error_pct": 1.97},
    }
    findings = report.tx_error_findings(links, _devices())
    f = next(f for f in findings if "failing to transmit" in f.title)
    assert f.severity == "critical"
    assert "SWAP" in f.discriminator


def test_normal_tx_error_rates_are_not_flagged():
    links = {
        "192.168.1.4": {"iface": "ath0", "tx_packets": 500_000,
                        "tx_errors": 5_000, "tx_error_pct": 1.0},
        "192.168.1.8": {"iface": "ath0", "tx_packets": 500_000,
                        "tx_errors": 10_000, "tx_error_pct": 2.0},
    }
    findings = report.tx_error_findings(links, _devices())
    assert not any("failing to transmit" in f.title for f in findings)


def test_bridge_only_models_are_excluded_from_tx_comparison():
    # The Play:1 surrounds expose no per-radio counters; bridge totals must
    # not be presented as radio errors.
    links = {"192.168.1.12": {"error": "radio counters not exposed",
                              "bridge_only": True, "iface": "br0"}}
    assert report.tx_error_findings(links, _devices()) == []


def test_events_inside_a_serving_span_are_discounted(tmp_path: Path):
    # A drop 90s after the last command, but while this machine was still
    # serving the audio, must not count as organic evidence.
    mon = tmp_path / "monitor.jsonl"
    mon.write_text("\n".join([
        json.dumps({"kind": "sample", "samples": []}),
        json.dumps({"kind": "vanish", "ts": "2026-09-17T23:40:54.000Z",
                    "ip": "192.168.1.4", "name": "Sonos Roam",
                    "probe": {"http_ok": True}}),
    ]))
    (tmp_path / "interventions.jsonl").write_text("\n".join([
        json.dumps({"ts": "2026-09-17T23:39:21.000+00:00",
                    "action": "serving_start", "ip": "192.168.1.4",
                    "span": True}),
        json.dumps({"ts": "2026-09-17T23:45:21.000+00:00",
                    "action": "serving_end", "ip": "192.168.1.4",
                    "span": True}),
    ]))
    findings = report.summarise_log(mon)
    assert any("our own commands" in f.title for f in findings)
    assert not any("left the household" in f.title for f in findings)


def test_event_outside_the_span_still_counts(tmp_path: Path):
    mon = tmp_path / "monitor.jsonl"
    mon.write_text("\n".join([
        json.dumps({"kind": "sample", "samples": []}),
        json.dumps({"kind": "vanish", "ts": "2026-09-17T23:27:59.000Z",
                    "ip": "192.168.1.8", "name": "Sonos Roam",
                    "probe": {"http_ok": True}}),
    ]))
    (tmp_path / "interventions.jsonl").write_text("\n".join([
        json.dumps({"ts": "2026-09-17T23:39:21.000+00:00",
                    "action": "serving_start", "ip": "192.168.1.4",
                    "span": True}),
        json.dumps({"ts": "2026-09-17T23:45:21.000+00:00",
                    "action": "serving_end", "ip": "192.168.1.4",
                    "span": True}),
    ]))
    findings = report.summarise_log(mon)
    assert any("left the household" in f.title for f in findings)


def test_unclosed_span_covers_everything_after_it(tmp_path: Path):
    # If a serving run is killed, its span never closes; anything after the
    # start is still confounded.
    mon = tmp_path / "monitor.jsonl"
    mon.write_text("\n".join([
        json.dumps({"kind": "sample", "samples": []}),
        json.dumps({"kind": "vanish", "ts": "2026-09-17T23:50:00.000Z",
                    "ip": "192.168.1.4", "name": "Sonos Roam",
                    "probe": {"http_ok": True}}),
    ]))
    (tmp_path / "interventions.jsonl").write_text(json.dumps(
        {"ts": "2026-09-17T23:39:21.000+00:00", "action": "serving_start",
         "ip": "192.168.1.4", "span": True}) + "\n")
    findings = report.summarise_log(mon)
    assert not any("left the household" in f.title for f in findings)


def test_analyse_reads_rotated_logs_oldest_first(tmp_path: Path):
    log = tmp_path / "daemon.jsonl"
    # .1 is the most recent predecessor, .2 older -- so read order is .2, .1, live.
    (tmp_path / "daemon.jsonl.2").write_text(json.dumps(
        {"kind": "sample", "samples": []}) + "\n")
    (tmp_path / "daemon.jsonl.1").write_text("\n".join([
        json.dumps({"kind": "sample", "samples": []}),
        json.dumps({"kind": "vanish", "ts": "2026-09-18T02:00:00.000Z",
                    "ip": "192.168.1.8", "name": "Sonos Roam",
                    "probe": {"http_ok": True}}),
    ]))
    log.write_text(json.dumps({"kind": "sample", "samples": []}) + "\n")

    assert [p.name for p in report.rotated_logs(log)] == [
        "daemon.jsonl.2", "daemon.jsonl.1", "daemon.jsonl"]

    # The drop lives in a rotated file and must still be found.
    with_rotated = report.summarise_log(log)
    assert any(f.title.startswith("Sonos Roam") and "left the household" in f.title
               for f in with_rotated)

    # Ignoring rotated files, the same log looks quiet. ("No speaker left the
    # household..." is the quiet finding -- match the per-speaker one only.)
    current = report.summarise_log(log, include_rotated=False)
    assert not any(f.title.startswith("Sonos Roam") and "left the household" in f.title
                   for f in current)
    assert any(f.title.startswith("No speaker left") for f in current)


def _snap(taken: str, speakers: dict) -> dict:
    return {"taken_utc": taken, "label": "", "speakers": speakers}


def test_baseline_diff_reports_windowed_rate():
    a = _snap("2026-09-17T23:35:30+00:00", {
        "192.168.1.4": {"zone_name": "Roam L", "serial": "S-L",
                        "model_number": "S27",
                        "link": {"tx_packets": 1_500_000, "tx_errors": 239_000}},
        "192.168.1.8": {"zone_name": "Roam R", "serial": "S-R",
                        "model_number": "S27",
                        "link": {"tx_packets": 300_000, "tx_errors": 6_100}},
    })
    b = _snap("2026-09-18T16:35:54+00:00", {
        "192.168.1.4": {"zone_name": "Roam L", "serial": "S-L",
                        "model_number": "S27",
                        "link": {"tx_packets": 2_670_000, "tx_errors": 332_000}},
        "192.168.1.8": {"zone_name": "Roam R", "serial": "S-R",
                        "model_number": "S27",
                        "link": {"tx_packets": 547_000, "tx_errors": 7_500}},
    })
    findings = report.diff_baselines([a, b])
    windowed = next(f for f in findings if "over just the interval" in f.title)
    # 93,000 / 1,170,000 = 7.95%, not the 12% the cumulative counter shows.
    assert "7.9" in windowed.detail
    crit = next(f for f in findings if "unacknowledged in this window" in f.title)
    # 7.95 / 0.58 = ~14x against its twin -- not a cross-chipset comparison.
    assert "14x worse" in crit.detail
    assert "S-L" in crit.detail


def test_baseline_diff_excludes_models_without_radio_counters():
    """Bridge-only models have no error count; absent data must not read 0%."""
    link = {"bridge_only": True, "iface": "br0", "tx_packets": 400_000}
    a = _snap("t1", {"192.168.1.12": {"zone_name": "Play:1", "link": dict(link)}})
    b = _snap("t2", {"192.168.1.12": {"zone_name": "Play:1",
                                      "link": dict(link, tx_packets=580_000)}})
    findings = report.diff_baselines([a, b])
    assert not any("Play:1" in f.detail for f in findings)


def test_baseline_diff_ignores_counter_resets():
    # A reboot zeroes the counters; a negative delta is not a 0% rate.
    a = _snap("t1", {"192.168.1.4": {"zone_name": "Roam L",
                                     "link": {"tx_packets": 2_000_000,
                                              "tx_errors": 300_000}}})
    b = _snap("t2", {"192.168.1.4": {"zone_name": "Roam L",
                                     "link": {"tx_packets": 5_000,
                                              "tx_errors": 40}}})
    assert report.diff_baselines([a, b]) == []


def test_baseline_diff_needs_two_snapshots():
    assert report.diff_baselines([_snap("t1", {})]) == []


def test_baseline_diff_never_compares_across_models():
    """A Roam must not be rated against a Beam: different WiFi chipsets."""
    a = _snap("t1", {
        "192.168.1.4": {"zone_name": "Roam L", "serial": "S-L",
                        "model_number": "S27",
                        "link": {"tx_packets": 1_000_000, "tx_errors": 100_000}},
        "192.168.1.2": {"zone_name": "Beam", "serial": "S-B",
                        "model_number": "S14",
                        "link": {"tx_packets": 300_000, "tx_errors": 0}},
    })
    b = _snap("t2", {
        "192.168.1.4": {"zone_name": "Roam L", "serial": "S-L",
                        "model_number": "S27",
                        "link": {"tx_packets": 2_000_000, "tx_errors": 200_000}},
        "192.168.1.2": {"zone_name": "Beam", "serial": "S-B",
                        "model_number": "S14",
                        "link": {"tx_packets": 640_000, "tx_errors": 0}},
    })
    crit = next(f for f in report.diff_baselines([a, b])
                if "unacknowledged in this window" in f.title)
    # With no S27 peer present, it must say so rather than invent a ratio
    # against the Beam's zero.
    assert "no same-model peer" in crit.detail
    assert "796x" not in crit.detail and "Beam" not in crit.detail


# ---- alarms: discounted like our own writes, whoever set them --------------
# 2026-10-05 is a Monday. The household is on Pacific time: PDT (UTC-7) until
# 2026-11-01 09:00Z, PST (UTC-8) after.
PDT, PST = -7 * 3600, -8 * 3600


def _alarm(start="07:00:00", recurrence="DAILY", enabled=True, duration="01:00:00",
           room="Bedroom", aid="1"):
    return {"id": aid, "start_time": start, "duration": duration,
            "recurrence": recurrence, "enabled": enabled,
            "room_uuid": "RINCON_00000000000101400", "room": room}


def _schedule(valid_from, alarms, offset=PDT, version="RINCON_00000000000101400:1",
              **kw):
    return {"kind": "alarm_schedule", "ts": valid_from, "valid_from": valid_from,
            "version": version, "utc_offset_s": offset, "alarms": alarms} | kw


def _vanish(ts, ip):
    return {"kind": "vanish", "ts": ts, "ip": ip, "name": ip,
            "probe": {"http_ok": True}}


def _scored(findings) -> dict[str, str]:
    """Each vanishing ip -> 'induced' or 'fault'."""
    out = {}
    for f in findings:
        ip = f.title.split(" (", 1)[0]
        if "right after" in f.title:
            out[ip] = "induced"
        elif "left the household" in f.title:
            out[ip] = "fault"
    return out


def _log(tmp_path: Path, records) -> Path:
    mon = tmp_path / "daemon.jsonl"
    mon.write_text("\n".join(json.dumps(r) for r in
                             [{"kind": "sample", "samples": []}, *records]) + "\n")
    return mon


def test_a_drop_at_an_alarms_fire_or_stop_is_induced_not_a_fault(tmp_path: Path):
    # Set in the Sonos app: there is no interventions.jsonl at all.
    mon = _log(tmp_path, [
        _schedule("2026-10-04T12:00:00.000Z", [
            _alarm("07:00:00", "WEEKDAYS"),
            _alarm("06:00:00", "DAILY", enabled=False, aid="2")]),
        _vanish("2026-10-05T14:00:20.000Z", "fire"),       # 07:00 PDT, Monday
        _vanish("2026-10-05T15:00:30.000Z", "stop"),       # + 01:00 duration
        _vanish("2026-10-05T14:30:00.000Z", "between"),
        _vanish("2026-10-06T14:00:00.000Z", "exact"),      # on the second
        _vanish("2026-10-10T14:00:20.000Z", "saturday"),   # not a weekday
        _vanish("2026-10-06T13:00:10.000Z", "disabled"),   # 06:00, alarm 2 is off
    ])
    assert not (tmp_path / "interventions.jsonl").exists()
    findings = report.summarise_log(mon)
    assert _scored(findings) == {"fire": "induced", "stop": "induced",
                                 "exact": "induced", "between": "fault", "saturday": "fault",
                                 "disabled": "fault"}
    fire = next(f for f in findings if f.title.startswith("fire "))
    assert "right after an alarm" in fire.title
    assert "alarm 07:00 on Bedroom, 20s away" in fire.detail
    stop = next(f for f in findings if f.title.startswith("stop "))
    assert "alarm 07:00 stop on Bedroom, 30s away" in stop.detail
    exact = next(f for f in findings if f.title.startswith("exact "))
    assert "alarm 07:00 on Bedroom, 0s away" in exact.detail


def test_a_drop_just_before_the_fire_time_is_induced(tmp_path: Path):
    # The vanish is logged before the fire time it is scored against: every
    # schedule has to be read before any event is.
    mon = _log(tmp_path, [
        _vanish("2026-10-05T13:59:30.000Z", "early"),
        _schedule("2026-10-04T12:00:00.000Z", [_alarm()]),
    ])
    assert _scored(report.summarise_log(mon)) == {"early": "induced"}


def test_an_alarm_changed_later_does_not_rescore_earlier_events(tmp_path: Path):
    mon = _log(tmp_path, [
        _schedule("2026-10-04T12:00:00.000Z", [_alarm("07:00:00", duration="02:00:00")]),
        _vanish("2026-10-05T14:00:20.000Z", "v1-fire"),
        # Disabled while it rang (07:30 PDT), and an 08:00 alarm added.
        _schedule("2026-10-05T14:30:00.000Z",
                  [_alarm("07:00:00", duration="02:00:00", enabled=False),
                   _alarm("08:00:00", aid="2")],
                  version="RINCON_00000000000101400:2"),
        _vanish("2026-10-05T16:00:10.000Z", "v1-stop"),       # its stop still counts
        _vanish("2026-10-06T14:00:20.000Z", "disabled-fire"),
        _vanish("2026-10-06T15:00:20.000Z", "added-fire"),
    ])
    assert _scored(report.summarise_log(mon)) == {
        "v1-fire": "induced", "v1-stop": "induced",
        "disabled-fire": "fault", "added-fire": "induced"}


def test_an_alarm_added_later_does_not_reach_back(tmp_path: Path):
    mon = _log(tmp_path, [
        _schedule("2026-10-04T12:00:00.000Z", [_alarm("07:00:00", duration="")]),
        _vanish("2026-10-05T15:00:20.000Z", "before-added"),    # 08:00 PDT
        _schedule("2026-10-05T20:00:00.000Z", [_alarm("08:00:00", aid="2")],
                  version="RINCON_00000000000101400:2"),
    ])
    assert _scored(report.summarise_log(mon)) == {"before-added": "fault"}


def test_the_checkpoint_keeps_an_alarm_discounted_after_rotation(tmp_path: Path):
    # The file that first recorded the schedule has rotated away; the live
    # file starts with a checkpoint carrying its original valid_from.
    log = tmp_path / "daemon.jsonl"
    (tmp_path / "daemon.jsonl.1").write_text(json.dumps(
        {"kind": "sample", "samples": []}) + "\n")
    log.write_text("\n".join(json.dumps(r) for r in [
        _schedule("2026-10-04T12:00:00.000Z", [_alarm()], checkpoint=True)
        | {"ts": "2026-10-09T03:00:00.000Z"},
        {"kind": "sample", "samples": []},
        _vanish("2026-10-09T14:00:20.000Z", "fire"),
    ]) + "\n")
    assert _scored(report.summarise_log(log)) == {"fire": "induced"}


def test_a_checkpoint_is_the_same_schedule_not_a_new_one(tmp_path: Path):
    # Read alongside the original, a checkpoint must not start a segment of
    # its own (here it would wrongly re-enable an alarm disabled since).
    original = _schedule("2026-10-04T12:00:00.000Z", [_alarm()])
    mon = _log(tmp_path, [
        original,
        _schedule("2026-10-06T20:00:00.000Z", [_alarm(enabled=False)],
                  version="RINCON_00000000000101400:2"),
        original | {"checkpoint": True, "ts": "2026-10-07T00:00:00.000Z"},
        _vanish("2026-10-08T14:00:20.000Z", "after-disable"),
    ])
    assert _scored(report.summarise_log(mon)) == {"after-disable": "fault"}


def test_fire_windows_follow_the_household_clock_across_dst(tmp_path: Path):
    mon = _log(tmp_path, [
        _schedule("2026-10-30T12:00:00.000Z", [_alarm()], offset=PDT),
        # Same version: only the offset changed, at 02:00 PDT on 2026-11-01.
        _schedule("2026-11-01T09:00:30.000Z", [_alarm()], offset=PST),
        _vanish("2026-10-31T14:00:20.000Z", "pdt-fire"),    # 07:00 PDT
        _vanish("2026-11-02T15:00:20.000Z", "pst-fire"),    # 07:00 PST
        _vanish("2026-11-02T14:00:20.000Z", "pst-0600"),    # 06:00 PST: nothing
    ])
    assert _scored(report.summarise_log(mon)) == {
        "pdt-fire": "induced", "pst-fire": "induced", "pst-0600": "fault"}


def test_a_once_alarm_fires_on_whatever_day_comes_next(tmp_path: Path):
    mon = _log(tmp_path, [
        _schedule("2026-10-04T12:00:00.000Z", [_alarm(recurrence="ONCE")]),
        _vanish("2026-10-05T14:00:20.000Z", "once"),
        # The speaker disables it once it has fired: a new version.
        _schedule("2026-10-05T14:01:00.000Z", [_alarm(recurrence="ONCE", enabled=False)],
                  version="RINCON_00000000000101400:2"),
        _vanish("2026-10-06T14:00:20.000Z", "next-day"),
    ])
    assert _scored(report.summarise_log(mon)) == {"once": "induced", "next-day": "fault"}
