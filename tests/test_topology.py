"""Topology parsing, including the bits that drive the diagnosis."""
from twiddle import topology

# Trimmed from this household's real GetZoneGroupState response: a Beam with
# two Play:1 surrounds, a Roam stereo pair, and one vanished device.
SAMPLE = """<ZoneGroupState><ZoneGroups>
<ZoneGroup Coordinator="RINCON_BEAM" ID="RINCON_BEAM:1803405565">
 <ZoneGroupMember UUID="RINCON_BEAM" Location="http://192.168.1.2:1400/xml/device_description.xml"
   ZoneName="Living Room" SoftwareVersion="97.1-80312" MinCompatibleVersion="96.0-00000"
   HTSatChanMapSet="RINCON_BEAM:LF,RF;RINCON_LR:LR" BootSeq="72" ChannelFreq="2462"
   WirelessMode="1" ConnectionType="5" EthLink="0">
  <Satellite UUID="RINCON_LR" Location="http://192.168.1.13:1400/xml/device_description.xml"
    ZoneName="Living Room" Invisible="1" SoftwareVersion="86.10-80260"
    MinCompatibleVersion="85.0-00000" BootSeq="69" ChannelFreq="2462" EthLink="0"/>
 </ZoneGroupMember>
</ZoneGroup>
<ZoneGroup Coordinator="RINCON_ROAML" ID="RINCON_ROAMR:2831555993">
 <ZoneGroupMember UUID="RINCON_ROAML" Location="http://192.168.1.4:1400/xml/device_description.xml"
   ZoneName="Sonos Roam" SoftwareVersion="97.1-80312" BootSeq="51" ChannelFreq="2462"
   EthLink="0" ChannelMapSet="RINCON_ROAML:LF,LF;RINCON_ROAMR:RF,RF"
   MoreInfo="RawBattPct:92,BattPct:100,BattChg:CHARGING,BattTmp:41"/>
</ZoneGroup>
</ZoneGroups><VanishedDevices>
<Device UUID="RINCON_ROAMR" ZoneName="Sonos Roam" Reason="UNKNOWN" ModelInfo="S27"
  Mac="02:00:5E:00:00:05" LastKnownIP="192.168.1.8" LastSeenUTC="2026-09-17T22:57:18Z"/>
</VanishedDevices></ZoneGroupState>"""


def test_groups_and_satellites():
    topo = topology.parse(SAMPLE)
    assert len(topo.groups) == 2
    beam = topo.by_uuid("RINCON_BEAM")
    assert beam.ip == "192.168.1.2"
    assert beam.coordinator_of  # it is its group's coordinator
    sat = topo.by_uuid("RINCON_LR")
    assert sat.is_satellite and sat.invisible
    assert sat.ip == "192.168.1.13"


def test_vanished_device_is_captured():
    topo = topology.parse(SAMPLE)
    assert len(topo.vanished) == 1
    v = topo.vanished[0]
    assert v.last_known_ip == "192.168.1.8"
    assert v.reason == "UNKNOWN"
    assert v.model == "S27"


def test_stereo_pair_detected_via_channel_map():
    topo = topology.parse(SAMPLE)
    roam = topo.by_uuid("RINCON_ROAML")
    assert "RINCON_ROAMR" in roam.chan_map
    assert "BattChg:CHARGING" in roam.more_info


def test_boot_seq_available_for_reboot_detection():
    topo = topology.parse(SAMPLE)
    # BootSeq is how we tell "rebooted" from "still running but disowned".
    assert topo.by_uuid("RINCON_BEAM").boot_seq == "72"
    assert topo.by_uuid("RINCON_LR").boot_seq == "69"
