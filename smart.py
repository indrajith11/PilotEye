#!/usr/bin/env python3
"""Smart layer: profile autofill, captcha handling, AI vision step, OTP bridge.
All functions take the LiveBrowser instance `lb`.
"""
import json, os, re, subprocess, time

BASE = os.path.dirname(os.path.abspath(__file__))
PROFILE_PATH = os.path.join(BASE, "profile.json")
OTP_SLOT_DIR = os.path.join(BASE, "otp_slots")

AUTOFILL_JS = """
(profile) => {
  const setVal = (el, v) => {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, v);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur', {bubbles: true}));
  };
  const fields = profile.fields || profile;
  const report = [];
  const els = document.querySelectorAll('input,textarea');
  const done = new Set();
  const match = (text) => {
    text = (text || '').toLowerCase();
    for (const [key, kws] of Object.entries(fields)) {
      if (!Array.isArray(kws) || key === '_values') continue;
      if (done.has(key)) continue;
      for (const kw of kws) {
        if (text.includes(kw)) return key;
      }
    }
    return null;
  };
  const values = fields._values || {};
  for (const el of els) {
    const t = (el.type || 'text').toLowerCase();
    if (['submit', 'button', 'image', 'file', 'hidden'].includes(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0) continue;
    let key = match(el.id + ' ' + el.name + ' ' + (el.placeholder||'') + ' ' +
      (el.getAttribute('aria-label')||''));
    if (!key) {
      let lab = '';
      if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) lab = l.innerText; }
      if (!lab) { const p = el.closest('label'); if (p) lab = p.innerText; }
      key = match(lab);
    }
    if (key && values[key] !== undefined) {
      if (t === 'checkbox') { if (!el.checked) el.click(); }
      else if (t === 'radio') {
        if ((el.value||'').toLowerCase().includes('yes') || values[key] === el.value) { if(!el.checked) el.click(); }
      }
      else setVal(el, String(values[key]));
      done.add(key);
      report.push([key, el.id || el.name || key]);
    }
  }
  return report;
}
"""


def load_profile():
    with open(PROFILE_PATH) as f:
        return json.load(f)


def autofill(lb):
    """Batch-fill profile fields in ONE JS eval. Returns filled report."""
    prof = load_profile()
    values = {k: v for k, v in prof.items() if not k.startswith("_") and k != "fields"}
    keywords = prof.get("fields", {})
    payload = {"fields": {**keywords, "_values": values}}
    with lb.lock:
        try:
            report = lb.page.evaluate(AUTOFILL_JS, payload)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "filled": []}
    lb.frame("autofill")
    lb._touch(last_action=f"autofill x{len(report)}")
    return {"ok": True, "filled": report}


# ---------------- vision helper ----------------
def vision(prompt, image_path, timeout=60):
    """Call z-ai vision CLI with local image; return text response."""
    out = os.path.join(SESSION_TMP(), f"vision_{int(time.time()*1000)}.json")
    try:
        subprocess.run(["z-ai", "vision", "-p", prompt, "-i", image_path, "-o", out],
                       capture_output=True, timeout=timeout)
        if os.path.exists(out):
            with open(out) as f:
                data = json.load(f)
            os.remove(out)
            return extract_text(data)
        return ""
    except Exception as e:
        return f"__error__ {e}"


def SESSION_TMP():
    os.makedirs(os.path.join(BASE, "sessions", "live"), exist_ok=True)
    return os.path.join(BASE, "sessions", "live")


def extract_text(data):
    try:
        if isinstance(data, dict):
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
            for k in ("content", "response", "output", "text", "result"):
                if k in data:
                    return data[k]
        return json.dumps(data)[:2000]
    except Exception:
        return str(data)[:2000]


# ---------------- captcha ----------------
def detect_captcha(lb):
    snap = lb.snapshot()
    return {"ok": True, "captcha": snap.get("captcha", []),
            "url": snap.get("url", "")}


