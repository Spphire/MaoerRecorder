#!/usr/bin/env python
"""Network health probe — runs alongside the recorder to attribute gaps.

Every second, measures TCP-connect latency to:
  - the missevan CDN host (the stream's edge)
  - a stable public target (AliDNS) — proxy for the local uplink/ISP

Gap coincides with BOTH degrading -> local uplink/ISP (distributing lanes to
another machine would help). CDN/stream drops while public stays healthy ->
remote (source/CDN side), which distribution can't fix.

Writes one JSON line per second to probe.log.
"""
import json
import socket
import sys
import time

CDN_HOST = sys.argv[1] if len(sys.argv) > 1 else "d1-missevan104.bilivideo.com"
PUBLIC_HOST = "223.5.5.5"  # AliDNS anycast — uplink proxy


def tcp_ms(host: str, port: int, timeout: float = 2.0) -> float:
    t0 = time.monotonic()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return round((time.monotonic() - t0) * 1000, 1)
    except Exception:
        return -1.0


def main():
    with open("probe.log", "a", encoding="utf-8") as out:
        while True:
            rec = {
                "t": round(time.time(), 1),
                "cdn_ms": tcp_ms(CDN_HOST, 80),
                "pub_ms": tcp_ms(PUBLIC_HOST, 443),
            }
            out.write(json.dumps(rec) + "\n")
            out.flush()
            time.sleep(1.0)


if __name__ == "__main__":
    main()
