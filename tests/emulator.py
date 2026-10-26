"""A local HTTP server speaking the Easy Smart web UI protocol, backed by the device rules.

Pages use the quirks seen in real firmware (unquoted keys, single-quoted strings,
`new Array(...)`, comments, line breaks inside arrays) so the client's parser is
exercised on realistic input. Write errors are swallowed silently, like the real
switch, which only shows a JavaScript alert.
"""

from __future__ import annotations

import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tplink_vlan_as_code.plan import DELETE_VLAN, ENABLE_DOT1Q, SET_PVID, SET_VLAN, Op, RuleError, apply_op
from tplink_vlan_as_code.state import SwitchState

LOGIN_PAGE = '<html><form method="post" action="logon.cgi"><input name="username"></form>' \
             "<script>var logonInfo = new Array(0, 0);</script></html>"


class SwitchEmulator:
    def __init__(self, state: SwitchState, mac="00-00-5e-00-53-10", hardware="TL-SG108PE 3.0",
                 password="hunter2", links=None):
        self.state, self.mac, self.hardware, self.password = state, mac, hardware, password
        self.links = links or [(1, 6 if p == 1 else 0) for p in range(1, state.port_count + 1)]
        self.session: str | None = None
        self.requests: list[tuple[str, str, str]] = []  # (method, path+query, body)
        self.drop_next_write = False
        self.redirect_everything = False
        emu = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                emu.requests.append(("POST", self.path, body))
                form = dict(urllib.parse.parse_qsl(body))
                if self.path == "/logon.cgi" and form.get("username") == "admin" and form.get("password") == emu.password:
                    emu.session = secrets.token_hex(8)
                    self.reply("<script>var logonInfo = new Array(\n0,\n0,0);</script>", cookie=emu.session)
                else:
                    self.reply("<script>var logonInfo = new Array(1,0,0);</script>")

            def do_GET(self):
                emu.requests.append(("GET", self.path, ""))
                if emu.redirect_everything:
                    self.send_response(302)
                    self.send_header("Location", "http://198.51.100.1/")
                    self.end_headers()
                    return
                url = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(url.query))
                if f"H_P_SSID={emu.session}" not in (self.headers.get("Cookie") or "") or emu.session is None:
                    return self.reply(LOGIN_PAGE)
                page = url.path.lstrip("/")
                if page.endswith(".cgi"):
                    if emu.drop_next_write:
                        emu.drop_next_write = False
                        emu.write(page, q)
                        self.close_connection = True
                        self.connection.close()  # like the switch restarting its web server
                        return
                    emu.write(page, q)
                    return self.reply("<script>var tip = '';</script>")
                if page == "Logout.htm":
                    emu.session = None
                    return self.reply(LOGIN_PAGE)
                self.reply(emu.page(page))

            def reply(self, text, cookie=None):
                data = text.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                if cookie:
                    self.send_header("Set-Cookie", f"H_P_SSID={cookie}; path=/")
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def page(self, name: str) -> str:
        s = self.state
        if name == "SystemInfoRpm.htm":
            return ('<script>\nvar info_ds = {\ndescriStr:[\n"TL-SG108PE"\n],\nmacStr:[\n"%s"\n],\nipStr:["192.0.2.10"],\n'
                    'hardwareStr:[\n"%s"\n],\nfirmwareStr:[\n"1.0.0 Build 20200415 Rel.54962"\n]\n};\n</script>'
                    % (self.mac, self.hardware))
        if name == "Vlan8021QRpm.htm":
            vids = sorted(s.vlans)
            def mask(ports):
                return sum(1 << (p - 1) for p in ports)
            return ("<script>\n/* 802.1Q */\nvar qvlan_ds = {\nstate:%d,\ncount:%d,\nvids:[%s],\nnames:[%s],\n"
                    "tagMbrs:[%s],\nuntagMbrs:[%s],\nlagIds:[0,0,0,0,0,0,0,0],\n};\n</script>" % (
                        int(s.dot1q_enabled), len(vids), ",".join(map(str, vids)),
                        ",".join("'%s'" % s.vlans[v].name for v in vids),
                        ",".join("0x%02x" % mask(s.vlans[v].tagged) for v in vids),
                        ",".join(str(mask(s.vlans[v].untagged)) for v in vids)))
        if name == "Vlan8021QPvidRpm.htm":
            return "<script>var pvid_ds = {\npvids:[%s], // per port\ncount:%d};</script>" % (
                ",".join(map(str, s.pvids)), s.port_count)
        if name == "PortSettingRpm.htm":
            return ("<script>var max_port_num = %d;\nvar all_info = {\nstate:[%s],\nspd_cfg:[%s],\nspd_act:[%s],\n"
                    "fc_cfg:[%s],\nfc_act:[%s],\ntrunk_info:[%s]\n};</script>" % (
                        s.port_count, ",".join(str(e) for e, _ in self.links), ",".join("1" for _ in self.links),
                        ",".join(str(a) for _, a in self.links), ",".join("0" for _ in self.links),
                        ",".join("0" for _ in self.links), ",".join("0" for _ in self.links)))
        return "<html>404</html>"

    def write(self, cgi: str, q: dict) -> None:
        ops = []
        if cgi == "qvlanSet.cgi" and q.get("qvlan_en") == "1":
            ops = [Op(ENABLE_DOT1Q)]
        elif cgi == "qvlanSet.cgi" and "qvlan_add" in q:
            sel = {int(k.split("_")[1]): v for k, v in q.items() if k.startswith("selType_")}
            ops = [Op(SET_VLAN, int(q["vid"]), q.get("vname", ""),
                      tuple(sorted(p for p, v in sel.items() if v == "1")),
                      tuple(sorted(p for p, v in sel.items() if v == "0")))]
        elif cgi == "qvlanSet.cgi" and "qvlan_del" in q:
            ops = [Op(DELETE_VLAN, int(q["selVlans"]))]
        elif cgi == "vlanPvidSet.cgi":
            pbm = int(q["pbm"])
            ops = [Op(SET_PVID, int(q["pvid"]), port=p) for p in range(1, self.state.port_count + 1) if pbm >> (p - 1) & 1]
        for op in ops:
            try:
                self.state = apply_op(self.state, op)
            except RuleError:
                pass  # the real switch shows an alert and changes nothing