def solve_captcha(lb, max_try=3):
    """Layered solving:
    1) reCAPTCHA/hCaptcha/Turnstile checkbox iframe -> click it
    2) image/text/math captcha -> element screenshot -> VLM reads it -> fill
    Falls back to human flag when it cannot solve (compliance-safe)."""
    for attempt in range(max_try):
        snap = lb.snapshot()
        caps = snap.get("captcha", [])
        if not caps:
            return {"ok": True, "solved": True, "note": "no captcha visible"}
        handled = False
        for c in caps:
            if c["tag"] == "iframe":
                # click checkbox zone (left side of widget)
                lb.hover(c["x"] + 30, c["y"] + 30)
                time.sleep(0.4)
                try:
                    lb.page.mouse.click(c["x"] + 30, c["y"] + 30)
                    handled = True
                    time.sleep(2.5)
                except Exception:
                    pass
            elif c["tag"] in ("img", "canvas") and c["w"] > 40:
                img_path = os.path.join(SESSION_TMP(), f"captcha_{attempt}.png")
                try:
                    lb.page.screenshot(path=img_path, clip={"x": c["x"], "y": c["y"],
                                      "width": c["w"], "height": c["h"]}, type="png")
                except Exception:
                    continue
                txt = vision("This is a captcha image. Read the exact characters/calculation result shown. "
                             "If it is a math captcha (like 3+4) give the numeric answer. "
                             "Reply with ONLY the characters/answer, nothing else.", img_path)
                txt = (txt or "").strip().replace(" ", "")
                if txt and not txt.startswith("__error__"):
                    # find the captcha input box
                    snap2 = lb.snapshot()
                    target = None
                    for el in snap2["elements"]:
                        blob = (el["id"] + " " + el["name"] + " " + el["label"] + " " + el["ph"]).lower()
                        if "captcha" in blob and el["tag"] == "input":
                            target = el["ref"]; break
                    if target:
                        lb.fill(target, txt)
                        handled = True
                        time.sleep(1.0)
                os.remove(img_path) if os.path.exists(img_path) else None
        # re-check
        snap3 = lb.snapshot()
        if not snap3.get("captcha"):
            lb.frame("captcha_solved")
            return {"ok": True, "solved": True, "attempts": attempt + 1}
        if handled:
            time.sleep(1.5)
            continue
    lb.frame("captcha_needs_human")
    return {"ok": False, "solved": False,
            "note": "captcha could not be solved automatically; human review needed",
            "frame": lb.latest_frame()}


# ---------------- OTP ----------------
def request_otp(lb, source="email", hint=""):
    """Create OTP request slot. source=email: background IMAP fetcher runs.
    source=mobile: user provides via POST /otp."""
    rid = f"otp_{int(time.time())}"
    slot = {"id": rid, "source": source, "hint": hint, "code": None,
            "ts": time.time(), "status": "waiting"}
    os.makedirs(OTP_SLOT_DIR, exist_ok=True)
    with open(os.path.join(OTP_SLOT_DIR, rid + ".json"), "w") as f:
        json.dump(slot, f)
    lb.otp_state = {"request_id": rid, "source": source, "hint": hint,
                    "status": "waiting", "started": time.strftime("%H:%M:%S")}
    lb._touch(last_action=f"otp request {source}")
    lb.frame("otp_request")
    if source == "email":
        t = threading.Thread(target=_email_otp_worker, args=(rid, hint), daemon=True)
        t.start()
    return {"ok": True, "request_id": rid, "source": source}


import threading  # noqa: E402  (used by request_otp worker)


def _email_otp_worker(rid, hint):
    try:
        import otp_bridge
        code = otp_bridge.fetch_email_otp(hint=hint, max_wait=50)
        path = os.path.join(OTP_SLOT_DIR, rid + ".json")
        with open(path) as f:
            slot = json.load(f)
        if code:
            slot["code"] = code
            slot["status"] = "resolved"
        else:
            slot["status"] = "failed"
        with open(path, "w") as f:
            json.dump(slot, f)
    except Exception:
        pass


def otp_inject(rid, code):
    path = os.path.join(OTP_SLOT_DIR, rid + ".json")
    if not os.path.exists(path):
        return {"ok": False, "error": "no such otp request"}
    with open(path) as f:
        slot = json.load(f)
    slot["code"] = code
    slot["status"] = "resolved"
    with open(path, "w") as f:
        json.dump(slot, f)
    return {"ok": True}


