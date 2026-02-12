import os
import time
import json
import imaplib
import email
import smtplib
import requests
import markdown
import re
from email.mime.text import MIMEText
from email.header import decode_header
from email.utils import parseaddr
from datetime import datetime, timedelta, timezone

IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"
API_BASE = "https://generativelanguage.googleapis.com/v1beta"

EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

STATE_FILE = "bot_state.json"
LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "60"))
MAX_EMAILS_PER_RUN = int(os.getenv("MAX_EMAILS_PER_RUN", "20"))

# תשובות קצרות ולא חופרות
DEFAULT_MAX_OUTPUT_TOKENS = 512

# ניסיונות + backoff (מוריד נפילות 429/5xx)
GEMINI_RETRIES = 4

def log(msg: str) -> None:
    print(f"[BOT] {msg}", flush=True)

# -------------------- state --------------------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"replied_message_ids": []}
    except Exception:
        return {"replied_message_ids": []}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"Failed saving state: {e}")

# -------------------- email parsing --------------------
def decode_mime_header(value):
    if not value:
        return ""
    parts = decode_header(value)
    return "".join(
        t.decode(enc or "utf-8", errors="ignore") if isinstance(t, bytes) else t
        for t, enc in parts
    ).strip()

def clean_email_body(text: str) -> str:
    patterns = [
        r"-----Original Message-----.*",
        r"^>.*$",
        r"^\s*On .*wrote:\s*$",
        r"--\s*$",
    ]
    for p in patterns:
        text = re.split(p, text, flags=re.IGNORECASE | re.MULTILINE)[0]
    return text.strip()

def extract_length_instruction(text: str):
    m = re.search(r"(\d+)\s*מילים", text)
    if m:
        return f"כתוב בדיוק {m.group(1)} מילים.", True
    if "קצר" in text:
        return "כתוב תשובה קצרה מאוד (שורה–שתיים).", False
    if "ארוך" in text:
        return "כתוב תשובה מפורטת יותר מהרגיל (אבל בלי חפירות).", False
    return None, False

def build_prompt(user_text: str):
    rule, exact = extract_length_instruction(user_text)

    prompt = (
        "ענה בעברית, בטון טבעי ולא רשמי.\n"
        "ענה רק למה שהתבקש.\n"
        "בלי תקצירים, בלי סעיפים קבועים, בלי הרצאות.\n"
        "אם יש בקשת אורך מפורשת (למשל '100 מילים') חובה לציית.\n"
    )

    max_tokens = DEFAULT_MAX_OUTPUT_TOKENS
    if rule:
        prompt += rule + "\n"
        if exact:
            max_tokens = 768

    prompt += "\nהבקשה:\n" + user_text.strip()
    return prompt, max_tokens

def extract_body_from_msg(msg: email.message.Message) -> str:
    body = ""
    html = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="ignore")
            if ctype == "text/plain":
                body += decoded
            elif ctype == "text/html":
                html += decoded
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="ignore")
            if msg.get_content_type() == "text/plain":
                body = decoded
            elif msg.get_content_type() == "text/html":
                html = decoded

    if not body and html:
        html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
        html = re.sub(r"</p\s*>", "\n", html, flags=re.IGNORECASE)
        html = re.sub(r"<[^>]+>", "", html)
        body = html

    return clean_email_body(body)

