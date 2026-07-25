from __future__ import annotations

import asyncio
import base64
import codecs
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import re
import urllib.parse
import uuid

import requests

CHAT_HOST = "127.0.0.1"
CHAT_DOMAIN = "deceive-localhost.molenzwiebel.xyz"

RIOT_CONFIG_URL = "https://clientconfig.rpg.riotgames.com"

GEO_PAS_URL = "https://riot-geo.pas.si.riotgames.com/pas/v1/service/chat"

CERT_URL = os.getenv(
    "SCOUT_OFFLINE_CERT_URL", "https://mln.cx/deceive/localhost.pfx"
)
_CACHED_CERT = os.path.join(
    os.getenv("LOCALAPPDATA", os.path.expanduser("~")),
    "ValorantScout", "offline", "chat.pem",
)
_CERT_CACHE_TTL = 7 * 86400

CERT_PATH = os.getenv(
    "SCOUT_OFFLINE_CERT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "offline_chat.pem"),
)

RIOT_INSTALLS = os.path.join(
    os.getenv("PROGRAMDATA", r"C:\ProgramData"),
    "Riot Games", "RiotClientInstalls.json",
)

_LOG_PATH = os.path.join(
    os.getenv("LOCALAPPDATA", os.path.expanduser("~")),
    "ValorantScout", "offline", "engine.log",
)


_HTTP_SESSION = None
_HTTP_LOCK = threading.Lock()


def _http():
    global _HTTP_SESSION
    with _HTTP_LOCK:
        if _HTTP_SESSION is None:
            _HTTP_SESSION = requests.Session()
        return _HTTP_SESSION


def _dbg(msg: str, echo: bool = False) -> None:
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass
    if echo:
        try:
            print(f"[offline] {msg}", flush=True)
        except Exception:
            pass


def _reset_log() -> None:
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        open(_LOG_PATH, "w", encoding="utf-8").close()
    except Exception:
        pass


_VALID_STATUS = ("online", "offline", "away", "mobile")
_DEFAULT_STATUS = "offline"
_STATUS_PATH = os.path.join(
    os.getenv("LOCALAPPDATA", os.path.expanduser("~")),
    "ValorantScout", "offline", "status",
)


def _load_status() -> str:
    try:
        s = open(_STATUS_PATH, encoding="utf-8").read().strip().lower()
        return s if s in _VALID_STATUS else _DEFAULT_STATUS
    except Exception:
        return _DEFAULT_STATUS


def _save_status(status: str) -> None:
    try:
        os.makedirs(os.path.dirname(_STATUS_PATH), exist_ok=True)
        with open(_STATUS_PATH, "w", encoding="utf-8") as f:
            f.write(status)
    except Exception:
        pass


def _roster_name(status: str) -> str:
    return f"{status.capitalize()} Mode Active"


def _status_line(status: str) -> str:
    return {
        "online": "Valorant Scout paused — you're now appearing ONLINE again.",
        "offline": "You're now appearing OFFLINE to your friends.",
        "away": "You're now appearing AWAY (idle) to your friends.",
        "mobile": "You're now appearing on MOBILE to your friends.",
    }.get(status, f"You're now appearing {status}.")

_RIOT_PROCS = [
    "RiotClientServices.exe",
    "VALORANT.exe",
    "VALORANT-Win64-Shipping.exe",
    "RiotClientCrashHandler.exe",
]


_GAME_ELEMENTS = (
    "valorant", "league_of_legends", "bacon", "lion", "keystone", "riot_client",
)


