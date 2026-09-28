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
