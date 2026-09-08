#!/usr/bin/env python3
"""LiveBrowser engine: persistent Playwright chromium + fast DOM perception.

Design goals (speed-first):
- persistent user_data_dir  -> cookies/session survive between jobs (no re-login)
- goto wait_until=domcontentloaded -> perception starts immediately
- single-eval element extraction -> 1 round trip per snapshot
- rolling jpeg frames + status.json -> live monitoring
"""
import json, os, time, threading
from playwright.sync_api import sync_playwright

BASE = os.path.dirname(os.path.abspath(__file__))
SESSION_DIR = os.path.join(BASE, "sessions", "live")
FRAME_DIR = os.path.join(SESSION_DIR, "frames")
STATUS_PATH = os.path.join(SESSION_DIR, "status.json")
LOG_PATH = os.path.join(SESSION_DIR, "actions.log")
USER_DATA = os.path.join(BASE, "chrome_profile")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

BLOCK_HOSTS = ("googletagmanager.com", "google-analytics.com", "analytics.",
               "doubleclick.net", "facebook.net", "connect.facebook.com",
               "hotjar.com", "clarity.ms", "segment.io", "amplitude.com",
               "mixpanel.com", "snowplow", "branch.io", "appsflyer.com",
               "optimizely.com", "criteo", "taboola", "outbrain", "pubmatic")

SNAP_JS = """
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const labelOf = el => {
    if (el.id) { try { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return (l.innerText||'').trim().slice(0,90); } catch(e){} }
    const p = el.closest('label'); if (p) return (p.innerText||'').trim().slice(0,90);
    return String(el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') || el.name || '').slice(0,90);
  };
  const out = []; let i = 0;
  const sel = 'input,textarea,select,button,a,[role=button],[role=link],[role=checkbox],[role=radio],[role=combobox],[role=tab],[contenteditable=true],[onclick]';
  for (const el of document.querySelectorAll(sel)) {
    if (out.length > 300) break;
    const tag = el.tagName.toLowerCase();
    let text = (el.innerText || '').trim();
    if (tag === 'a' && !text) continue;
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    const it = { ref: 'e' + (++i), tag, type: el.type || '', id: el.id || '', name: el.name || '',
      ph: el.placeholder || '', label: labelOf(el), text: text.slice(0, 70),
      value: (el.value || '').slice(0, 60), req: !!el.required,
      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
    if (tag === 'select') { it.options = [...el.options].map(o => o.value || o.text).slice(0, 25);
      it.sel_text = el.selectedOptions[0] ? el.selectedOptions[0].text : ''; }
    if (it.type === 'checkbox' || it.type === 'radio') it.checked = el.checked;
    out.push(it);
  }
  const cap = [];
  for (const el of document.querySelectorAll('img,canvas,iframe,div,span,input')) {
    const s = ((el.id||'') + ' ' + (el.name||'') + ' ' + (el.className||'') + ' ' + (el.src||'')).toLowerCase();
    if (/captcha|challenge|recaptcha|hcaptcha|turnstile/.test(s) && vis(el)) {
      const r = el.getBoundingClientRect();
      if (r.width < 500 && r.height < 500) cap.push({ tag: el.tagName.toLowerCase(), id: el.id || '',
        src: (el.src || '').slice(0, 200), x: Math.round(r.x), y: Math.round(r.y),
        w: Math.round(r.width), h: Math.round(r.height) });
    }
  }
  return { title: document.title, url: location.href, elements: out, captcha: cap.slice(0, 8) };
}
"""