def _rewrite_presence(xml_text: str, target: str = "offline") -> str:
    if "<presence" not in xml_text:
        return xml_text

    open_tag = xml_text[: xml_text.find(">") + 1]
    if re.search(r"\bto=", open_tag):
        return xml_text

    s = xml_text
    if "<show>" in s:
        s = re.sub(r"<show>.*?</show>", f"<show>{target}</show>", s,
                   count=1, flags=re.DOTALL)
    else:
        s = re.sub(r"<show\s*/>", f"<show>{target}</show>", s, count=1)
    s = re.sub(r"<status>.*?</status>", "", s, flags=re.DOTALL)
    s = re.sub(r"<status\s*/>", "", s)
    for tag in _GAME_ELEMENTS:
        s = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", "", s, flags=re.DOTALL)
        s = re.sub(rf"<{tag}\b[^>]*/>", "", s)
    if s != xml_text:
        _dbg(f"presence: offline-rewrote ({len(xml_text)}->{len(s)}b)")
    return s


def _is_directed(stanza: str) -> bool:
    return bool(re.search(r"\bto=", stanza[: stanza.find(">") + 1]))


def process_c2s(buf: str, target: str = "offline", on_presence=None, rewrite: bool = True):
    out = []
    i = 0
    while True:
        start = buf.find("<presence", i)
        if start == -1:
            tail = _pending_prefix(buf, i)
            out.append(buf[i:len(buf) - len(tail)])
            return "".join(out), tail
        out.append(buf[i:start])
        end = _presence_end(buf, start)
        if end == -1:
            return "".join(out), buf[start:]
        raw = buf[start:end]
        if on_presence is not None and not _is_directed(raw):
            on_presence(raw)
        out.append(_rewrite_presence(raw, target) if rewrite else raw)
        i = end


def _element_end(buf: str, start: int, name: str) -> int:
    gt = buf.find(">", start)
    if gt == -1:
        return -1
    if buf[gt - 1] == "/":
        return gt + 1
    close = buf.find(f"</{name}>", gt)
    if close == -1:
        return -1
    return close + len(name) + 3


def _presence_end(buf: str, start: int) -> int:
    return _element_end(buf, start, "presence")


def _pending_prefix(buf: str, i: int) -> str:
    tag = "<presence"
    tail = buf[max(i, len(buf) - len(tag)):]
    for k in range(len(tail), 0, -1):
        if tag.startswith(tail[-k:]):
            return tail[-k:]
    return ""


_FAKE_PUUID = "5ca07a5c-0ff1-4c0d-9e00-000000000001"
_FAKE_JID = f"{_FAKE_PUUID}@eu1.pvp.net"
_FAKE_RES = "RC-Scout"

_ROSTER_MARKER = b"<query xmlns='jabber:iq:riotgames:roster'>"

_FAKE_ROSTER_ITEM = (
    f"<item jid='{_FAKE_JID}' name='&#9;Valorant Scout Active' subscription='both' puuid='{_FAKE_PUUID}'>"
    "<group priority='9999'>Valorant Scout</group>"
    "<state>online</state>"
    "<id name='&#9;Valorant Scout Active' tagline='OFFLINE'/>"
    "<lol name='&#9;Valorant Scout Active'/>"
    "<platforms><riot name='&#9;Valorant Scout Active' tagline='OFFLINE'/></platforms>"
    "</item>"
).encode("utf-8")


def inject_fake_roster(data: bytes) -> bytes | None:
    idx = data.find(_ROSTER_MARKER)
    if idx == -1:
        return None
    pos = idx + len(_ROSTER_MARKER)
    return data[:pos] + _FAKE_ROSTER_ITEM + data[pos:]


def strip_fake_stanzas(text: str) -> str:
    out = []
    i = 0
    while True:
        start, name = -1, ""
        for tag in ("message", "iq", "presence"):
            k = text.find(f"<{tag}", i)
            if k != -1 and (start == -1 or k < start):
                start, name = k, tag
        if start == -1:
            break
        end = _element_end(text, start, name)
        if end == -1:
            break
        out.append(text[i:start] if _FAKE_PUUID in text[start:end] else text[i:end])
        i = end
    out.append(text[i:])
    return "".join(out)


