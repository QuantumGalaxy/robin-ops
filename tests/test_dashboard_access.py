import http.client
import json
import os
import stat
from threading import Thread

import pytest

from optionsagent.config import Config
from optionsagent.webserver import dashboard_token, make_server


def test_dashboard_link_survives_restart_and_key_is_private(tmp_path):
    first = dashboard_token(tmp_path)
    assert dashboard_token(tmp_path) == first
    assert len(first) == 43
    assert stat.S_IMODE((tmp_path / "dashboard-access.key").stat().st_mode) == 0o600
    (tmp_path / "dashboard-access.key").write_text("broken")
    with pytest.raises(ValueError, match="Invalid"):
        dashboard_token(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
def test_shared_access_key_is_rejected(tmp_path):
    dashboard_token(tmp_path)
    (tmp_path / "dashboard-access.key").chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        dashboard_token(tmp_path)


def test_cookie_restores_access_and_survives_server_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("optionsagent.webserver.state", lambda cfg: {"paper": True})
    cfg = Config(state_dir=str(tmp_path), risk={"kill_switch_file": str(tmp_path / "KILL")})
    server, token = make_server(cfg, port=0)
    port = server.server_port

    def start(s):
        thread = Thread(target=s.serve_forever, daemon=True)
        thread.start()
        return thread

    def request(method, path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    thread = start(server)
    origin = f"http://127.0.0.1:{port}"
    try:
        assert request("GET", "/api/state")[0] == 403
        assert request("GET", "/api/report?date=2026-09-08")[0] == 403
        assert request("POST", "/api/session")[0] == 403
        auth = {"Authorization": "Bearer " + token}
        assert request("GET", "/api/report?date=bad", auth)[0] == 400
        assert request("GET", "/api/report?date=2026-09-08", auth)[0] == 200
        assert request("POST", "/api/session", {**auth, "Origin": "https://evil.test"})[0] == 403
        status, headers, _ = request("POST", "/api/session", {**auth, "Origin": origin})
        assert status == 200
        full_cookie = headers["Set-Cookie"]
        assert "HttpOnly" in full_cookie and "SameSite=Strict" in full_cookie
        cookie = {"Cookie": full_cookie.split(";", 1)[0]}
        # Bare URL reload: no fragment or browser storage, just the session cookie.
        assert request("GET", "/api/state", cookie)[0] == 200
        assert request("GET", "/api/state", {**cookie, "Host": "evil.test"})[0] == 403
        assert request("GET", "/api/state", {**cookie, "Sec-Fetch-Site": "cross-site"})[0] == 403
        assert request("POST", "/api/control", cookie, json.dumps({"action": "pause"}))[0] == 403
        assert (
            request(
                "POST",
                "/api/control",
                {**cookie, "Origin": "http://127.0.0.1:1"},
                json.dumps({"action": "pause"}),
            )[0]
            == 403
        )
        assert (
            request(
                "POST",
                "/api/control",
                {**cookie, "Origin": origin},
                json.dumps({"action": "pause"}),
            )[0]
            == 200
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    server, restarted_token = make_server(cfg, port=port)
    assert restarted_token == token
    thread = start(server)
    try:
        assert request("GET", "/api/state", cookie)[0] == 200
        assert request("GET", "/api/state", {"Cookie": f"robin_desk_{port}=invalid"})[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_https_proxy_origin_secure_cookie_and_csrf(tmp_path, monkeypatch):
    monkeypatch.setenv('ROBIN_OPS_DASHBOARD_ORIGIN', 'https://desk.example.com')
    monkeypatch.setattr('optionsagent.webserver.state', lambda cfg: {'paper': True})
    server, token = make_server(Config(state_dir=str(tmp_path)), port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(headers):
        c = http.client.HTTPConnection('127.0.0.1', server.server_port)
        try:
            c.request('POST', '/api/session', headers=headers)
            r = c.getresponse()
            result = r.status, r.getheader('Set-Cookie')
            r.read()
            return result
        finally:
            c.close()
    try:
        headers = {'Host': 'desk.example.com', 'Origin': 'https://desk.example.com',
                   'Authorization': 'Bearer ' + token}
        status, cookie = request(headers)
        assert status == 200 and '; Secure' in cookie
        assert request({**headers, 'Origin': 'https://evil.example'})[0] == 403
        assert request({**headers, 'Host': 'evil.example'})[0] == 403
        assert request({'Host': 'desk.example.com'})[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