class LiveBrowser:
    def __init__(self):
        os.makedirs(FRAME_DIR, exist_ok=True)
        os.makedirs(USER_DATA, exist_ok=True)
        self.lock = threading.RLock()
        self.pw = None
        self.ctx = None
        self.page = None
        self.frame_n = 0
        self.phase = "idle"
        self.last_action = "-"
        self.otp_state = None
        self.started_at = time.time()
        self._start()

    # ---------- lifecycle ----------
    def _start(self):
        self.pw = sync_playwright().start()
        self.ctx = self.pw.chromium.launch_persistent_context(
            USER_DATA, headless=True, accept_downloads=True,
            viewport={"width": 1440, "height": 900}, locale="en-IN",
            timezone_id="Asia/Kolkata", user_agent=UA,
            args=["--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage", "--no-sandbox",
                  "--disable-gpu"])
        self.ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'languages',{get:()=>['en-IN','en']});"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});")
        self._apply_blocklist()
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(8000)
        self.log("browser started (persistent context)")

    def _apply_blocklist(self):
        def route(r):
            host = r.request.url.lower()
            if any(b in host for b in BLOCK_HOSTS):
                return r.abort()
            return r.continue_()
        try:
            self.ctx.route("**/*", route)
        except Exception:
            pass

    def restart(self):
        with self.lock:
            try: self.ctx.close()
            except Exception: pass
            self._start()

    # ---------- helpers ----------
    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        try:
            with open(LOG_PATH, "a") as f: f.write(line + "\n")
        except Exception: pass
        return line

    def _touch(self, phase=None, last_action=None):
        if phase: self.phase = phase
        if last_action: self.last_action = last_action
        st = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "phase": self.phase,
              "url": "", "title": "", "last_action": self.last_action,
              "elements": 0, "captcha": [], "otp": self.otp_state,
              "frame": self.latest_frame(), "uptime_s": int(time.time() - self.started_at)}
        try:
            st["url"] = self.page.url
            st["title"] = self.page.title()[:100]
        except Exception: pass
        try:
            with open(STATUS_PATH, "w") as f: json.dump(st, f, indent=1)
        except Exception: pass
        return st

    def latest_frame(self):
        return f"frames/frame_{self.frame_n:04d}.jpg" if self.frame_n else None

    def frame(self, tag=""):
        self.frame_n += 1
        path = os.path.join(FRAME_DIR, f"frame_{self.frame_n:04d}.jpg")
        try:
            self.page.screenshot(path=path, type="jpeg", quality=72)
            if self.frame_n > 80:
                old = os.path.join(FRAME_DIR, f"frame_{self.frame_n-80:04d}.jpg")
                if os.path.exists(old): os.remove(old)
        except Exception as e:
            self.log(f"frame fail: {e}")
            return None
        if tag: self.log(f"frame #{self.frame_n} [{tag}]")
        return f"frames/frame_{self.frame_n:04d}.jpg"

    def shot(self):
        return self.frame("manual")

    # ---------- perception ----------
    def snapshot(self, frame_tag=None):
        with self.lock:
            for attempt in range(4):
                try:
                    data = self.page.evaluate(SNAP_JS)
                    if frame_tag: self.frame(frame_tag)
                    self._touch(phase="ready", last_action="snapshot")
                    return data
                except Exception as e:
                    if attempt == 3:
                        return {"error": str(e)[:200], "url": self.page.url,
                                "title": "", "elements": [], "captcha": []}
                    time.sleep(0.35)
        return {"elements": []}

    def nav(self, url):
        with self.lock:
            url = url if "://" in url else "https://" + url
            self._touch(phase="navigating", last_action=f"nav {url}")
            self.frame("nav")
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
            except Exception as e:
                self.log(f"goto warn: {str(e)[:120]}")
            self.frame("loaded")
            return {"ok": True, "url": self.page.url}

    def wait_idle(self, ms=3500):
        with self.lock:
            try: self.page.wait_for_load_state("networkidle", timeout=ms)
            except Exception: pass
            self.frame("idle")
            return {"ok": True}

    # ---------- actions ----------
    def _resolve(self, sel):
        if sel.startswith("e") and sel[1:].isdigit():
            data = self.page.evaluate(SNAP_JS)
            for el in data["elements"]:
                if el["ref"] == sel:
                    return el
            raise ValueError(f"ref {sel} not found")
        return sel

    def _click_done(self, sel, how):
        self.frame("click")
        self._touch(last_action=f"click {sel} via {how}")
        return {"ok": True, "via": how}

    def click(self, sel):
        with self.lock:
            target = self._resolve(sel)
            if isinstance(target, dict):
                build = None
                if target.get("id"): build = f"#{target['id']}"
                elif target.get("name") and target["tag"] in ("input", "select", "textarea", "button"):
                    build = f"{target['tag']}[name='{target['name']}']"
                ok, how = False, "coords"
                # buttons: match by visible text FIRST (never blanket type=submit)
                if target["tag"] == "button" and target.get("text"):
                    txt = target["text"].replace('"', "").strip()
                    if txt and len(txt) < 40:
                        try:
                            self.page.locator(f'button:has-text("{txt}")').first.click(timeout=2500)
                            return self._click_done(sel, f"button text {txt[:25]}")
                        except Exception:
                            pass
                if target["tag"] == "button" and target.get("type") == "submit":
                    build = "button[type=submit]"
                if build:
                    try:
                        self.page.locator(build).first.click(timeout=2500)
                        ok, how = True, f"selector {build}"
                    except Exception: pass
                if not ok:
                    # ensure element inside viewport before coords click
                    try:
                        vh = self.page.evaluate("window.innerHeight")
                        ty = target["y"] + target["h"] / 2
                        if ty > vh - 10 or ty < 0:
                            self.page.evaluate(f"window.scrollBy(0, {int(ty - vh / 2)})")
                            time.sleep(0.25)
                            data2 = self.page.evaluate(SNAP_JS)
                            for el2 in data2["elements"]:
                                if el2["ref"] == sel:
                                    target = el2
                                    break
                    except Exception:
                        pass
                    self.page.mouse.click(target["x"] + target["w"]/2, target["y"] + target["h"]/2)
                    how = f"coords({int(target['x'] + target['w']/2)},{int(target['y'] + target['h']/2)})"
                self.frame("click")
                self._touch(last_action=f"click {sel} via {how}")
                return {"ok": True, "via": how}
            self.page.locator(sel).first.click(timeout=6000)
            self.frame("click")
            self._touch(last_action=f"click {sel}")
            return {"ok": True, "via": "css"}

    def fill(self, sel, text):
        with self.lock:
            target = self._resolve(sel)
            if isinstance(target, dict):
                build = None
                if target.get("id"): build = f"#{target['id']}"
                elif target.get("name"): build = f"{target['tag']}[name='{target['name']}']"
                ok = False
                if build:
                    try:
                        self.page.locator(build).first.fill(str(text), timeout=2500)
                        ok = True
                    except Exception: pass
                if not ok:
                    self.page.mouse.click(target["x"] + target["w"]/2, target["y"] + target["h"]/2)
                    self.page.keyboard.press("Control+A")
                    self.page.keyboard.type(str(text), delay=8)
                self.frame("fill")
                self._touch(last_action=f"fill {sel}={str(text)[:40]}")
                return {"ok": True}
            self.page.locator(sel).first.fill(str(text), timeout=6000)
            self.frame("fill")
            self._touch(last_action=f"fill {sel}")
            return {"ok": True}

    _SELECT_JS = """(p) => {
      let el = document.elementFromPoint(p.x, p.y);
      while (el && el.tagName !== 'SELECT') el = el.parentElement;
      if (!el) {
        for (const s of document.querySelectorAll('select')) {
          const r = s.getBoundingClientRect();
          if (Math.abs(r.left + r.width / 2 - p.x) < 8 && Math.abs(r.top + r.height / 2 - p.y) < 8) { el = s; break; }
        }
      }
      if (!el) return 'NO_SELECT';
      el.scrollIntoView({ block: 'center' });
      let opt = [...el.options].find(o => o.value === p.v) ||
                [...el.options].find(o => o.text.trim().toLowerCase() === String(p.v).trim().toLowerCase());
      if (!opt) return 'NO_OPT';
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
      setter.call(el, opt.value);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      el.dispatchEvent(new Event('blur', { bubbles: true }));
      return 'OK';
    }"""

    def select(self, sel, value):
        with self.lock:
            target = self._resolve(sel)
            if isinstance(target, dict) and target.get("id"):
                sel2 = f"#{target['id']}"
            elif isinstance(target, dict) and target.get("name"):
                sel2 = f"{target['tag']}[name='{target['name']}']"
            elif isinstance(target, str):
                sel2 = sel  # direct CSS selector
            else:
                sel2 = None  # ref without id/name -> JS fallback
            done = False
            if sel2:
                try:
                    self.page.locator(sel2).first.select_option(value, timeout=3000)
                    done = True
                except Exception:
                    try:
                        self.page.locator(sel2).first.select_option(label=value, timeout=3000)
                        done = True
                    except Exception:
                        done = False
            if not done and isinstance(target, dict):
                cx = target["x"] + target["w"] / 2
                cy = target["y"] + target["h"] / 2
                res = self.page.evaluate(self._SELECT_JS, {"x": cx, "y": cy, "v": str(value)})
                if res != "OK":
                    raise RuntimeError(f"select failed: {res}")
            self.frame("select")
            self._touch(last_action=f"select {sel}={value}")
            return {"ok": True}

    def press(self, key):
        with self.lock:
            self.page.keyboard.press(key)
            self.frame("press")
            self._touch(last_action=f"press {key}")
            return {"ok": True}

    def scroll(self, dy=400):
        with self.lock:
            self.page.mouse.wheel(0, dy)
            time.sleep(0.25)
            self.frame("scroll")
            self._touch(last_action=f"scroll {dy}")
            return {"ok": True}

    def hover(self, x, y):
        with self.lock:
            self.page.mouse.move(x, y, steps=4)
            self._touch(last_action=f"hover {x},{y}")
            return {"ok": True}

    def mouse_click(self, x, y):
        with self.lock:
            self.page.mouse.move(x, y, steps=3)
            time.sleep(0.12)
            self.page.mouse.click(x, y)
            self.frame("mouse_click")
            self._touch(last_action=f"mouse_click {x},{y}")
            return {"ok": True}

    def upload(self, sel, path):
        with self.lock:
            target = self._resolve(sel)
            css = sel
            if isinstance(target, dict):
                if target.get("id"): css = f"#{target['id']}"
                elif target.get("name"): css = f"input[name='{target['name']}']"
                else: css = "input[type=file]"
            self.page.locator(css).first.set_input_files(path, timeout=8000)
            self.frame("upload")
            self._touch(last_action=f"upload {path}")
            return {"ok": True}

    def upload_click(self, trigger_sel, path):
        """Human-like upload: click trigger (label/button) -> file chooser -> set file."""
        with self.lock:
            with self.page.expect_file_chooser(timeout=6000) as fc_info:
                self.page.locator(trigger_sel).first.click()
            fc_info.value.set_files(path)
            self.frame("upload_click")
            self._touch(last_action=f"upload_click {path}")
            return {"ok": True}

    def new_tab(self, url=None):
        with self.lock:
            self.page = self.ctx.new_page()
            self.page.set_default_timeout(8000)
            if url: self.nav(url)
            return {"ok": True, "url": self.page.url}

    def eval_js(self, expr):
        with self.lock:
            try:
                return {"ok": True, "result": self.page.evaluate(expr)}
            except Exception as e:
                return {"ok": False, "error": str(e)[:200]}
