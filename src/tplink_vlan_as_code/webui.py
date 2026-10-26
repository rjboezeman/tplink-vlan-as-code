"""Minimal client for the TP-Link Easy Smart web UI (HTTP only), standard library only.

The switches have no API. State is embedded in pages as JavaScript literals
(`var qvlan_ds = {...};`) and changes are GET requests to `.cgi` endpoints.
Write responses do not say whether the change worked; callers must read back.

Protocol (Easy Smart "E" series web firmware):
  login     POST /logon.cgi             username, password, logon=Login
  identity  GET  /SystemInfoRpm.htm     var info_ds   = {hardwareStr:[..], firmwareStr:[..], macStr:[..], ...}
  vlans     GET  /Vlan8021QRpm.htm      var qvlan_ds  = {state, count, vids:[..], names:[..], tagMbrs:[..], untagMbrs:[..]}
  pvids     GET  /Vlan8021QPvidRpm.htm  var pvid_ds   = {pvids:[..]}
  links     GET  /PortSettingRpm.htm    var all_info  = {state:[1 enabled|0], spd_act:[0 no link|2..6 speed], ...}
  enable    GET  /qvlanSet.cgi          qvlan_en=1, qvlan_mode=Apply
  set vlan  GET  /qvlanSet.cgi          vid, vname, selType_<port>=0 untagged|1 tagged|2 not a member, qvlan_add=Add/Modify
  del vlan  GET  /qvlanSet.cgi          selVlans=<vid>, qvlan_del=Delete
  set pvid  GET  /vlanPvidSet.cgi       pbm=<port bitmask, bit 0 = port 1>, pvid
  logout    GET  /Logout.htm

Hardening: proxies are ignored (the password is clear text; it must not detour
through an HTTP_PROXY), redirects are refused, responses are size-capped, and
page data is parsed by a literal-only parser that cannot execute anything.
"""

from __future__ import annotations

import http.client
import http.cookiejar
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MAX_RESPONSE = 1 << 20  # 1 MiB; real pages are a few KiB


class WebUIError(Exception):
    pass


