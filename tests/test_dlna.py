from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tv_cast import app, dlna, live

LOCATION = "http://192.168.3.35:38237/upnp/dev/93f9/desc"
DESC = """<?xml version="1.0"?><root><device>
<friendlyName>TV-N52</friendlyName><manufacturer>SaluteTV</manufacturer>
<modelName>MSI MediaRenderer</modelName><UDN>uuid:93f900c7-a708</UDN><serviceList>
<service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
<controlURL>/upnp/control/rc</controlURL></service>
<service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
<controlURL>/upnp/control/avt</controlURL></service>
</serviceList></device></root>"""


def test_description_gives_the_renderer_and_its_controls():
    r = dlna.parse_description(LOCATION, DESC)
    assert r == dlna.Renderer(
        id="dlna:93f900c7-a708", name="TV-N52", model="SaluteTV DLNA", host="192.168.3.35",
        avt="http://192.168.3.35:38237/upnp/control/avt",
        rc="http://192.168.3.35:38237/upnp/control/rc")


def test_a_device_without_av_transport_is_not_a_renderer():
    assert dlna.parse_description(LOCATION, DESC.replace("AVTransport", "ContentDirectory")) is None


class Stream:
    def __init__(self, channel, quality="best"):
        self.channel, self.quality, self.key = channel, quality, "k"
        self.alive, self.stopped = True, False

    def start(self):
        pass

    def wait_ready(self, cancelled=lambda: False):
        return not cancelled()

    def stop(self):
        self.stopped, self.alive = True, False


@pytest.fixture
def tv(monkeypatch):
    """A renderer that answers with `state` and records the actions sent to it."""
    sent, state = [], {"now": "STOPPED"}

    def soap(url, action, body="", service=dlna.AVT, timeout=5):
        sent.append(action)
        if action == "GetTransportInfo":
            return f"<CurrentTransportState>{state['now']}</CurrentTransportState>"
        return ""

    clock = {"t": 1000.0}
    monkeypatch.setattr(dlna, "soap", soap)
    monkeypatch.setattr(dlna.time, "sleep", lambda s: None)
    monkeypatch.setattr(dlna.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(dlna, "local_ip_for", lambda host: "192.168.3.116")
    monkeypatch.setattr(live, "Stream", Stream)
    m = dlna.DlnaManager(lambda: "192.168.3.116")
    r = dlna.parse_description(LOCATION, DESC)
    m.renderers[r.id] = r
    return SimpleNamespace(m=m, id=r.id, sent=sent, state=state, clock=clock)


def test_play_live_hands_the_playlist_to_the_renderer(tv):
    s = tv.m.play_live(tv.id, "chan")
    assert s.url == "http://192.168.3.116:8765/live/k/live.m3u8"
    assert tv.sent == ["Stop", "SetAVTransportURI", "Play", "SetMute", "SetVolume"]
    assert tv.m.status(tv.id)["session"]["channel"] == "chan"


def test_ignored_first_command_is_sent_again(tv):
    tv.m.play_live(tv.id, "chan")
    tv.sent.clear()
    tv.clock["t"] += 10
    tv.m.check(tv.id)
    assert "SetAVTransportURI" not in tv.sent
    tv.clock["t"] += dlna.RESEND_EVERY
    tv.m.check(tv.id)
    assert "SetAVTransportURI" in tv.sent


def test_stop_on_the_tv_remote_ends_the_relay_after_a_grace(tv):
    s = tv.m.play_live(tv.id, "chan")
    tv.state["now"] = "PLAYING"
    tv.m.check(tv.id)
    tv.state["now"] = "STOPPED"
    tv.m.check(tv.id)
    tv.clock["t"] += 5
    tv.m.check(tv.id)
    assert tv.id in tv.m.sessions  # a reconnect blip
    tv.clock["t"] += dlna.STOPPED_GRACE
    tv.m.check(tv.id)
    assert tv.id not in tv.m.sessions and s.stream.stopped


def test_ended_broadcast_stops_the_tv_and_says_why(tv):
    s = tv.m.play_live(tv.id, "chan")
    s.stream.alive = False
    tv.sent.clear()
    tv.m.check(tv.id)
    assert tv.sent == ["Stop"]
    assert "закончилась" in tv.m.status(tv.id)["error"]


def test_stop_from_the_page(tv):
    s = tv.m.play_live(tv.id, "chan")
    tv.m.control(tv.id, "stop")
    assert s.stream.stopped and tv.m.sessions == {}


def test_salute_cast_twin_is_hidden_behind_its_dlna_side(monkeypatch):
    monkeypatch.setattr(app.manager, "devices", lambda: [
        {"id": "c1", "name": "TV-N52", "model": "TV-N52", "host": "192.168.3.35",
         "connected": False},
        {"id": "c2", "name": "Mi", "model": "MiTV", "host": "192.168.3.4", "connected": True},
    ])
    monkeypatch.setattr(app.renderers, "devices", lambda: [
        {"id": "dlna:x", "name": "TV-N52", "model": "SaluteTV DLNA", "host": "192.168.3.35"}])
    ids = [d["id"] for d in TestClient(app.app).get("/api/devices").json()]
    assert ids == ["c2", "dlna:x"]


def test_films_are_not_sent_to_a_dlna_tv():
    r = TestClient(app.app).post("/api/play", json={"device": "dlna:x", "item": "1"})
    assert r.status_code == 422