# -------------------- IMAP recent emails --------------------
def get_recent_emails_in_inbox(lookback_minutes: int, max_count: int):
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    since_day = cutoff.strftime("%d-%b-%Y")

    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    mail.select("INBOX")

    result, data = mail.search(None, f'(SINCE {since_day})')
    if result != "OK":
        mail.logout()
        raise RuntimeError(f"IMAP search failed: {result} {data}")

    ids = data[0].split()
    ids = ids[-max_count:]  # רק האחרונים

    messages = []
    for num in reversed(ids):
        result, msg_data = mail.fetch(num, "(RFC822)")
        if result != "OK":
            continue

        msg = email.message_from_bytes(msg_data[0][1])

        # סינון לפי Date אמיתי
        try:
            dt = email.utils.parsedate_to_datetime(msg.get("Date"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_utc = dt.astimezone(timezone.utc)
        except Exception:
            dt_utc = datetime.now(timezone.utc)

        if dt_utc < cutoff:
            continue

        sender = parseaddr(msg.get("From", ""))[1]
        subject = decode_mime_header(msg.get("Subject")) or "(ללא נושא)"
        message_id = msg.get("Message-ID") or f"<no-id-{num.decode('utf-8','ignore')}>"
        body = extract_body_from_msg(msg)

        messages.append({
            "from": sender,
            "subject": subject,
            "body": body,
            "message_id": message_id
        })

    mail.logout()
    return messages

# -------------------- SMTP send (עברית ודאית) --------------------
def send_email(to, subject, body, reply_to=None):
    html_content = markdown.markdown(body)

    html = f"""
    <html lang="he" dir="rtl">
      <body style="direction: rtl; text-align: right; font-family: Arial, sans-serif;">
        {html_content}
      </body>
    </html>
    """

    msg = MIMEText(html, "html", "utf-8")
    msg["From"] = EMAIL_ACCOUNT
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["In-Reply-To"] = reply_to
        msg["References"] = reply_to

    with smtplib.SMTP_SSL(SMTP_SERVER, 465) as s:
        s.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        s.sendmail(EMAIL_ACCOUNT, to, msg.as_string())

# -------------------- Gemini call + better fallback --------------------
def call_gemini(prompt: str, max_tokens: int):
    url = f"{API_BASE}/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.9, "topP": 0.95, "maxOutputTokens": max_tokens},
    }

    last_err = ""
    for attempt in range(1, GEMINI_RETRIES + 1):
        try:
            r = requests.post(url, json=payload, timeout=60)
            if r.status_code == 200:
                data = r.json()
                return True, data["candidates"][0]["content"]["parts"][0]["text"]

            last_err = f"HTTP {r.status_code}"
            # backoff אמיתי
            if r.status_code in (429, 500, 502, 503, 504):
                sleep_s = min(12, 2 ** attempt)
                log(f"Gemini temporary error {r.status_code}; retry in {sleep_s}s")
                time.sleep(sleep_s)
                continue

            # שגיאה לא זמנית
            log(f"Gemini permanent error: {r.status_code} {r.text[:500]}")
            return False, f"{last_err}: {r.text[:500]}"

        except Exception as e:
            last_err = f"Exception: {e}"
            sleep_s = min(12, 2 ** attempt)
            log(f"Gemini exception; retry in {sleep_s}s: {e}")
            time.sleep(sleep_s)

    return False, last_err or "Unknown Gemini error"

def smart_fallback(user_text: str, err: str) -> str:
    """
    fallback שימושי וקצר:
    - מנסה בכל זאת לענות משהו מינימלי לפי סוג הבקשה
    - בלי אימוג'ים (כדי שלא יזוהה כאנגלית)
    """
    t = (user_text or "").strip()

    # בקשת בדיחה + אורך
    m = re.search(r"(\d+)\s*מילים", t)
    if "בדיחה" in t and m:
        n = int(m.group(1))
        # לא "בדיוק" מושלם תמיד, אבל יהיה בערך ויעמוד ברוח
        return (
            f"ביקשת בדיחה בערך {n} מילים. היתה תקלה זמנית במנוע התשובות, אז הנה אחת קצרה במקום:\n\n"
            "אדם נכנס למכולת ושואל: יש לכם לחם טרי? המוכר אומר: כן, יצא עכשיו מהתנור. "
            "האדם מחייך ואומר: יופי, תגיד לו שיבוא אליי גם מחר, כי אני תמיד שוכח לקנות בזמן."
        )

    if "בדיחה" in t:
        return (
            "הייתה תקלה זמנית במנוע התשובות, אבל הנה בדיחה קצרה:\n\n"
            "למה המחשב הלך לרופא? כי היה לו וירוס."
        )

    # ברירת מחדל: תשובה קצרה + בקשה לחזרה
    return "הייתה תקלה זמנית במנוע התשובות. נסה שוב בעוד דקה."

# -------------------- main --------------------
def main():
    log("Run start")

    state = load_state()
    replied = set(state.get("replied_message_ids", []))

    emails = get_recent_emails_in_inbox(LOOKBACK_MINUTES, MAX_EMAILS_PER_RUN)
    log(f"Recent emails found: {len(emails)} (lookback={LOOKBACK_MINUTES}m)")

    sent_count = 0
    for m in emails:
        mid = m["message_id"]
        if mid in replied:
            continue

        user_text = m["body"]

        if not user_text:
            reply = "קיבלתי הודעה ריקה. תכתוב לי בקשה קצרה וברורה."
        else:
            prompt, max_tokens = build_prompt(user_text)
            ok, out = call_gemini(prompt, max_tokens)
            reply = out.strip() if ok and out else smart_fallback(user_text, out if isinstance(out, str) else "")

        try:
            send_email(m["from"], f"Re: {m['subject']}", reply, mid)
            replied.add(mid)
            sent_count += 1
            log(f"Replied to {m['from']} | mid={mid}")
        except Exception as e:
            log(f"SMTP failed: {e}")

    state["replied_message_ids"] = list(replied)[-5000:]
    save_state(state)

    log(f"Run end. Sent replies: {sent_count}")

if __name__ == "__main__":
    main()
