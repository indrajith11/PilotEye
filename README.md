# PilotEye — AI Live Browser

An AI-operated headless browser with live screen perception, code-connected mouse/keyboard control, vision-based page understanding, email-OTP automation, and an autonomous goal loop (`ai-loop`) that can drive multi-step web workflows — originally built to run job-application pipelines end-to-end (form fill → resume upload → email OTP → submit → confirmation detection).

**First fully autonomous submission achieved:** a Greenhouse-hosted application (Bugcrowd) filled, email-OTP-verified via IMAP in ~20s, and submitted with zero human input — confirmed on the `/confirmation` page.

---

## Why

Driving a browser with LLM agents usually dies on three things: **speed** (re-snapshotting the whole DOM every step), **threading** (Playwright's sync API explodes when called from HTTP handler threads), and **state blindness** (the model can't tell whether a checkbox/select is already set, so it loops forever). PilotEye solves all three:

| Problem | Solution |
|---|---|
| Slow perception | **One `evaluate()` snapshot** (`SNAP_JS`) returns every visible interactive element with ref id, label, text, value, coords, options, `CHECKED` / `sel_text` state, plus captcha signal detection — no per-element round trips |
| Sync-API thread binding | **`BrowserWorker` command queue** — a single worker thread owns Playwright; the HTTP daemon marshals every command through `queue + Event`, so any HTTP request can drive the browser safely |
| AI decision loops | Compact element lines carry `CHECKED`/`sel_text`, the prompt says "already-matched fields = DONE, do not repeat", and the client kills the loop after 3 identical actions |
| Off-screen clicks | `click()` detects coords outside the viewport, `scrollBy`s the element into view, re-snapshots, then clicks |
| React/forms that ignore synthetic events | Autofill + select fallback use **native prototype setters** (`HTMLInputElement.prototype.value` etc.) + `input/change/blur` events — works on React-controlled Greenhouse/Workday-style forms |
| OTP within ~30s windows | `otp_bridge.py` polls Gmail over IMAP every 3s with a prioritized OTP regex; typical pickup ≈ 2s from email arrival |
| Hidden buttons / invisible submit fallbacks | Button click strategy: `#id` → `[name=]` → `button[type=submit]` → **visible-text match** (`button:has-text("SUBMIT APPLICATION")`) → coords |

## Architecture

```
                       HTTP  (127.0.0.1:8765)
  you / your agent  ────────────────────────────┐
  (client.py CLI)                               │
                                                ▼
                                       ┌─────────────────┐
                                       │  daemon.py v2   │  threaded HTTP server
                                       │  BrowserWorker  │  single thread owns Playwright
                                       │  (queue+Event)  │  fixes greenlet thread-binding
                                       └───────┬─────────┘
                                               │
                     ┌─────────────────────────┼──────────────────────────┐
                     ▼                         ▼                          ▼
              engine.py                  smart.py                  otp_bridge.py
   persistent Chromium profile    profile autofill (native      IMAP OTP fetcher
   SNAP_JS perception             setters, batch JS)            3s poll, OTP regex,
   click/fill/select/upload       captcha layer (detect →       prioritized parsing
   mouse_click / scroll / hover   checkbox attempt → VLM        (skips years, prefers
   frame rollover (80 jpeg)       image read → honest human     codes near keywords)
   status.json heartbeat          handoff)                      gmail app-password auth
                                  ai_step / ai-loop (VLM
                                  sees screenshot + compact
                                  elements → JSON decision)
```

Perception is **stateless per call but stateful per page**: cookies/localStorage survive restarts via the persistent profile (`sessions/live/chrome_profile/`), so logins stick between runs.

## Components

| File | Role |
|---|---|
| `engine.py` | `LiveBrowser`: persistent-context Chromium, stealth patches (UA, `webdriver=undefined`, languages/plugins), tracker blocklist, `SNAP_JS` single-eval perception, all actions, jpeg frame rollover, `status.json` heartbeat |
| `smart.py` | `autofill()` batch form filler w/ keyword field mapping; `solve_captcha()` layered solver; `request_otp/wait_otp/otp_inject`; `ai_step()` VLM decision loop; `vision()` pluggable image-to-text hook |
| `otp_bridge.py` | Standalone Gmail IMAP OTP fetcher — reusable in any project |
| `daemon.py` | Threaded HTTP API on `127.0.0.1:8765`, BrowserWorker marshaling, all endpoints |
| `client.py` | CLI over the HTTP API — every daemon command, plus `ai-loop` with repeat-action guard |
| `profile.example.json` | Candidate profile template for autofill (rename to `profile.json`, fill your data) |

