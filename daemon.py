#!/usr/bin/env python3
"""Live browser daemon v2: keeps a warm persistent browser, HTTP control API.
Playwright sync API is thread-bound -> ALL browser ops run in ONE worker thread
via a command queue; HTTP handler threads just marshal requests."""
import json, os, queue, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as eng
import smart

PORT = 8765


class BrowserWorker:
    """Single thread owns Playwright. Others submit callables and wait."""

    def __init__(self):
        self.q = queue.Queue()
        self.box = {}
        self.ready = threading.Event()
        self.err = None
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()
        self.ready.wait(timeout=90)
        if self.err:
            raise RuntimeError(f"browser start failed: {self.err}")

    def _run(self):
        try:
            self.lb = eng.LiveBrowser()
        except Exception as e:
            self.err = str(e)
            self.ready.set()
            return
        self.ready.set()
        while True:
            fn, box, evt = self.q.get()
            try:
                box["r"] = fn(self.lb)
            except Exception as e:
                box["e"] = e
            evt.set()

    def run(self, fn, timeout=170):
        box, evt = {}, threading.Event()
        self.q.put((fn, box, evt))
        if not evt.wait(timeout):
            raise TimeoutError("browser op timeout")
        if "e" in box:
            raise box["e"]
        return box["r"]


W = None
LB_REF = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/status":
            return self._send(W.run(lambda lb: lb._touch()))
        if self.path == "/snapshot":
            return self._send(W.run(lambda lb: lb.snapshot()))
        if self.path == "/shot":
            return self._send(W.run(lambda lb: {"ok": True, "frame": lb.shot()}))
        if self.path == "/log":
            try:
                with open(eng.LOG_PATH) as f:
                    return self._send({"log": f.read().splitlines()[-40:]})
            except Exception:
                return self._send({"log": []})
        return self._send({"error": "unknown GET"})

    def do_POST(self):
        b = self._body()
        try:
            if self.path == "/nav":
                return self._send(W.run(lambda lb: lb.nav(b["url"])))
            if self.path == "/click":
                return self._send(W.run(lambda lb: lb.click(b["sel"])))
            if self.path == "/fill":
                return self._send(W.run(lambda lb: lb.fill(b["sel"], b.get("text", ""))))
            if self.path == "/select":
                return self._send(W.run(lambda lb: lb.select(b["sel"], b.get("value", ""))))
            if self.path == "/press":
                return self._send(W.run(lambda lb: lb.press(b.get("key", "Enter"))))
            if self.path == "/scroll":
                return self._send(W.run(lambda lb: lb.scroll(int(b.get("dy", 400)))))
            if self.path == "/hover":
                return self._send(W.run(lambda lb: lb.hover(int(b.get("x", 0)), int(b.get("y", 0)))))
            if self.path == "/mouse_click":
                return self._send(W.run(lambda lb: lb.mouse_click(int(b.get("x", 0)), int(b.get("y", 0)))))
            if self.path == "/upload":
                return self._send(W.run(lambda lb: lb.upload(b["sel"], b["path"])))
            if self.path == "/upload_click":
                return self._send(W.run(lambda lb: lb.upload_click(b["trigger_sel"], b["path"])))
            if self.path == "/wait_idle":
                return self._send(W.run(lambda lb: lb.wait_idle(int(b.get("ms", 3500)))))
            if self.path == "/autofill":
                return self._send(W.run(lambda lb: smart.autofill(lb)))
            if self.path == "/captcha_detect":
                return self._send(W.run(lambda lb: smart.detect_captcha(lb)))
            if self.path == "/captcha_solve":
                return self._send(W.run(lambda lb: smart.solve_captcha(lb), timeout=120))
            if self.path == "/otp_request":
                return self._send(W.run(lambda lb: smart.request_otp(
                    lb, b.get("source", "email"), b.get("hint", ""))))
            if self.path == "/otp":
                return self._send(smart.otp_inject(b["request_id"], b["code"]))
            if self.path == "/otp_wait":
                rid = b["request_id"]
                return self._send(W.run(lambda lb: smart.wait_otp(lb, rid, int(b.get("timeout", 55))),
                                        timeout=int(b.get("timeout", 55)) + 15))
            if self.path == "/ai_step":
                goal = b.get("goal", "complete the page")
                return self._send(W.run(lambda lb: smart.ai_step(lb, goal), timeout=150))
            if self.path == "/new_tab":
                return self._send(W.run(lambda lb: lb.new_tab(b.get("url"))))
            if self.path == "/restart":
                return self._send(W.run(lambda lb: lb.restart()))
            if self.path == "/eval":
                return self._send(W.run(lambda lb: lb.eval_js(b["expr"])))
            return self._send({"error": "unknown POST"})
        except Exception as e:
            try:
                W.run(lambda lb: lb.log(f"ERR {self.path}: {str(e)[:150]}"))
            except Exception:
                pass
            return self._send({"ok": False, "error": str(e)[:300]}, code=500)


if __name__ == "__main__":
    print("starting browser worker (persistent warm context)...")
    W = BrowserWorker()
    LB_REF["lb"] = W.lb
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    W.run(lambda lb: lb.log(f"daemon v2 listening on :{PORT}"))
    print(f"live browser daemon v2 on 127.0.0.1:{PORT}")
    srv.serve_forever()
