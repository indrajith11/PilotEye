#!/usr/bin/env python3
"""CLI client for the live browser daemon.
Usage:
  python client.py status
  python client.py nav <url>
  python client.py snap [--shot]        # element list (+save frame)
  python client.py click <ref|css>
  python client.py fill <ref|css> <text>
  python client.py select <ref|css> <value>
  python client.py press <key> | scroll <dy> | hover <x> <y>
  python client.py upload <ref|css> <path>
  python client.py autofill
  python client.py captcha              # detect + attempt solve
  python client.py otp-request [email|mobile] [hint]
  python client.py otp <request_id> <code>
  python client.py ai <goal>            # one AI vision decision+action
  python client.py ai-loop <goal> [n]   # autonomous loop (default 12 steps)
  python client.py idle | tab <url> | restart | log
"""
import json, sys, time, urllib.request

BASE = "http://127.0.0.1:8765"


def call(path, payload=None):
    if payload is None and path not in ("/status", "/snapshot", "/shot", "/log"):
        payload = {}
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def compact_snap(s):
    if "error" in s:
        print("SNAP ERROR:", s.get("error"))
    print(f"URL: {s.get('url','')}  TITLE: {s.get('title','')}")
    caps = s.get("captcha", [])
    if caps:
        print(f"CAPTCHA SIGNALS: {json.dumps(caps)[:300]}")
    for el in s.get("elements", []):
        bits = [f"{el['ref']} [{el['tag']}" + (f" {el['type']}" if el["type"] else "")
                + (" req" if el.get("req") else "") + "]"]
        if el.get("label"): bits.append(f"label={el['label']!r}")
        if el.get("ph"): bits.append(f"ph={el['ph']!r}")
        if el.get("text"): bits.append(f"text={el['text']!r}")
        if el.get("value"): bits.append(f"val={el['value']!r}")
        if el.get("options"): bits.append(f"opts={el['options'][:6]}")
        print("  " + " ".join(bits))


def main():
    a = sys.argv[1:]
    cmd = a[0] if a else "status"
    if cmd == "status":
        print(json.dumps(call("/status"), indent=1))
    elif cmd == "nav":
        print(call("/nav", {"url": a[1]}))
        time.sleep(0.3)
        compact_snap(call("/snapshot"))
    elif cmd == "snap":
        s = call("/snapshot")
        if "--shot" in a: print("frame:", call("/shot").get("frame"))
        compact_snap(s)
    elif cmd == "click":
        print(call("/click", {"sel": a[1]}))
    elif cmd == "fill":
        print(call("/fill", {"sel": a[1], "text": a[2] if len(a) > 2 else ""}))
    elif cmd == "select":
        print(call("/select", {"sel": a[1], "value": a[2]}))
    elif cmd == "press":
        print(call("/press", {"key": a[1] if a else "Enter"}))
    elif cmd == "scroll":
        print(call("/scroll", {"dy": int(a[1]) if a else 400}))
    elif cmd == "hover":
        print(call("/hover", {"x": a[1], "y": a[2]}))
    elif cmd == "mclick":
        print(call("/mouse_click", {"x": a[1], "y": a[2]}))
    elif cmd == "upload":
        print(call("/upload", {"sel": a[1], "path": a[2]}))
    elif cmd == "autofill":
        print(call("/autofill"))
    elif cmd == "captcha":
        d = call("/captcha_detect")
        print("detect:", json.dumps(d)[:300])
        if d.get("captcha"):
            print("solve:", json.dumps(call("/captcha_solve"))[:400])
    elif cmd == "otp-request":
        r = call("/otp_request", {"source": a[1] if a else "email",
                                  "hint": a[2] if len(a) > 2 else ""})
        print(r)
        if "--wait" in sys.argv:
            print(call("/otp_wait", {"request_id": r["request_id"]}))
    elif cmd == "otp":
        print(call("/otp", {"request_id": a[1], "code": a[2]}))
    elif cmd == "ai":
        print(json.dumps(call("/ai_step", {"goal": " ".join(a[1:])}), indent=1)[:1200])
    elif cmd == "ai-loop":
        goal = a[1]; n = int(a[2]) if len(a) > 2 else 12
        seen = {}
        for i in range(n):
            r = call("/ai_step", {"goal": goal})
            dec = r.get("decision", {})
            sig = (dec.get("action", "?"), dec.get("ref", ""), (dec.get("value", "") or "")[:30])
            seen[sig] = seen.get(sig, 0) + 1
            print(f"[step {i+1}] {sig[0]} {sig[1]} {sig[2]!r} — {dec.get('reason','')[:60]}"
                  + ("  (REPEAT x%d)" % seen[sig] if seen[sig] > 1 else ""))
            if seen[sig] >= 3:
                print("STUCK: same action repeated 3x — stopping for human review"); break
            if r.get("exec", {}).get("done") or dec.get("action") == "done":
                print("GOAL DONE"); break
            if r.get("exec", {}).get("stuck"):
                print("STUCK:", r["exec"]["stuck"]); break
            time.sleep(0.4)
        print("final:", call("/shot"))
    elif cmd == "eval":
        print(call("/eval", {"expr": a[1]}))
    elif cmd == "idle":
        print(call("/wait_idle"))
    elif cmd == "tab":
        print(call("/new_tab", {"url": a[1] if a else None}))
    elif cmd == "restart":
        print(call("/restart"))
    elif cmd == "log":
        for line in call("/log").get("log", []):
            print(line)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
