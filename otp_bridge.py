#!/usr/bin/env python3
"""Email OTP fetcher via IMAP. Reads gmail.env credentials.
Speed: 3s polling loop, newest-first, regex OTP extraction with 20xx filter."""
import email, imaplib, os, re, ssl, time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ENV_PATH = "/home/z/my-project/credentials/gmail.env"


def creds():
    addr = pwd = None
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line.startswith("GMAIL_ADDRESS="):
                addr = line.split("=", 1)[1].strip()
            elif line.startswith("GMAIL_APP_PASSWORD="):
                pwd = line.split("=", 1)[1].strip()
    return addr, pwd


OTP_HINT_WORDS = ("otp", "code", "verify", "verification", "one time", "passcode", "password")


def _decode_part(part):
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="ignore")
    except Exception:
        return ""


def _extract_code(text):
    if not text:
        return None
    # prefer code near hint words on same line
    for line in text.splitlines():
        low = line.lower()
        if any(w in low for w in OTP_HINT_WORDS):
            nums = re.findall(r"\b(\d{4,8})\b", line)
            for n in nums:
                if not n.startswith("20") and not n.startswith("19"):
                    return n
    nums = re.findall(r"\b(\d{4,8})\b", text)
    for n in nums:
        if not n.startswith("20") and not n.startswith("19"):
            return n
    # alphanumeric codes: 6-10 chars with BOTH letters and digits, near hint words
    for line in text.splitlines():
        low = line.lower()
        if any(w in low for w in OTP_HINT_WORDS) or "copy and paste" in low:
            for cand in re.findall(r"\b([A-Za-z0-9]{6,10})\b", line):
                if re.search(r"[A-Za-z]", cand) and re.search(r"\d", cand):
                    return cand
    for cand in re.findall(r"\b([A-Za-z0-9]{6,10})\b", text):
        if re.search(r"[A-Za-z]", cand) and re.search(r"\d", cand):
            return cand
    # mixed-case codes like U90Gwpym (8 chars, letters+digits)
    m = re.search(r"\b([A-Za-z0-9]{8})\b", text)
    if m and re.search(r"[A-Za-z]", m.group(1)) and re.search(r"\d", m.group(1)):
        return m.group(1)
    return None


def fetch_email_otp(hint="", max_wait=50, poll=3):
    """Poll INBOX for recent mail containing an OTP. Returns code or None."""
    addr, pwd = creds()
    if not addr or not pwd:
        return None
    ctx = ssl.create_default_context()
    deadline = time.time() + max_wait
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    while time.time() < deadline:
        m = None
        try:
            m = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ctx)
            m.login(addr, pwd)
            m.select("INBOX", readonly=True)
            date_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")
            typ, data = m.search(None, f'(SINCE "{date_str}")')
            if typ == "OK" and data and data[0]:
                ids = data[0].split()[-8:]  # newest 8
                for mid in reversed(ids):
                    typ2, mdata = m.fetch(mid, "(RFC822)")
                    if typ2 != "OK" or not mdata or mdata[0] is None:
                        continue
                    raw = mdata[0][1]
                    msg = email.message_from_bytes(raw)
                    try:
                        d = parsedate_to_datetime(msg.get("Date", ""))
                        if d.tzinfo is None:
                            d = d.replace(tzinfo=timezone.utc)
                        if d < cutoff:
                            continue
                    except Exception:
                        pass
                    subj = str(msg.get("Subject", ""))
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            ct = part.get_content_type()
                            if ct in ("text/plain", "text/html"):
                                body += _decode_part(part)
                    else:
                        body = _decode_part(msg)
                    text = subj + "\n" + re.sub(r"<[^>]+>", " ", body)
                    code = _extract_code(text)
                    if code:
                        return code
        except Exception:
            pass
        finally:
            try:
                if m: m.logout()
            except Exception:
                pass
        time.sleep(poll)
    return None


if __name__ == "__main__":
    import sys
    hint = sys.argv[1] if len(sys.argv) > 1 else ""
    t0 = time.time()
    code = fetch_email_otp(hint=hint, max_wait=int(sys.argv[2]) if len(sys.argv) > 2 else 50)
    print(f"OTP={'FOUND' if code else 'NOT FOUND'} code={code} took={time.time()-t0:.0f}s")