## Quick start

```bash
# 1) deps
pip install -r requirements.txt
python -m playwright install chromium

# 2) your data (never committed)
cp profile.example.json profile.json    # then edit with your real data

# 3) email OTP bridge (optional, for OTP-gated portals)
export GMAIL_ADDRESS=you@gmail.com
export GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   # Google app password (IMAP enabled)

# 4) run
python daemon.py &                      # headless browser + API on 127.0.0.1:8765
python client.py nav "https://example.com/form"
python client.py autofill               # batch-fill from profile.json
python client.py ai-loop "Fill and submit this form correctly" 12
```

## CLI

```
python client.py status                 # live heartbeat: url, phase, captcha hits, last frame
python client.py nav <url>              # navigate + wait idle
python client.py snap [--shot]          # element list (e1, e2, ...) [+ save frame]
python client.py click <ref|css>        # smart click (id/name/text/submit/coords fallback)
python client.py fill <ref|css> <text>
python client.py select <ref|css> <value|label>   # native-setter fallback for React forms
python client.py mclick <x> <y>         # raw mouse click at viewport coords (captchas)
python client.py upload <ref|css> <file>          # file input (resume/CV)
python client.py autofill               # profile-driven batch fill
python client.py captcha                # detect + attempt solve
python client.py otp-request [email|mobile]       # start OTP capture
python client.py ai <goal>              # one vision decision + action
python client.py ai-loop <goal> [n]     # autonomous loop (default 12 steps)
python client.py tab <url>              # open url in a new tab
python client.py restart | idle | log
```

## HTTP API (127.0.0.1:8765)

`GET /status /snapshot /shot /log` · `POST /nav /click /fill /select /press /scroll /hover /mouse_click /upload /new_tab /wait_idle /autofill /captcha_detect /captcha_solve /otp_request /otp /otp_wait /ai_step /eval /restart`

Every mutating endpoint returns a fresh frame reference, so an agent can drive the whole flow from response payloads without extra polls.

## The AI decision loop

`ai_step(goal)` sends the **screenshot + compact element list** to a vision model and expects strict JSON back:

```json
{"action": "click|fill|select|upload|scroll|captcha|otp|done|stuck",
 "ref": "e12", "value": "...", "reason": "why"}
```

The prompt embeds three hard rules that eliminated the classic failure modes:
1. fields whose `value`/`sel_text` already match the profile are **DONE — never repeat**;
2. never re-click a `CHECKED` box;
3. if the goal is visibly achieved (confirmation text), return `done`.

`vision()` shells out to a `z-ai vision` CLI in this build — swap the ~10-line function for any vision API (OpenAI-compatible `chat.completions` with image input, Gemini, Qwen-VL, GLM-4V…) and everything else stays the same.

## Captcha policy (by design)

- **Detect** every captcha family from DOM signals (reCAPTCHA / hCaptcha / Turnstile / custom).
- **Attempt** the compliant path only: checkbox click, math/text captcha via vision, simple image-challenge reads.
- **Hand off honestly** when a challenge is adversarial (hCaptcha anti-AI texture, reCAPTCHA risk-engine hangs on datacenter IPs). The tool reports `PENDING_CAPTCHA_HUMAN` instead of pretending — no captcha-solving-service integration, no bypass tooling. Challenges exist to prove humanity; this tool respects that line.

## Field-tested notes (battle scars)

- **Playwright sync API is thread-bound** — the only safe pattern is a single owner thread + command queue. This alone is worth copying.
- **React forms ignore direct `.value=`** — use `Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el, v)` + bubbled `input/change/blur`.
- **Clicking a checkbox you just set with the native setter double-toggles it** — either `.click()` alone, or setter alone + events.
- **Greenhouse-style selects often have no `id`/`name`** — fall back to rect/coords matching + `elementFromPoint`.
- **Hidden placeholder `<button type=submit>`** fools naive selectors; match buttons by visible text.
- **reCAPTCHA/Turnstile/hCaptcha from datacenter IPs** mostly will not pass regardless of click realism — plan a human step or run from a residential network.

## Repository safety

`profile.json`, `credentials/`, `sessions/`, `chrome_profile/`, OTP slots, frames and logs are git-ignored (see `.gitignore`) — they carry personal data and live session cookies. The repo ships only code + `profile.example.json`.

## Disclaimer

Built for **personal job-application automation and web-automation research**. You are responsible for complying with every site's Terms of Service, robots policies, and applicable law when you point this at any website. The authors provide this code as-is, without warranty, and do not endorse misuse.
