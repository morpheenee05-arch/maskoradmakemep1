"""
Static SDP offer template shaped exactly like the web client's captured one.

Used for signaling-only tests (phase 2): the ICE ufrag/pwd and DTLS
fingerprint are fake, so media never flows — but Google evaluates only the
SIP/SDP shape at the signaling layer. Phase 3 (aiortc) replaces this with a
real offer from the media stack.
"""

TEMPLATE_SDP = """v=0\r
o=- 6279769904123977737 2 IN IP4 127.0.0.1\r
s=-\r
t=0 0\r
a=group:BUNDLE 0\r
a=extmap-allow-mixed\r
a=msid-semantic: WMS\r
m=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 0 8 13 110 126\r
c=IN IP4 0.0.0.0\r
a=rtcp:9 IN IP4 0.0.0.0\r
a=ice-ufrag:mAS9\r
a=ice-pwd:0JtffAzPf/4Zqn5LSFiuLfL6\r
a=ice-options:trickle\r
a=fingerprint:sha-256 20:B3:DC:33:62:75:DE:63:A5:21:ED:EF:5C:2A:47:14:F6:C6:28:1F:45:33:95:64:5D:93:F1:BD:18:17:20:CA\r
a=setup:actpass\r
a=mid:0\r
a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level\r
a=sendrecv\r
a=msid:- 55e8e7ac-259b-4445-b1d3-93f6a3f0496c\r
a=rtcp-mux\r
a=rtpmap:111 opus/48000/2\r
a=fmtp:111 minptime=10;useinbandfec=1\r
a=rtpmap:63 red/48000/2\r
a=fmtp:63 111/111\r
a=rtpmap:9 G722/8000\r
a=rtpmap:0 PCMU/8000\r
a=rtpmap:8 PCMA/8000\r
a=rtpmap:13 CN/8000\r
a=rtpmap:110 telephone-event/48000\r
a=rtpmap:126 telephone-event/8000\r
a=ssrc:2407426248 cname:TSEsOzc+jKql3A2g\r
a=ssrc:2407426248 msid:- 55e8e7ac-259b-4445-b1d3-93f6a3f0496c\r
"""