def wait_otp(lb, rid, timeout=55):
    """Poll slot every 1s; fill page OTP inputs automatically when resolved."""
    path = os.path.join(OTP_SLOT_DIR, rid + ".json")
    deadline = time.time() + timeout
    code = None
    while time.time() < deadline:
        try:
            with open(path) as f:
                slot = json.load(f)
            if slot.get("code"):
                code = slot["code"]
                break
            if slot.get("status") == "failed":
                break
        except Exception:
            pass
        time.sleep(1)
    if not code:
        return {"ok": False, "error": "otp timeout"}
    # type it into the most likely OTP input
    snap = lb.snapshot()
    target = None
    for el in snap["elements"]:
        blob = (el["id"] + " " + el["name"] + " " + el["label"] + " " + el["ph"]).lower()
        if el["tag"] == "input" and any(k in blob for k in ("otp", "code", "verify", "verification")):
            target = el["ref"]; break
    if not target:
        for el in snap["elements"]:
            if el["tag"] == "input" and el["type"] in ("text", "tel", "number") and not el["value"]:
                target = el["ref"]; break
    if target:
        lb.fill(target, code)
    lb.otp_state = None
    lb._touch(last_action=f"otp {code} typed")
    return {"ok": True, "code": code, "typed_into": target}


# ---------------- AI autonomous step ----------------
def ai_step(lb, goal):
    """One perception->decision->action cycle using VLM (screenshot + element list)."""
    snap = lb.snapshot(frame_tag="ai_look")
    els = snap["elements"]
    compact = []
    for el in els[:120]:
        line = (f"{el['ref']} [{el['tag']}"
                + (f" {el['type']}" if el["type"] else "")
                + (f" req" if el["req"] else "")
                + (f" value={el['value']}" if el["value"] else "")
                + (f" CHECKED" if el.get("checked") else "")
                + (f" sel_text={el['sel_text']}" if el.get("sel_text") else "")
                + f"] label={el['label']!r} text={el['text']!r}")
        compact.append(line)
    caps = snap.get("captcha", [])
    _p = load_profile()
    cand = ", ".join([str(v) for v in [_p.get("full_name",""), _p.get("current_title",""), _p.get("email",""), _p.get("phone",""), _p.get("location",""), f'{_p.get("years_experience","")}yrs experience'] if v]).strip(", ")
    prompt = f"""You ARE the browser agent. GOAL: {goal}
URL: {snap.get('url','')}  TITLE: {snap.get('title','')}

INTERACTIVE ELEMENTS:
{chr(10).join(compact)}

CAPTCHA SIGNALS: {json.dumps(caps)}

Decide the SINGLE next action. Return ONLY minified JSON, no markdown:
{{"action":"click|fill|select|upload|scroll|captcha|otp|done|stuck","ref":"eN","value":"","reason":"one short line"}}
Rules:
- fill=type text into ref; click=click ref; select=choose value for ref
- For select elements: if sel_text already matches the goal, DO NOT re-select — the field is DONE, move to the next subtask
- If a field already contains the requested value, it is done — move on, never repeat an action on a filled field
- action=captcha when captcha must be solved before proceeding
- action=otp when page is waiting for an OTP/verification code
- If a form is on screen, fill the next empty required field using context (candidate: {cand}).
- If everything needed is complete and a submit/next button exists, click it.
- done only when goal visibly achieved (confirmation message/thank you page)."""
    img = os.path.join(BASE, "sessions", "live", lb.latest_frame()) if lb.latest_frame() else None
    resp = vision(prompt, img) if img and os.path.exists(img) else ""
    m = re.search(r"\{.*\}", resp, re.S)
    if not m:
        return {"ok": False, "error": "AI no-parse", "raw": resp[:300]}
    try:
        decision = json.loads(m.group(0))
    except Exception:
        return {"ok": False, "error": "AI json-bad", "raw": resp[:300]}
    act = decision.get("action", "stuck")
    ref = decision.get("ref", "")
    val = decision.get("value", "")
    result = {"decision": decision}
    try:
        if act == "click": result["exec"] = lb.click(ref)
        elif act == "fill": result["exec"] = lb.fill(ref, val)
        elif act == "select": result["exec"] = lb.select(ref, val)
        elif act == "scroll": result["exec"] = lb.scroll(int(val or 400))
        elif act == "captcha": result["exec"] = solve_captcha(lb)
        elif act == "otp":
            r = request_otp(lb, "email", hint=snap.get("title", ""))
            result["exec"] = wait_otp(lb, r["request_id"])
        elif act == "done": result["exec"] = {"ok": True, "done": True}
        else: result["exec"] = {"ok": False, "stuck": decision.get("reason", "")}
    except Exception as e:
        result["exec"] = {"ok": False, "error": str(e)[:200]}
    return result
