"""Graph client: paging, 410, @removed, 429 handling."""

import pytest

from outlook_junk_filter import graph as graph_module
from outlook_junk_filter.graph import DeltaGone, GraphClient, GraphError, NetworkError


class Resp:
    def __init__(self, status=200, data=None, headers=None, content=b"", text=""):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.content = content
        self.text = text

    def json(self):
        return self._data


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(responses, sleeps=None):
    session = Session(responses)
    return GraphClient(lambda: "tok", session=session, sleep=(sleeps.append if sleeps is not None else lambda s: None)), session


def test_delta_follows_next_link_and_returns_delta_link_ignoring_removed():
    c, session = client(
        [
            Resp(data={"value": [{"id": "1"}, {"id": "2", "@removed": {"reason": "deleted"}}], "@odata.nextLink": "NEXT"}),
            Resp(data={"value": [{"id": "3"}], "@odata.deltaLink": "DELTA"}),
        ]
    )
    messages, link = c.delta(None)
    assert [m["id"] for m in messages] == ["1", "3"]
    assert link == "DELTA"
    assert session.requests[0][1] == graph_module.DELTA_URL
    assert session.requests[1][1] == "NEXT"


def test_delta_uses_saved_link_and_restarts_from_scratch_on_410():
    c, session = client([Resp(410), Resp(data={"value": [{"id": "9"}], "@odata.deltaLink": "D2"})])
    messages, link = c.delta("SAVED/delta?token=1")
    assert session.requests[0][1] == "SAVED/delta?token=1"
    assert session.requests[1][1] == graph_module.DELTA_URL
    assert link == "D2" and messages[0]["id"] == "9"


def test_429_waits_for_retry_after_then_retries():
    sleeps = []
    c, session = client([Resp(429, headers={"Retry-After": "7"}), Resp(data={"displayName": "Junk Email", "totalItemCount": 4})], sleeps)
    assert c.folder()["totalItemCount"] == 4
    assert sleeps == [7]
    assert len(session.requests) == 2


def test_503_without_retry_after_still_backs_off():
    sleeps = []
    c, _ = client([Resp(503), Resp(data={})], sleeps)
    c.folder()
    assert sleeps and sleeps[0] >= 1


def test_network_failure_is_a_network_error():
    import requests

    c, _ = client([requests.ConnectionError("down")])
    with pytest.raises(NetworkError):
        c.folder()


def test_http_error_is_a_graph_error():
    c, _ = client([Resp(403, text="denied")])
    with pytest.raises(GraphError) as exc:
        c.folder()
    assert exc.value.status == 403


def test_move_and_permanent_delete_calls():
    c, session = client([Resp(201, data={}), Resp(204)])
    c.move_to_deleted_items("A/B=")
    c.permanent_delete("A/B=")
    (m1, u1, kw1), (m2, u2, kw2) = session.requests
    assert (m1, kw1["json"]) == ("POST", {"destinationId": "deleteditems"})
    assert u1.endswith("/me/messages/A%2FB%3D/move")
    assert (m2, u2.endswith("/me/messages/A%2FB%3D/permanentDelete")) == ("POST", True)
    assert "json" not in kw2 or kw2["json"] is None


def test_raw_message_returns_bytes_and_410_elsewhere_is_a_plain_error():
    c, _ = client([Resp(content=b"MIME")])
    assert c.raw("X") == b"MIME"
    c, _ = client([Resp(410, text="gone")])
    with pytest.raises(GraphError):
        c.raw("X")


def test_delta_gone_twice_is_an_error():
    c, _ = client([Resp(410), Resp(410)])
    with pytest.raises(GraphError):
        c.delta("OLD/delta")


def test_delta_gone_is_exported():
    assert issubclass(DeltaGone, Exception)
