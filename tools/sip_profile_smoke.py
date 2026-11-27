"""Smoke test: SIP wire profile (Birdsong v2) + Chrome-shaped SDP offer.

Runs fully offline: a FakeWS stands in for the websocket, scripted 401/200/
183/180/200/BYE/487 responses drive GVWebPhone through register, call setup,
PRACK/ACK, remote BYE (P-RTP-Stat), CANCEL (same branch), zombie-ACK and
orphan-2xx handling. Every emitted message is asserted against the shapes
captured in tools/our_test/decoded_frames.txt.

    py -3 tools/sip_profile_smoke.py
"""
import asyncio
import os
import re
import sys
from types import SimpleNamespace

os.environ["SIP_TRACE"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telephony.sip.sip_ws import GVWebPhone, SipMessage  # noqa: E402

DOMAIN = "web.c.pbx.voice.sip.google.com"
CREDS = SimpleNamespace(
    username="AYpoY9uiME0rBVhoUZGGHwbH-8774SfqK2N3mzvIR1J4obx64-7rcjU=",
    domain=DOMAIN, realm=DOMAIN, password="s3cret")
SERVICE_ROUTE = ("sip:216.239.36.145:443;transport=udp;lr;"
                 "uri-econt=ADD2O7COJLT4JIPYWKG432BMI3HTUNUVE7RA")
P_ASSOC = (f"sip:BI4ECWLQN5MTS5LJJVCTA4SCKZUG6VK2I5DUQ53CJAWTQNZX@{DOMAIN}")
RR_ECONT = ("<sip:216.239.36.145:443;lr;transport=wss;"
            "uri-econt=ADD2O7COCBVMQULRHHBYHVP2XL74IPNGAM3NR5NG72>")
RR_PLAIN = "<sip:216.239.36.145:443;lr;transport=wss>"
REMOTE_TARGET = (f"sip:+12487095328@ACZQQLQOUMLHMZP7CLBW7DT4VBBBCATWTVOH"
                 f"OWNIMJPXQBJUFOVLQGUJKS7IV2P:5060;transport=udp;"
                 f"uri-econt=CQX3JTRMQQGJWIDP5UVHPLPFEFDUA")
REMOTE_TAG = ("BIQVAQ2JIZCUGUJSI5EUMSSFKNLVGTCJGVBEEVKCKFEUCSKJIFFUSQKDCI"
              "EFETSIJ5CFSWSB")

ANSWER_SDP = (
    "v=0\r\n"
    "o=xavier 1795816875549 1795816876 IN IP4 74.125.39.93\r\n"
    "s=SIP Call\r\n"
    "c=IN IP4 74.125.39.93\r\n"
    "t=0 0\r\n"
    "a=sendrecv\r\n"
    "a=ice-lite\r\n"
    "a=ice-ufrag:tDZ0e/Gm+RTh\r\n"
    "a=ice-pwd:A9eeZe7J/paKL890H8mBIRKJ\r\n"
    "a=group:BUNDLE 0\r\n"
    "a=fingerprint:sha-256 98:BF:0D:81:0F:B8:B0:E8:9D:67:CE:BB:EC:7F:3B:E1:"
    "69:B1:41:BA:8B:07:9A:44:06:45:5F:58:8C:88:16:DC\r\n"
    "a=setup:passive\r\n"
    "m=audio 26500 UDP/TLS/RTP/SAVPF 111 110\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "a=rtpmap:110 telephone-event/48000\r\n"
    "a=candidate:1 1 UDP 1 74.125.39.93 26500 typ host\r\n"
    "a=candidate:2 1 UDP 2 2001:4860:4864:2::93 26500 typ host\r\n"
    "a=mid:0\r\n"
    "a=sendrecv\r\n"
    "a=rtcp-mux\r\n")

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


def branch_of(msg):
    m = re.search(r";branch=([^;\s]+)", msg.header("Via") or "")
    return m.group(1) if m else ""


def header_names(msg):
    # Content-Length is appended by to_bytes() at serialize time
    return [n for n, _ in msg.headers if n.lower() != "content-length"]


class FakeWS:
    def __init__(self):
        self.sent = asyncio.Queue()

    async def send(self, data):
        text = data.decode() if isinstance(data, (bytes, bytearray)) else data
        await self.sent.put(text)

    async def next_msg(self, timeout=5):
        raw = await asyncio.wait_for(self.sent.get(), timeout)
        return SipMessage.parse(raw)


def respond(req, code, reason, extra=(), body="", to_tag=None):
    resp = SipMessage(is_request=False, status_code=code, reason=reason)
    resp.headers = [("Via", req.header("Via"))]
    for rr in req.headers_all("Record-Route"):
        resp.headers.append(("Record-Route", rr))
    to = req.header("To") or ""
    if to_tag and "tag=" not in to:
        to += f";tag={to_tag}"
    cseq = req.header("CSeq") or ""
    resp.headers += [("To", to), ("From", req.header("From") or ""),
                     ("Call-ID", req.header("Call-ID") or ""),
                     ("CSeq", cseq)]
    resp.headers += list(extra)
    if body:
        resp.headers.append(("Content-Type", "application/sdp"))
        resp.body = body
    # parse() would derive this from CSeq — direct construction must not
    parts = cseq.split()
    if len(parts) == 2:
        resp.method = parts[1]
    return resp


async def sip_part():
    print("== SIP wire profile ==")
    ws = FakeWS()
    events = []
    phone = GVWebPhone(CREDS, on_event=lambda k, i: events.append(k))
    phone._ws = ws

    # ── REGISTER → 401 → REGISTER → 200 ──
    reg_task = asyncio.create_task(phone.register())
    reg1 = await ws.next_msg()
    check("REGISTER request-line", reg1.method == "REGISTER"
          and reg1.uri == f"sip:{DOMAIN}")
    check("REGISTER branch shape",
          re.fullmatch(r"z9hG4bK-524287-1---[0-9a-f]{20}",
                       branch_of(reg1)) is not None, branch_of(reg1))
    via1 = reg1.header("Via") or ""
    check("REGISTER via host shape",
          re.search(r"SIP/2\.0/WSS [A-Za-z0-9]+-[A-Za-z0-9]+\.invalid:5061",
                    via1) is not None, via1)
    check("REGISTER via has ;keep", via1.endswith(";keep"), via1)
    check("REGISTER header order",
          header_names(reg1) == [
              "Via", "Max-Forwards", "Contact", "To", "From", "Call-ID",
              "CSeq", "Expires", "Allow", "Supported", "User-Agent",
              "Authorization", "X-Google-Client-Info"],
          str(header_names(reg1)))
    check("REGISTER CSeq 1", reg1.header("CSeq") == "1 REGISTER")
    check("REGISTER Call-ID shape",
          re.fullmatch(r"[A-Za-z0-9\-_]{40}_\d{10}",
                       reg1.header("Call-ID") or "") is not None,
          reg1.header("Call-ID"))
    m = re.search(r";tag=([0-9a-f]{8})$", reg1.header("From") or "")
    check("REGISTER From-tag 8 hex", m is not None, reg1.header("From"))
    reg_from_tag = m.group(1) if m else ""
    contact = reg1.header("Contact") or ""
    check("REGISTER Contact @[::]:5061",
          f"<sip:{CREDS.username}@[::]:5061;transport=wss>" in contact)
    check("REGISTER Contact reg-id=1", ";reg-id=1;" in contact)
    check("REGISTER Contact instance UPPERCASE",
          re.search(r'\+sip\.instance="<urn:uuid:[0-9A-F-]{36}>"',
                    contact) is not None, contact)
    check("REGISTER Contact icsi-ref", "icsi.mmtel" in contact)
    check("REGISTER Contact device-id",
          re.search(r';device-id="[A-Za-z0-9]{12}"', contact) is not None)
    auth1 = reg1.header("Authorization") or ""
    check("REGISTER #1 empty digest",
          auth1.endswith('nonce="",response=""') and "algorithm" not in auth1,
          auth1)
    check("REGISTER UA Birdsong",
          reg1.header("User-Agent") ==
          "GoogleVoice voice.web-frontend_20261104.07_p4/Birdsong v2.2.74")
    check("REGISTER Max-Forwards 70", reg1.header("Max-Forwards") == "70")

    await phone._dispatch(respond(
        reg1, 401, "Unauthorized", to_tag="3012e84b", extra=[(
            "WWW-Authenticate",
            f'Digest nonce="dg4aqm0k46azJNEHyKd6jw==",algorithm=MD5,'
            f'realm="{DOMAIN}"')]))
    reg2 = await ws.next_msg()
    check("REGISTER #2 CSeq 2", reg2.header("CSeq") == "2 REGISTER")
    check("REGISTER #2 same Call-ID",
          reg2.header("Call-ID") == reg1.header("Call-ID"))
    auth2 = reg2.header("Authorization") or ""
    order = [auth2.find(p) for p in (
        'username="', 'realm="', 'nonce="dg4aqm0k46azJNEHyKd6jw=="',
        'uri="', 'response="', "algorithm=MD5")]
    check("REGISTER #2 digest param order",
          all(i >= 0 for i in order) and order == sorted(order), auth2)
    check("REGISTER #2 new branch", branch_of(reg2) != branch_of(reg1))

    await phone._dispatch(respond(
        reg2, 200, "OK", to_tag="e528584e", extra=[
            ("Service-Route", f"<{SERVICE_ROUTE}>"),
            ("Contact", (reg1.header("Contact") or "").replace(
                ";device-id=", ";expires=3600;device-id=")),
            ("P-Associated-URI", f"<{P_ASSOC}>"),
        ]))
    await asyncio.wait_for(reg_task, 5)
    check("registered event", "registered" in events, str(events))
    check("expires parsed", phone._reg_expires == 3600)

    # ── INVITE ──
    dialog = await phone.start_call("+12487095328", "OFFER-SDP")
    invite = await ws.next_msg()
    check("INVITE R-URI",
          invite.uri == f"sip:+12487095328@{DOMAIN}", invite.uri)
    via_i = invite.header("Via") or ""
    check("INVITE via w/o ;keep",
          ";keep" not in via_i and via_i.endswith(";rport"), via_i)
    check("INVITE header order",
          header_names(invite) == [
              "Via", "Max-Forwards", "Route", "Contact", "To", "From",
              "Call-ID", "CSeq", "Allow", "Content-Type", "Supported",
              "User-Agent", "P-Preferred-Identity", "X-Google-Client-Info"],
          str(header_names(invite)))
    check("INVITE Route = Service-Route",
          invite.header("Route") == f"<{SERVICE_ROUTE}>")
    check("INVITE CSeq 1", invite.header("CSeq") == "1 INVITE")
    icontact = invite.header("Contact") or ""
    check("INVITE Contact device-id user, no reg-id",
          "reg-id" not in icontact and "@[::]:5061" in icontact, icontact)
    dev_user = re.search(r"<sip:([A-Za-z0-9]{12})@", icontact)
    check("INVITE Contact user = 12-char device-id", dev_user is not None,
          icontact)
    dev_id = dev_user.group(1) if dev_user else "?"
    check("INVITE From = device-id",
          re.fullmatch(rf"<sip:{re.escape(dev_id)}@{re.escape(DOMAIN)}>"
                       rf";tag=[0-9a-f]{{8}}",
                       invite.header("From") or "") is not None,
          invite.header("From"))
    check("INVITE PPI in <>",
          invite.header("P-Preferred-Identity") == f"<{P_ASSOC}>")
    check("INVITE Supported 100rel",
          invite.header("Supported") ==
          "outbound, path, record-aware, replaces, 100rel")
    check("INVITE body is offer", invite.body == "OFFER-SDP")
    check("INVITE branch shape",
          re.fullmatch(r"z9hG4bK-524287-1---[0-9a-f]{20}",
                       branch_of(invite)) is not None)
    check("INVITE Call-ID != REGISTER Call-ID",
          invite.header("Call-ID") != reg1.header("Call-ID"))
    invite_branch = branch_of(invite)

    # ── 183 (100rel) → PRACK → 200 ──
    r183 = respond(invite, 183, "Session Progress", to_tag=REMOTE_TAG,
                   body=ANSWER_SDP, extra=[
                       ("Record-Route", RR_ECONT),
                       ("Record-Route", RR_PLAIN),
                       ("Require", "100rel"),
                       ("Contact", f"<{REMOTE_TARGET}>"),
                       ("RSeq", "1"),
                   ])
    # fix header order to look like the capture (RR right after Via)
    await phone._dispatch(r183)
    prack = await ws.next_msg()
    check("PRACK target = remote Contact", prack.uri == REMOTE_TARGET,
          prack.uri)
    check("PRACK CSeq 2", prack.header("CSeq") == "2 PRACK")
    check("PRACK RAck", prack.header("RAck") == "1 1 INVITE",
          prack.header("RAck"))
    routes = prack.headers_all("Route")
    check("PRACK route-set reversed",
          routes == ["<sip:216.239.36.145:443;lr;transport=wss>",
                     "<sip:216.239.36.145:443;lr;transport=wss;"
                     "uri-econt=ADD2O7COCBVMQULRHHBYHVP2XL74IPNGAM3NR5NG72>"],
          str(routes))
    check("PRACK To has remote tag",
          f";tag={REMOTE_TAG}" in (prack.header("To") or ""))
    check("PRACK header order",
          header_names(prack) == [
              "Via", "Max-Forwards", "Route", "Route", "Contact", "To",
              "From", "Call-ID", "CSeq", "User-Agent", "RAck",
              "X-Google-Client-Info"],
          str(header_names(prack)))
    await phone._dispatch(respond(prack, 200, "OK", to_tag=None))

    # ── 180 (100rel, RSeq 2) → PRACK CSeq 3 ──
    r180 = respond(invite, 180, "Ringing", to_tag=REMOTE_TAG, extra=[
        ("Record-Route", RR_ECONT), ("Record-Route", RR_PLAIN),
        ("Require", "100rel"), ("Contact", f"<{REMOTE_TARGET}>"),
        ("RSeq", "2")])
    await phone._dispatch(r180)
    prack2 = await ws.next_msg()
    check("PRACK #2 CSeq 3", prack2.header("CSeq") == "3 PRACK")
    check("PRACK #2 RAck", prack2.header("RAck") == "2 1 INVITE")
    await phone._dispatch(respond(prack2, 200, "OK"))
    check("ringing event", "ringing" in events, str(events))

    # ── 200 OK (INVITE) → ACK with INVITE's CSeq ──
    r200 = respond(invite, 200, "OK", to_tag=REMOTE_TAG, extra=[
        ("Record-Route", RR_ECONT), ("Record-Route", RR_PLAIN),
        ("Contact", f"<{REMOTE_TARGET}>"),
        ("Session-Expires", "1800;refresher=uas"),
        ("X-GV-CallLegId", "AOkN4pIeV5suvUPFp9SRE0jvmVVbJB35"),
    ])
    await phone._dispatch(r200)
    ack = await ws.next_msg()
    check("ACK CSeq == INVITE CSeq", ack.header("CSeq") == "1 ACK",
          ack.header("CSeq"))
    check("ACK target = remote Contact", ack.uri == REMOTE_TARGET)
    check("answered event", "answered" in events, str(events))
    check("dialog answered", phone.dialog is not None
          and phone.dialog.answered)
    check("refresher=uas → no refresh task",
          phone._session_refresh_task is None)

    # ── inbound BYE → 200 with P-RTP-Stat ──
    phone.rtp_stat_provider = lambda: (
        "PS=1400, OS=48493, PR=1401, OR=52779, PL=0, JI=0, LA=41, DU=28, NP=1")
    bye = SipMessage(is_request=True, method="BYE",
                     uri=f"sip:{dev_user.group(1) if dev_user else 'x'}"
                         f"@[::]:5061;transport=wss")
    bye.headers = [
        ("Via", "SIP/2.0/WSS 216.239.36.145:443;"
                "branch=z9hG4bK-524287-1---ace5332dd34828632dc97b466121bdab"
                ";rport"),
        ("Via", "SIP/2.0/UDP 216.239.36.145:443;"
                "branch=z9hG4bK-524287-1---c3c04f96aba4d2c66736877d21cb3d36"),
        ("Record-Route", RR_PLAIN),
        ("Record-Route", RR_ECONT),
        ("Max-Forwards", "68"),
        ("To", f"<sip:{dev_user.group(1) if dev_user else 'x'}@{DOMAIN}>"
               f";tag={re.search(r';tag=([0-9a-f]{8})', invite.header('From')).group(1)}"),
        ("From", f"<sip:+12487095328@{DOMAIN}>;tag={REMOTE_TAG}"),
        ("Call-ID", invite.header("Call-ID")),
        ("CSeq", "2 BYE"),
    ]
    await phone._dispatch(bye)
    bye200 = await ws.next_msg()
    check("BYE → 200 OK", bye200.status_code == 200)
    check("BYE 200 keeps all Via",
          len(bye200.headers_all("Via")) == 2)
    check("BYE 200 P-RTP-Stat",
          (bye200.header("P-RTP-Stat") or "").startswith("PS=1400, OS=48493"),
          bye200.header("P-RTP-Stat"))
    check("BYE 200 has UA + X-GCI",
          bye200.header("User-Agent") is not None
          and bye200.header("X-Google-Client-Info") is not None)
    check("bye event", "bye" in events, str(events))
    check("dialog cleared after BYE", phone.dialog is None)

    # ── CANCEL reuses INVITE branch/CSeq; 487 → zombie ACK ──
    d2 = await phone.start_call("+12487095329", "OFFER2")
    invite2 = await ws.next_msg()
    inv2_branch = branch_of(invite2)
    await phone.hangup()
    cancel = await ws.next_msg()
    check("CANCEL same branch as INVITE",
          branch_of(cancel) == inv2_branch,
          f"{branch_of(cancel)} vs {inv2_branch}")
    check("CANCEL CSeq == INVITE CSeq",
          cancel.header("CSeq") == "1 CANCEL", cancel.header("CSeq"))
    check("CANCEL R-URI == INVITE R-URI", cancel.uri == invite2.uri)
    check("CANCEL Reason 487",
          cancel.header("Reason") == 'SIP ;cause=487 ;text="Cancelled"')
    r487 = respond(invite2, 487, "Request Terminated", to_tag="deadbeef01")
    await phone._dispatch(r487)
    zack = await ws.next_msg()
    check("zombie ACK same branch as INVITE",
          branch_of(zack) == inv2_branch, branch_of(zack))
    check("zombie ACK CSeq", zack.header("CSeq") == "1 ACK")
    check("zombie ACK To keeps 487's tag",
          ";tag=deadbeef01" in (zack.header("To") or ""))

    # ── orphan 2xx → ACK + BYE ──
    orphan = respond(invite2, 200, "OK", to_tag="0rphan99", extra=[
        ("Record-Route", RR_ECONT), ("Record-Route", RR_PLAIN),
        ("Contact", f"<{REMOTE_TARGET}>")])
    orphan.headers = [("Via", "SIP/2.0/WSS x.invalid:5061;"
                              "branch=z9hG4bK-524287-1---00000000000000000000"
                              ";rport")] + orphan.headers[1:]
    await phone._dispatch(orphan)
    oack = await ws.next_msg()
    obye = await ws.next_msg()
    check("orphan ACK", oack.method == "ACK"
          and oack.header("CSeq") == "1 ACK")
    check("orphan BYE", obye.method == "BYE"
          and obye.header("CSeq") == "1 BYE")
    check("orphan BYE Reason normal",
          obye.header("Reason") == 'SIP ;cause=200 ;text="Normal call clearing"')

    await phone.close()
    phone._closed = True
    if phone._reregister_task:
        phone._reregister_task.cancel()


async def media_part():
    print("== Chrome-shaped SDP offer ==")
    from telephony.sip.media import SipMediaSession
    sess = SipMediaSession(ice_servers=[])
    offer = await asyncio.wait_for(sess.create_offer(), 30)
    lines = offer.replace("\r\n", "\n").split("\n")

    check("offer m-line Chrome",
          "m=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 0 8 13 110 101" in lines)
    check("offer o=- Chrome",
          any(re.fullmatch(r"o=- \d{19} 2 IN IP4 127\.0\.0\.1", l)
              for l in lines),
          next((l for l in lines if l.startswith("o=")), ""))
    check("offer c= 0.0.0.0", "c=IN IP4 0.0.0.0" in lines)
    expected_order = [
        "a=rtpmap:111 opus/48000/2",
        "a=fmtp:111 minptime=10;useinbandfec=1",
        "a=rtpmap:63 red/48000/2",
        "a=fmtp:63 111/111",
        "a=rtpmap:9 G722/8000",
        "a=rtpmap:0 PCMU/8000",
        "a=rtpmap:8 PCMA/8000",
        "a=rtpmap:13 CN/8000",
        "a=rtpmap:110 telephone-event/48000",
        "a=rtpmap:101 telephone-event/8000",
    ]
    idx = [lines.index(x) if x in lines else -1 for x in expected_order]
    check("offer codec block order",
          all(i >= 0 for i in idx) and idx == sorted(idx), str(idx))
    fps = [l for l in lines if l.startswith("a=fingerprint:sha-256 ")]
    check("offer exactly one sha-256 fingerprint", len(fps) == 1, str(fps))
    check("offer ice-options:trickle", "a=ice-options:trickle" in lines)
    check("offer setup:actpass", "a=setup:actpass" in lines)
    check("offer extmap 1-4 Chrome",
          all(any(l.startswith(f"a=extmap:{i} ") for l in lines)
              for i in (1, 2, 3, 4)))
    check("offer msid-semantic not '*'",
          any(l.startswith("a=msid-semantic: WMS ") and l != "a=msid-semantic: WMS *"
              for l in lines),
          next((l for l in lines if l.startswith("a=msid-semantic")), ""))
    check("offer rtcp-rsize/xr",
          "a=rtcp-rsize" in lines and "a=rtcp-xr:rcvr-rtt=all" in lines)
    check("offer rtcp-fb transport-cc", "a=rtcp-fb:111 transport-cc" in lines)
    ssrc_lines = [l for l in lines if l.startswith("a=ssrc:")]
    check("offer ssrc cname 16 chars",
          any(re.fullmatch(r"a=ssrc:\d+ cname:.{16}", l) for l in ssrc_lines),
          str(ssrc_lines))
    check("offer ssrc-msid present",
          any(" msid:" in l for l in ssrc_lines), str(ssrc_lines))
    n_cand = sum(1 for l in lines if l.startswith("a=candidate:"))
    check("offer keeps ICE candidates", n_cand >= 1, f"count={n_cand}")
    check("offer end-of-candidates", "a=end-of-candidates" in lines)
    check("offer ufrag copied",
          any(l.startswith("a=ice-ufrag:") for l in lines))
    check("offer no aiortc c= IP leak",
          not any(l.startswith("c=IN IP4 ") and l != "c=IN IP4 0.0.0.0"
                  for l in lines))

    await sess.accept_answer(ANSWER_SDP)
    check("telephone-event pt 110 from answer",
          sess._te_payload_type == 110, str(sess._te_payload_type))

    stat = sess.rtp_stat()
    check("rtp_stat shape",
          re.fullmatch(r"PS=\d+, OS=\d+, PR=\d+, OR=\d+, PL=0, JI=0, "
                       r"LA=0, DU=\d+, NP=1", stat) is not None, stat)
    await sess.close()


async def main():
    await sip_part()
    await media_part()
    if FAILS:
        print(f"\n{len(FAILS)} FAILURES: {FAILS}")
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
