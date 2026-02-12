import os
import time
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
from typing import List, Dict, Any, Optional, Tuple

IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"
API_BASE = "https://generativelanguage.googleapis.com/v1beta"

EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PREFERRED_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "120"))
MAX_EMAILS_PER_RUN = int(os.getenv("MAX_EMAILS_PER_RUN", "20"))

DEFAULT_MAX_OUTPUT_TOKENS = 768  # לא חופר, אבל מאפשר "100 מילים"
GEMINI_RETRIES = 4

# -------- logging --------
def log(msg: str) -> None:
    print(f"[BOT] {msg}", flush=True)

# -------- headers/utils --------
def decode_mime_header(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    return "".join(
        t.decode(enc or "utf-8", errors="ignore") if isinstance(t, bytes) else t
        for t, enc in parts
    ).strip()

def clean_email_body(text: str) -> str:
    # ניקוי עדין - לא אגרסיבי מדי
    patterns = [
        r"-----Original Message-----.*",
        r"^>.*$",
        r"^\s*On .*wrote:\s*$",
        r"--\s*$",
    ]
    for p in patterns:
        text = re.split(p, text, flags=re.IGNORECASE | re.MULTILINE)[0]
    return text.strip()

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

def extract_length_instruction(text: str) -> Tuple[Optional[str], bool]:
    m = re.search(r"(\d+)\s*מילים", text)
    if m:
        return f"כתוב בדיוק {m.group(1)} מילים.", True
    if "קצר" in text:
        return "כתוב תשובה קצרה מאוד (שורה–שתיים).", False
    if "ארוך" in text:
        return "כתוב תשובה מפורטת יותר מהרגיל (אבל בלי חפירות).", False
    return None, False

def build_prompt(user_text: str) -> Tuple[str, int]:
    rule, exact = extract_length_instruction(user_text)

    prompt = (
        "ענה בעברית, בטון טבעי ולא רשמי.\n"
        "ענה רק למה שהתבקש.\n"
        "בלי תקצירים, בלי סעיפים קבועים, בלי הרצאות.\n"
        "אם המשתמש ביקש אורך מפורש (למשל '100 מילים') חובה לציית.\n"
    )

    max_tokens = DEFAULT_MAX_OUTPUT_TOKENS
    if rule:
        prompt += rule + "\n"
        if exact:
            max_tokens = 900  # מרווח ל-100 מילים בלי להיגרר למגילה

    prompt += "\nהבקשה:\n" + user_text.strip()
    return prompt, max_tokens

# -------- SMTP send (עברית ודאית) --------
def send_email(to: str, subject: str, body: str, reply_to: Optional[str] = None) -> None:
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

# -------- Gemini model selection --------
def list_models() -> List[Dict[str, Any]]:
    url = f"{API_BASE}/models?key={GEMINI_API_KEY}"
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        log(f"models.list failed {r.status_code}: {r.text[:300]}")
        return []
    return r.json().get("models", [])

def pick_model(preferred: str) -> str:
    models = list_models()
    candidates = []
    for m in models:
        name = m.get("name", "")
        methods = m.get("supportedGenerationMethods", []) or []
        if name.startswith("models/") and "generateContent" in methods:
            candidates.append(name.split("/", 1)[1])
    if preferred in candidates:
        return preferred
    if candidates:
        log(f"Preferred model '{preferred}' not available. Using '{candidates[0]}'")
        return candidates[0]
    return preferred

# -------- Gemini call (עם retry + הסבר שגיאה אמיתי) --------
def call_gemini(prompt: str, model_id: str, max_tokens: int) -> Tuple[bool, str]:
    url = f"{API_BASE}/models/{model_id}:generateContent?key={GEMINI_API_KEY}"
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
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return True, text

            # אם 404 - כנראה מודל לא נכון -> לא עושים retry עיוור
            if r.status_code == 404:
                return False, f"שגיאת Gemini 404: המודל '{model_id}' לא נמצא/לא זמין."

            # 401/403 - מפתח/הרשאה
            if r.status_code in (401, 403):
                return False, f"שגיאת Gemini {r.status_code}: בעיית הרשאה/מפתח API."

            # זמני: 429/5xx -> retry עם backoff
            if r.status_code in (429, 500, 502, 503, 504):
                sleep_s = min(16, 2 ** attempt)
                last_err = f"שגיאת Gemini {r.status_code}: עומס/מגבלה זמנית. ניסיון נוסף בעוד {sleep_s} שניות."
                log(last_err)
                time.sleep(sleep_s)
                continue

            # כל שאר השגיאות
            return False, f"שגיאת Gemini {r.status_code}: {r.text[:400]}"

        except Exception as e:
            sleep_s = min(16, 2 ** attempt)
            last_err = f"חריגה בתקשורת עם Gemini: {e}. ניסיון נוסף בעוד {sleep_s} שניות."
            log(last_err)
            time.sleep(sleep_s)

    return False, last_err or "Gemini נכשל ללא פירוט."

# -------- IMAP: fetch only UNANSWERED + mark ANSWERED to prevent duplicates --------
def get_recent_unanswered_emails(mail: imaplib.IMAP4_SSL, lookback_minutes: int, max_count: int):
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    since_day = cutoff.strftime("%d-%b-%Y")

    # ✅ קריטי: רק UNANSWERED כדי למנוע כפילויות
    result, data = mail.search(None, f'(SINCE {since_day} UNANSWERED)')
    if result != "OK":
        raise RuntimeError(f"IMAP search failed: {result} {data}")

    ids = data[0].split()
    ids = ids[-max_count:]

    out = []
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

        out.append({
            "imap_num": num,           # 👈 צריך בשביל לסמן ANSWERED
            "from": sender,
            "subject": subject,
            "body": body,
            "message_id": message_id
        })

    return out

def mark_answered(mail: imaplib.IMAP4_SSL, imap_num: bytes) -> None:
    # ✅ מסמן כ-Answered וגם Seen כדי שלא יחזור שוב
    mail.store(imap_num, "+FLAGS", "\\Answered")
    mail.store(imap_num, "+FLAGS", "\\Seen")

# -------- main --------
def main():
    if not EMAIL_ACCOUNT or not EMAIL_PASSWORD or not GEMINI_API_KEY:
        raise RuntimeError("Missing EMAIL_ACCOUNT / EMAIL_PASSWORD / GEMINI_API_KEY")

    log("Run start")

    # בוחרים מודל אמיתי (מונע 404)
    model_id = pick_model(PREFERRED_MODEL)
    log(f"Using model: {model_id}")

    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    mail.select("INBOX")

    emails = get_recent_unanswered_emails(mail, LOOKBACK_MINUTES, MAX_EMAILS_PER_RUN)
    log(f"Unanswered recent emails: {len(emails)}")

    for m in emails:
        user_text = (m["body"] or "").strip()

        if not user_text:
            reply = "קיבלתי הודעה ריקה. תכתוב בקשה קצרה וברורה."
            send_email(m["from"], f"Re: {m['subject']}", reply, m["message_id"])
            mark_answered(mail, m["imap_num"])
            continue

        prompt, max_tokens = build_prompt(user_text)
        ok, out = call_gemini(prompt, model_id, max_tokens)

        if not ok:
            # ✅ אין “fallback בדיחה וירוס”.
            # במקום זה: הודעת תקלה קצרה + סיבת התקלה (כדי שתדע מה קורה)
            reply = (
                "כרגע יש תקלה זמנית במנוע התשובות של גוגל.\n\n"
                f"פירוט: {out}\n\n"
                "נסה שוב בעוד דקה."
            )
        else:
            reply = out.strip()

        send_email(m["from"], f"Re: {m['subject']}", reply, m["message_id"])

        # ✅ הכי חשוב: לסמן כטופל כדי לא לענות שוב
        mark_answered(mail, m["imap_num"])
        log(f"Replied+marked answered: {m['from']} | {m['message_id']}")

    mail.logout()
    log("Run end")

if __name__ == "__main__":
    main()