class Disconnected(WebUIError):
    """The switch closed the connection mid-request (it does this when restarting its web server)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WebUIError(f"switch answered with a redirect to {newurl!r}; refusing to follow")


class WebUI:
    """One admin session on one switch. Not thread-safe; the switch allows one session anyway."""

    def __init__(self, host: str, username: str, password: str, timeout: float = 15.0, port: int = 80):
        self.base = f"http://{host}:{port}/" if port != 80 else f"http://{host}/"
        self._credentials = {"username": username, "password": password, "logon": "Login"}
        self.timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )
        self.login()

    # -- transport
    def _request(self, path: str, query: dict | None = None, form: dict | None = None) -> str:
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        try:
            req = urllib.request.Request(url, data=data)  # noqa: S310 - scheme is always the http:// built in __init__
            with self._opener.open(req, timeout=self.timeout) as r:
                body = r.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as e:
            raise WebUIError(f"/{path}: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, (ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected)):
                raise Disconnected(f"/{path}: connection closed by switch") from None
            raise WebUIError(f"/{path}: {reason}") from None
        if len(body) > MAX_RESPONSE:
            raise WebUIError(f"/{path}: response larger than {MAX_RESPONSE} bytes")
        return body.decode("utf-8", errors="replace")

    @staticmethod
    def _is_login_page(text: str) -> bool:
        return "logon.cgi" in text

    def login(self) -> None:
        """Log in and prove it by reading a protected page. Never retried: switches lock out after failures."""
        text = self._request("logon.cgi", form=self._credentials)
        m = re.search(r"logonInfo\s*=\s*new\s+Array\s*\(\s*(\d+)", text)
        if m and m.group(1) != "0":
            raise WebUIError(f"login rejected (switch code {m.group(1)}): wrong credentials, or another admin session is active")
        if "info_ds" not in self._request("SystemInfoRpm.htm"):
            raise WebUIError("login did not produce a working session")

    def logout(self) -> None:
        self._request("Logout.htm")

    def read(self, page: str, var: str) -> Any:
        """Value of `var` on `page`, logging in again once if the session has expired."""
        text = self._request(page)
        if self._is_login_page(text) and var not in text:
            self.login()
            text = self._request(page)
        value = extract_var(text, var)
        if value is None:
            raise WebUIError(f"{page}: no '{var}' found (unsupported firmware or model?)")
        return value

    def write(self, cgi: str, params: dict) -> None:
        """Send a change. The switch may drop the connection while restarting its web server; that is not an error.
        Re-sending after an expired session is safe because every write sets an absolute value."""
        try:
            text = self._request(cgi, query=params)
        except Disconnected:
            return  # read-back will tell whether it took effect; the next read logs in again if needed
        if self._is_login_page(text):
            self.login()
            self._request(cgi, query=params)

    # -- typed operations
    def system_info(self) -> dict[str, str]:
        ds = _require_dict(self.read("SystemInfoRpm.htm", "info_ds"), "info_ds")
        return {k: _first_str(ds, k) for k in ("hardwareStr", "firmwareStr", "macStr")}

    def dot1q(self) -> tuple[bool, list[tuple[int, str, int, int]]]:
        """(802.1Q enabled, [(vid, name, tagged bitmask, untagged bitmask)])."""
        ds = _require_dict(self.read("Vlan8021QRpm.htm", "qvlan_ds"), "qvlan_ds")
        count = ds.get("count")
        cols = [ds.get(k) for k in ("vids", "names", "tagMbrs", "untagMbrs")]
        if not isinstance(count, int) or not all(isinstance(c, list) and len(c) >= count for c in cols):
            raise WebUIError("qvlan_ds: unexpected structure")
        rows = list(zip(*(c[:count] for c in cols), strict=True))
        if not all(isinstance(v, int) and isinstance(n, str) and isinstance(t, int) and isinstance(u, int) for v, n, t, u in rows):
            raise WebUIError("qvlan_ds: unexpected value types")
        return bool(ds.get("state")), rows

    def pvids(self) -> list[int]:
        ds = _require_dict(self.read("Vlan8021QPvidRpm.htm", "pvid_ds"), "pvid_ds")
        pv = ds.get("pvids")
        if not isinstance(pv, list) or not pv or not all(isinstance(p, int) for p in pv):
            raise WebUIError("pvid_ds: unexpected structure")
        return pv

    def port_status(self) -> list[tuple[bool, int]]:
        """[(port enabled, actual speed code)] per port. Speed code 0 means no link."""
        ds = _require_dict(self.read("PortSettingRpm.htm", "all_info"), "all_info")
        state, act = ds.get("state"), ds.get("spd_act")
        if not (isinstance(state, list) and isinstance(act, list) and state and len(act) >= len(state)
                and all(isinstance(x, int) for x in state + act)):
            raise WebUIError("all_info: unexpected structure")
        return [(bool(e), a) for e, a in zip(state, act[:len(state)], strict=True)]

    def enable_dot1q(self) -> None:
        self.write("qvlanSet.cgi", {"qvlan_en": "1", "qvlan_mode": "Apply"})

    def set_vlan(self, vid: int, name: str, tagged: list[int], untagged: list[int], port_count: int) -> None:
        params = {"vid": str(vid), "vname": name}
        for p in range(1, port_count + 1):
            params[f"selType_{p}"] = "1" if p in tagged else "0" if p in untagged else "2"
        params["qvlan_add"] = "Add/Modify"
        self.write("qvlanSet.cgi", params)

    def delete_vlan(self, vid: int) -> None:
        self.write("qvlanSet.cgi", {"selVlans": str(vid), "qvlan_del": "Delete"})

    def set_pvid(self, port: int, pvid: int) -> None:
        self.write("vlanPvidSet.cgi", {"pbm": str(1 << (port - 1)), "pvid": str(pvid)})


def _require_dict(v: Any, name: str) -> dict:
    if not isinstance(v, dict):
        raise WebUIError(f"{name}: expected an object")
    return v


def _first_str(ds: dict, key: str) -> str:
    v = ds.get(key)
    v = v[0] if isinstance(v, list) and v else v
    if not isinstance(v, str):
        raise WebUIError(f"info_ds.{key}: missing")
    return v.strip()


# --- JavaScript literal parser ------------------------------------------------------------------
# Accepts only: objects (identifier or string keys), arrays, `new Array(...)`, numbers (incl. hex),
# strings, true/false/null, and comments. Anything else is an error, never evaluated.

def extract_var(text: str, name: str) -> Any:
    """Value assigned by `var <name> = <literal>` in `text`, or None if there is no such assignment."""
    m = re.search(rf"\bvar\s+{re.escape(name)}\s*=", text)
    if not m:
        return None
    value, _ = _Parser(text, m.end()).value()
    return value


class _Parser:
    def __init__(self, text: str, pos: int):
        self.s, self.i = text, pos

    def fail(self, msg: str):
        raise WebUIError(f"cannot parse page data at offset {self.i}: {msg}")

    def skip(self) -> None:
        while True:
            while self.i < len(self.s) and self.s[self.i].isspace():
                self.i += 1
            if self.s.startswith("//", self.i):
                end = self.s.find("\n", self.i)
                self.i = len(self.s) if end < 0 else end
            elif self.s.startswith("/*", self.i):
                end = self.s.find("*/", self.i + 2)
                if end < 0:
                    self.fail("unterminated comment")
                self.i = end + 2
            else:
                return

    def peek(self) -> str:
        self.skip()
        return self.s[self.i] if self.i < len(self.s) else ""

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            self.fail(f"expected {ch!r}")
        self.i += 1

    def value(self) -> tuple[Any, int]:
        ch = self.peek()
        if ch == "{":
            return self.obj(), self.i
        if ch == "[":
            self.i += 1
            return self.items("]"), self.i
        if ch in "'\"":
            return self.string(), self.i
        m = re.compile(r"new\s+Array\s*\(").match(self.s, self.i)
        if m:
            self.i = m.end()
            return self.items(")"), self.i
        m = re.compile(r"-?(0[xX][0-9a-fA-F]+|\d+(\.\d+)?)").match(self.s, self.i)
        if m:
            self.i = m.end()
            t = m.group(0)
            return (int(t, 16) if "x" in t.lower() else float(t) if "." in t else int(t)), self.i
        for word, val in (("true", True), ("false", False), ("null", None)):
            if self.s.startswith(word, self.i):
                self.i += len(word)
                return val, self.i
        self.fail("unsupported value")

    def items(self, close: str) -> list:
        out = []
        while self.peek() != close:
            out.append(self.value()[0])
            if self.peek() == ",":
                self.i += 1
            elif self.peek() != close:
                self.fail(f"expected ',' or {close!r}")
        self.i += 1
        return out

    def obj(self) -> dict:
        self.expect("{")
        out = {}
        while self.peek() != "}":
            if self.peek() in "'\"":
                key = self.string()
            else:
                m = re.compile(r"[A-Za-z_$][\w$]*").match(self.s, self.i)
                if not m:
                    self.fail("expected a key")
                key, self.i = m.group(0), m.end()
            self.expect(":")
            out[key] = self.value()[0]
            if self.peek() == ",":
                self.i += 1
            elif self.peek() != "}":
                self.fail("expected ',' or '}'")
        self.i += 1
        return out

    def string(self) -> str:
        quote, self.i = self.s[self.i], self.i + 1
        out = []
        escapes = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "b": "\b", "f": "\f", "v": "\v"}
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == quote:
                self.i += 1
                return "".join(out)
            if c == "\\":
                n = self.s[self.i + 1:self.i + 2]
                if n in ("x", "u"):
                    width = 2 if n == "x" else 4
                    hexd = self.s[self.i + 2:self.i + 2 + width]
                    if not re.fullmatch(r"[0-9a-fA-F]+", hexd) or len(hexd) != width:
                        self.fail("bad escape")
                    out.append(chr(int(hexd, 16)))
                    self.i += 2 + width
                    continue
                out.append(escapes.get(n, n))
                self.i += 2
                continue
            if c == "\n":
                self.fail("newline in string")
            out.append(c)
            self.i += 1
        self.fail("unterminated string")