def _extract_valorant_version(text: str) -> str | None:
    m = re.search(r"<valorant\b[^>]*>.*?<p>([A-Za-z0-9+/=]+)</p>.*?</valorant>",
                  text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(base64.b64decode(m.group(1)))
        v = data.get("partyPresenceData", {}).get("partyClientVersion")
        return v if isinstance(v, str) and v else None
    except Exception:
        return None


def _fake_presence(version: str | None = None,
                   status: str = _DEFAULT_STATUS) -> bytes:
    ts = int(time.time() * 1000)
    val = base64.b64encode(json.dumps({
        "isValid": True, "isIdle": False, "queueId": "competitive",
        "provisioningFlow": "Invalid",
        "partyId": "00000000-0000-0000-0000-000000000000",
        "partySize": 1, "maxPartySize": 5,
        "partyOwnerMatchScoreAllyTeam": 0, "partyOwnerMatchScoreEnemyTeam": 0,
        "premierPresenceData": {
            "rosterId": "",
            "rosterName": _roster_name(status),
            "rosterTag": "Scout Active", "rosterType": "VCT",
            "division": 0, "score": 0, "plating": 0,
            "showAura": False, "showTag": True, "showPlating": False,
        },
        "matchPresenceData": {
            "sessionLoopState": "MENUS", "provisioningFlow": "Invalid",
            "matchMap": "", "queueId": "competitive",
        },
        "partyPresenceData": {
            "partyId": "00000000-0000-0000-0000-000000000000",
            "isPartyOwner": True, "partyState": "DEFAULT",
            "partyAccessibility": "CLOSED", "partyLFM": False,
            "partyClientVersion": version or "unknown",
            "partyVersion": ts,
            "partySize": 1, "maxPartySize": 5,
            "queueEntryTime": "0001.01.01-00.00.00",
            "isPartyCrossPlayEnabled": False, "isPlayerCrossPlayEnabled": False,
            "partyPrecisePlatformTypes": 1,
            "customGameName": "Valorant Scout Active", "customGameTeam": "",
            "tournamentId": "", "rosterId": "",
            "partyOwnerSessionLoopState": "MENUS",
            "partyOwnerMatchMap": "", "partyOwnerProvisioningFlow": "Invalid",
            "partyOwnerMatchScoreAllyTeam": 0, "partyOwnerMatchScoreEnemyTeam": 0,
        },
        "playerPresenceData": {
            "playerCardId": "d93ad22d-4db7-b6bc-5e9c-e5959bb9dd76",
            "playerTitleId": "e3ca05a4-4e44-9afe-3791-7d96ca8f71fa",
            "accountLevel": 999, "competitiveTier": 27, "leaderboardPosition": 1,
        },
    }).encode("utf-8")).decode("ascii")
    sid = uuid.uuid4()
    return (
        f"<presence from='{_FAKE_JID}/{_FAKE_RES}' id='b-{sid}'>"
        "<games>"
        f"<keystone><st>chat</st><s.t>{ts}</s.t><s.p>keystone</s.p><pty/></keystone>"
        f"<league_of_legends><st>chat</st><s.t>{ts}</s.t><s.p>league_of_legends</s.p>"
        f"<s.c>live</s.c><p>{{&quot;pty&quot;:true}}</p></league_of_legends>"
        f"<valorant><st>chat</st><s.t>{ts}</s.t><s.p>valorant</s.p><s.r>PC</s.r>"
        f"<p>{val}</p><pty/></valorant>"
        f"<bacon><st>chat</st><s.t>{ts}</s.t><s.l>bacon_availability_online</s.l>"
        f"<s.p>bacon</s.p></bacon>"
        "</games>"
        "<show>chat</show><platform>riot</platform><status/>"
        "</presence>"
    ).encode("utf-8")


def _fake_message(text: str) -> bytes:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S.000", time.gmtime())
    return (
        f"<message from='{_FAKE_JID}/{_FAKE_RES}' stamp='{stamp}' "
        f"id='scout-{uuid.uuid4()}' type='chat'><body>{text}</body></message>"
    ).encode("utf-8")


def find_riot_client() -> str | None:
    try:
        with open(RIOT_INSTALLS, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    for key in ("rc_default", "rc_live", "rc_beta"):
        p = data.get(key)
        if p and os.path.isfile(p):
            return p
    return None


def kill_riot() -> None:
    for name in _RIOT_PROCS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", name],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass


def _fetch_cert() -> bool:
    try:
        r = _http().get(CERT_URL, timeout=15)
        r.raise_for_status()
        pfx = r.content
    except Exception as e:
        _dbg(f"cert: fetch failed ({e})")
        return False
    try:
        from cryptography.hazmat.primitives.serialization import (
            pkcs12, Encoding, PrivateFormat, NoEncryption,
        )
        key, cert, extra = pkcs12.load_key_and_certificates(pfx, None)
        if key is None or cert is None:
            _dbg("cert: pfx missing key or leaf cert; ignoring")
            return False
        pem = cert.public_bytes(Encoding.PEM)
        for c in (extra or []):
            pem += c.public_bytes(Encoding.PEM)
        pem += key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    except Exception as e:
        _dbg(f"cert: pfx->pem conversion failed ({e})")
        return False
    tmp = _CACHED_CERT + ".tmp"
    try:
        os.makedirs(os.path.dirname(_CACHED_CERT), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(pem)
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(tmp)
        os.replace(tmp, _CACHED_CERT)
        _dbg("cert: fetched Deceive pfx + cached as PEM")
        return True
    except Exception as e:
        _dbg(f"cert: converted PEM unusable ({e})")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def ensure_cert() -> str:
    try:
        age = time.time() - os.path.getmtime(_CACHED_CERT)
        if 0 <= age < _CERT_CACHE_TTL and os.path.getsize(_CACHED_CERT) > 0:
            return _CACHED_CERT
    except OSError:
        pass
    if _fetch_cert():
        return _CACHED_CERT
    if os.path.isfile(_CACHED_CERT) and os.path.getsize(_CACHED_CERT) > 0:
        _dbg("cert: fetch failed — using stale cached trusted cert")
        return _CACHED_CERT
    if os.path.isfile(CERT_PATH) and os.path.getsize(CERT_PATH) > 0:
        _dbg("cert: WARNING no trusted cert available; using bundled self-signed. "
             "The Riot client will REJECT this — check network access to "
             f"{CERT_URL} (Deceive's cert host).")
        return CERT_PATH
    raise RuntimeError(
        f"No offline-mode cert available: fetch from {CERT_URL} failed and no cache "
        f"or bundled fallback at {CERT_PATH}."
    )


class _Target:
    host: str | None = None
    port: int = 5223
    affinity_resolved: bool = False


class _Conn:
    def __init__(self, client_writer, up_writer):
        self.client_writer = client_writer
        self.up_writer = up_writer
        self.version: str | None = None
        self.inserted = False
        self.presence_sent = False
        self.last_presence: str | None = None

    def capture(self, raw: str) -> None:
        self.last_presence = raw


class _Engine:
    def __init__(self):
        self._lock = threading.Lock()
        self.started = False
        self.config_port = None
        self.chat_port = None
        self.target = _Target()
        self._loop = None
        self.status = _DEFAULT_STATUS
        self.connected = False
        self.friends_loaded = False
        self._conns: list[_Conn] = []


    def start(self):
        with self._lock:
            if self.started:
                return
            cert = ensure_cert()
            _reset_log()
            saved = _load_status()
            self.status = saved if saved != "online" else _DEFAULT_STATUS
            self.connected = False
            self.friends_loaded = False
            self._start_chat(cert)
            self._start_config()
            self.started = True
            _dbg(f"engine started: config_port={self.config_port} "
                 f"chat_port={self.chat_port}", echo=True)


    def _start_chat(self, cert: str):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert)
        loop = asyncio.new_event_loop()
        self._loop = loop
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(loop)
            server = loop.run_until_complete(
                asyncio.start_server(self._handle_chat, "127.0.0.1", 0, ssl=ctx)
            )
            self.chat_port = server.sockets[0].getsockname()[1]
            ready.set()
            loop.run_forever()

        threading.Thread(target=run, name="offline-chat", daemon=True).start()
        if not ready.wait(10):
            raise RuntimeError("offline chat proxy failed to bind")

    async def _handle_chat(self, c_reader, c_writer):
        host, port = self.target.host, self.target.port
        _dbg(f"chat: client connected, relaying to upstream {host}:{port}", echo=True)
        if not host:
            _dbg("chat: NO upstream host captured yet — closing (config not fetched?)", echo=True)
            c_writer.close()
            return
        try:
            up_ctx = ssl.create_default_context()
            u_reader, u_writer = await asyncio.open_connection(
                host, port, ssl=up_ctx, server_hostname=host
            )
        except Exception:
            c_writer.close()
            return
        self.connected = True
        conn = _Conn(c_writer, u_writer)
        self._conns.append(conn)
        try:
            results = await asyncio.gather(
                self._pump_c2s(c_reader, u_writer, conn),
                self._pump_s2c(u_reader, c_writer, conn),
                return_exceptions=True,
            )
        finally:
            if conn in self._conns:
                self._conns.remove(conn)
        errs = [repr(r) for r in results if isinstance(r, Exception)]
        _dbg("chat: connection closed" + (f" errors={errs}" if errs else ""))
        for w in (c_writer, u_writer):
            try:
                w.close()
            except Exception:
                pass

    async def _pump_c2s(self, reader, writer, conn):
        dec = codecs.getincrementaldecoder("utf-8")()
        buf = ""
        while True:
            data = await reader.read(65536)
            if not data:
                buf += dec.decode(b"", final=True)
                break
            chunk = dec.decode(data)
            if _FAKE_JID in chunk:
                await self._handle_fake_command(chunk, conn)
                chunk = strip_fake_stanzas(chunk)
                if not chunk:
                    continue
            if conn.version is None:
                v = _extract_valorant_version(chunk)
                if v:
                    conn.version = v
                    _dbg(f"c2s: learned VALORANT version {v}")
                    if conn.presence_sent:
                        await self._send_fake_presence(conn)
            buf += chunk
            hide = self.status != "online"
            out, buf = process_c2s(buf, target=self.status if hide else "offline",
                                   on_presence=conn.capture, rewrite=hide)
            if out:
                writer.write(out.encode("utf-8"))
                await writer.drain()
            if conn.inserted and not conn.presence_sent:
                await self._send_fake_presence(conn)
        if buf:
            writer.write(buf.encode("utf-8"))
            await writer.drain()

    async def _pump_s2c(self, reader, writer, conn):
        while True:
            data = await reader.read(65536)
            if not data:
                break
            if not conn.inserted:
                hacked = inject_fake_roster(data)
                if hacked is not None:
                    conn.inserted = True
                    self.friends_loaded = True
                    writer.write(hacked)
                    await writer.drain()
                    _dbg("s2c: injected fake 'Valorant Scout Active' friend", echo=True)
                    asyncio.create_task(self._greet_later(conn))
                    continue
            writer.write(data)
            await writer.drain()

    async def _send_fake_presence(self, conn):
        conn.presence_sent = True
        try:
            conn.client_writer.write(_fake_presence(conn.version, self.status))
            await conn.client_writer.drain()
        except Exception:
            pass

    async def _greet_later(self, conn):
        try:
            await asyncio.sleep(6)
            conn.client_writer.write(_fake_message(
                f"Valorant Scout is active — friends see you as {self.status.upper()}. "
                "Message me 'online', 'offline', 'away' or 'mobile' to switch "
                "anytime (or use the Scout app / website)."))
            await conn.client_writer.drain()
        except Exception:
            pass


    def set_status(self, status: str) -> dict:
        status = (status or "").strip().lower()
        if status not in _VALID_STATUS:
            return {"ok": False, "message": f"Unknown status '{status}'."}
        self.status = status
        _save_status(status)
        _dbg(f"status: -> {status}")
        self._push_status()
        return {"ok": True, "status": status, "enabled": status != "online"}

    def set_enabled(self, enabled: bool) -> dict:
        if enabled:
            target = self.status if self.status != "online" else _load_status()
            return self.set_status(target if target != "online" else _DEFAULT_STATUS)
        return self.set_status("online")

    def _push_status(self):
        if self._loop is not None:
            for conn in list(self._conns):
                asyncio.run_coroutine_threadsafe(
                    self._resend_presence(conn), self._loop)

    async def _resend_presence(self, conn):
        try:
            raw = conn.last_presence
            if raw:
                payload = raw if self.status == "online" else _rewrite_presence(raw, self.status)
                conn.up_writer.write(payload.encode("utf-8"))
                await conn.up_writer.drain()
            if conn.presence_sent:
                conn.client_writer.write(_fake_presence(conn.version, self.status))
            conn.client_writer.write(_fake_message(_status_line(self.status)))
            await conn.client_writer.drain()
        except Exception:
            pass

    async def _handle_fake_command(self, chunk: str, conn):
        m = re.search(r"<body>(.*?)</body>", chunk, re.DOTALL)
        if not m:
            return
        body = m.group(1).strip().lower()
        for kw in ("offline", "away", "mobile", "online"):
            if kw in body:
                self.set_status(kw)
                return
        if "status" in body:
            reply = f"You're currently appearing {self.status.upper()}."
        elif "help" in body:
            reply = "Commands: online / offline / away / mobile / status / help"
        else:
            reply = "Didn't catch that. Try: online / offline / away / mobile / status / help"
        try:
            conn.client_writer.write(_fake_message(reply))
            await conn.client_writer.drain()
        except Exception:
            pass


    def _start_config(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        engine = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                fwd = {}
                for h in ("Authorization", "X-Riot-Entitlements-JWT", "User-Agent"):
                    if h in self.headers:
                        fwd[h] = self.headers[h]
                _dbg(f"config: GET {self.path[:80]} (auth={'Authorization' in fwd})")
                try:
                    up = _http().get(RIOT_CONFIG_URL + self.path,
                                     headers=fwd, timeout=20)
                except Exception:
                    self.send_error(502)
                    return
                body = up.content
                ctype = up.headers.get("Content-Type", "application/json")
                if "json" in ctype.lower():
                    try:
                        body = engine._rewrite_config(up.json(), fwd.get("Authorization"))
                        body = json.dumps(body).encode("utf-8")
                    except Exception:
                        body = up.content
                self.send_response(up.status_code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.config_port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever,
                         name="offline-config", daemon=True).start()

    def _rewrite_config(self, cfg: dict, auth: str | None = None) -> dict:
        if not isinstance(cfg, dict):
            return cfg
        host = cfg.get("chat.host")
        if isinstance(host, str):
            if not self.target.affinity_resolved:
                self.target.host = host
            cfg["chat.host"] = CHAT_DOMAIN
            _dbg(f"config: rewrote chat.host {host} -> {CHAT_DOMAIN}")
        if isinstance(cfg.get("chat.port"), int):
            self.target.port = cfg["chat.port"]
            cfg["chat.port"] = self.chat_port
        aff = cfg.get("chat.affinities")
        if isinstance(aff, dict):
            if cfg.get("chat.affinity.enabled") and auth \
                    and not self.target.affinity_resolved:
                resolved = self._resolve_affinity_host(aff, auth)
                if resolved:
                    self.target.host = resolved
                    self.target.affinity_resolved = True
            cfg["chat.affinities"] = {k: CHAT_DOMAIN for k in aff}
        return cfg

    def _resolve_affinity_host(self, affinities: dict, auth: str) -> str | None:
        try:
            r = _http().get(GEO_PAS_URL, headers={"Authorization": auth}, timeout=15)
            payload = r.text.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload))
            affinity = data.get("affinity")
            host = affinities.get(affinity) if affinity else None
            _dbg(f"config: affinity {affinity} -> {host}")
            return host if isinstance(host, str) else None
        except Exception as e:
            _dbg(f"config: affinity lookup failed ({e}); using default host")
            return None


_engine = _Engine()


def launch(status_: str | None = None) -> dict:
    _dbg(f"launch: requested (status={status_!r})", echo=True)
    if not sys.platform.startswith("win"):
        return {"ok": False, "message": "Offline mode is Windows-only."}

    rc = find_riot_client()
    if not rc:
        return {"ok": False,
                "message": "Couldn't find the Riot Client. Is VALORANT installed?"}

    s = (status_ or "").strip().lower()
    if s in _VALID_STATUS and s != "online":
        _save_status(s)

    try:
        _engine.start()
    except Exception as e:
        return {"ok": False, "message": str(e)}
    if s in _VALID_STATUS and s != "online" and _engine.status != s:
        _engine.set_status(s)

    _dbg(f"launch: killing Riot, then starting {rc}", echo=True)
    kill_riot()
    time.sleep(3.0)

    args = [
        rc,
        f'--client-config-url=http://127.0.0.1:{_engine.config_port}',
        "--launch-product=valorant",
        "--launch-patchline=live",
    ]
    try:
        subprocess.Popen(args, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        _dbg("launch: Popen'd Riot Client with " + " ".join(args[1:]))
    except Exception as e:
        return {"ok": False, "message": f"Couldn't start the Riot Client: {e}"}

    return {"ok": True,
            "message": "Launching VALORANT in offline mode — sign in as usual. "
                       f"Your friends will see you as {_engine.status}."}


def set_enabled(enabled: bool) -> dict:
    if not _engine.started:
        return {"ok": False, "message": "Offline mode isn't running."}
    return _engine.set_enabled(enabled)


def set_status(status_: str) -> dict:
    if not _engine.started:
        return {"ok": False, "message": "Offline mode isn't running."}
    return _engine.set_status(status_)


def status() -> dict:
    live = bool(_engine._conns)
    return {"running": _engine.started,
            "status": _engine.status,
            "enabled": _engine.status != "online",
            "connected": _engine.connected and live,
            "friendsLoaded": _engine.friends_loaded and live,
            "configPort": _engine.config_port,
            "chatPort": _engine.chat_port}


if __name__ == "__main__":
    p = ('<presence from="x"><show>chat</show><status>hi</status>'
         '<games><valorant><st>in game</st><p>YWJj</p></valorant>'
         '<league_of_legends><st>online</st></league_of_legends>'
         '<keystone><st>online</st></keystone></games></presence>')
    out, rem = process_c2s(p)
    assert rem == "", rem
    assert "<valorant" not in out, out
    assert "<keystone" not in out, out
    assert "<league_of_legends" not in out, out
    assert "<show>offline</show>" in out, out
    assert "<status" not in out, out

    muc = ("<presence to='room@muc' from='x'><show>chat</show>"
           "<games><valorant><st>x</st></valorant></games></presence>")
    out, rem = process_c2s(muc)
    assert out == muc and rem == "", (out, rem)

    other = "<iq type='result' id='1'><query/></iq>"
    out, rem = process_c2s(other)
    assert out == other and rem == "", (out, rem)

    a, b = p[:40], p[40:]
    out1, rem1 = process_c2s(a)
    assert out1 == "" and rem1, (out1, rem1)
    out2, rem2 = process_c2s(rem1 + b)
    assert rem2 == "" and "<valorant" not in out2 and "<keystone" not in out2, (out2, rem2)

    out, rem = process_c2s("hello <pres")
    assert out == "hello " and rem == "<pres", (out, rem)

    out, rem = process_c2s('<presence type="unavailable"/>')
    assert rem == "" and out.startswith("<presence"), (out, rem)

    roster = (b"<iq type='result'><query xmlns='jabber:iq:riotgames:roster'>"
              b"<item jid='real@pvp.net'/></query></iq>")
    hacked = inject_fake_roster(roster)
    assert hacked is not None
    assert b"Valorant Scout Active" in hacked
    assert hacked.index(b"Valorant Scout Active") < hacked.index(b"real@pvp.net")
    assert inject_fake_roster(b"<iq><nothing/></iq>") is None

    ver_blob = base64.b64encode(json.dumps(
        {"partyPresenceData": {"partyClientVersion": "release-10.11-shipping-9-9"}}
    ).encode()).decode()
    pv = (f'<presence from="me"><games><valorant><st>x</st>'
          f'<p>{ver_blob}</p></valorant></games></presence>')
    assert _extract_valorant_version(pv) == "release-10.11-shipping-9-9"
    _fp = _fake_presence("release-10.11-shipping-9-9").decode()
    assert _extract_valorant_version(_fp) == "release-10.11-shipping-9-9"
    assert _extract_valorant_version("<presence><show>chat</show></presence>") is None
    _m = re.search(r"<valorant\b[^>]*>.*?<p>([A-Za-z0-9+/=]+)</p>", _fp, re.DOTALL)
    _blob = json.loads(base64.b64decode(_m.group(1)))
    assert _blob["playerPresenceData"].get("playerCardId"), _blob
    assert _blob["playerPresenceData"].get("playerTitleId"), _blob
    assert _blob["partyPresenceData"].get("partyPrecisePlatformTypes") == 1, _blob
    assert "&quot;pty&quot;" in _fp and _fp.count("<p>") == 2, _fp

    seen = []
    out, rem = process_c2s(p, on_presence=seen.append)
    assert seen and seen[0].startswith("<presence") and "to=" not in seen[0][:60]
    seen2 = []
    process_c2s(muc, on_presence=seen2.append)
    assert seen2 == []
    passthru, _ = process_c2s(p, rewrite=False)
    assert "<valorant" in passthru and "<show>chat</show>" in passthru

    for st in ("offline", "away", "mobile"):
        out, _ = process_c2s(p, target=st)
        assert f"<show>{st}</show>" in out, (st, out)
        assert "<valorant" not in out and "<keystone" not in out, (st, out)

    dm = f"<message to='{_FAKE_JID}' type='chat'><body>offline</body></message>"
    real = "<message to='someone@eu1.pvp.net' type='chat'><body>hi</body></message>"
    assert strip_fake_stanzas(dm) == ""
    assert strip_fake_stanzas(dm + real) == real
    assert strip_fake_stanzas(real + dm) == real
    assert strip_fake_stanzas(real) == real
    assert strip_fake_stanzas(f"<iq to='{_FAKE_JID}' id='1'><q/></iq>{real}") == real
    assert strip_fake_stanzas(real + "<message to='x'") == real + "<message to='x'"

    for st, want in (("offline", "Offline Mode Active"),
                     ("away", "Away Mode Active"),
                     ("mobile", "Mobile Mode Active"),
                     ("online", "Online Mode Active")):
        assert _roster_name(st) == want, (st, _roster_name(st))
        blob = json.loads(base64.b64decode(re.search(
            r"<valorant\b[^>]*>.*?<p>([A-Za-z0-9+/=]+)</p>",
            _fake_presence("v", st).decode(), re.DOTALL).group(1)))
        assert blob["premierPresenceData"]["rosterName"] == want, blob
    assert all(_roster_name(s) for s in _VALID_STATUS)

    print("offline_launch self-check OK")
