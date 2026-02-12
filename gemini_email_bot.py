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
from typing import List, Dict, Any, Optional, Tuple, Set

IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"
API_BASE = "https://generativelanguage.googleapis.com/v1beta"

EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PREFERRED_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "120"))
MAX_EMAILS_PER_RUN = int(os.getenv("MAX_EMAILS_PER_RUN", "20"))

# יותר “מרווח נשימה” כדי למנוע תשובות קצרות מדי
DEFAULT_MAX_OUTPUT_TOKENS = int(os.getenv("DEFAULT_MAX_OUTPUT_TOKENS", "1200"))
GEMINI_RETRIES = int(os.getenv("GEMINI_RETRIES", "4"))

# ---- Persistent state (מונע תשובות כפולות בין ריצות) ----
STATE_FILE = os.getenv("STATE_FILE", "answered_ids.txt")
STATE_MAX_IDS = int(os.getenv("STATE_MAX_IDS", "5000"))  # למנוע קובץ שמתנפח לנצח

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

# -------- state helpers --------
def load_answered_ids() -> Set[str]:
    if not os.path.exists(STATE_FILE):
        return set()
    out: Set[str] = set()
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s:
                out.add(s)
    return out

def save_answered_id(msg_id: str) -> None:
    # append בלבד (פשוט ומהיר)
    with open(STATE_FILE, "a", encoding="utf-8") as f:
        f.write(msg_id + "\n")

def trim_state_file(max_ids: int) -> None:
    # שומר רק את האחרונים כדי לא לנפח
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        if len(lines) <= max_ids:
            return
        lines = lines[-max_ids:]
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        log(f"State trim failed: {e}")

# -------- length rules/prompt --------
def extract_length_instruction(text: str) -> Tuple[Optional[str], bool, bool]:
    """
    returns: (instruction, exact_word_count, user_requested_short)
    """
    m = re.search(r"(\d+)\s*מילים", text)
    if m:
        return f"כתוב בדיוק {m.group(1)} מילים.", True, False

    # משתמש ביקש קצר: עדיין לא “טלגרפי”, אלא קצר-שלם
    if re.search(r"\bקצר\b", text):
        return "כתוב תשובה קצרה אבל שלמה (לפחות 2–3 משפטים מלאים).", False, True

    if re.search(r"\bארוך\b", text):
        return "כתוב תשובה מפורטת יותר מהרגיל (אבל בלי חפירות).", False, False

    return None, False, False

def build_prompt(user_text: str) -> Tuple[str, int]:
    rule, exact, requested_short = extract_length_instruction(user_text)

    # בסיס: מונע תשובות של 7–12 מילים
    prompt = (
        "ענה בעברית מלאה וזורמת.\n"
        "אל תקטע משפטים באמצע.\n"
        "אל תכתוב תשובה טלגרפית.\n"
        "אם לא נאמר אחרת – כתוב לפחות 3–5 משפטים מלאים.\n"
        "בלי תקצירים ובלי מבני 'סעיפים קבועים' אלא אם התבקש.\n"
        "ענה רק למה שהתבקש.\n"
        "אם המשתמש ביקש אורך מפורש (למשל '100 מילים') חובה לציית.\n"
    )

    max_tokens = DEFAULT_MAX_OUTPUT_TOKENS

    if rule:
        prompt += rule + "\n"
        if exact:
            # מרווח ל-100+ מילים
            max_tokens = max(max_tokens, 1400)
        elif requested_short:
            # קצר, אבל לא קיצוץ אגרסיבי
            max_tokens = min(max_tokens, 600)

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

    # מונע “אוטו-ריפליי לולאה” אצל חלק מהשרתים/קליינטים
    msg["Auto-Submitted"] = "auto-replied"
    msg["X-Auto-Response-Suppress"] = "All"

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
        "generationConfig": {
            "temperature": 0.9,
            "topP": 0.95,
            "maxOutputTokens": max_tokens,
            "candidateCount": 1
        },
    }

    last_err = ""
    for attempt in range(1, GEMINI_RETRIES + 1):
        try:
            r = requests.post(url, json=payload, timeout=60)

            if r.status_code == 200:
                data = r.json()
                try:
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                except Exception:
                    return False, f"Gemini החזיר תשובה בפורמט לא צפוי: {json.dumps(data)[:400]}"
                return True, text

            if r.status_code == 404:
                return False, f"שגיאת Gemini 404: המודל '{model_id}' לא נמצא/לא זמין."

            if r.status_code in (401, 403):
                return False, f"שגיאת Gemini {r.status_code}: בעיית הרשאה/מפתח API."

            if r.status_code in (429, 500, 502, 503, 504):
                sleep_s = min(16, 2 ** attempt)
                last_err = f"שגיאת Gemini {r.status_code}: עומס/מגבלה זמנית. ניסיון נוסף בעוד {sleep_s} שניות."
                log(last_err)
                time.sleep(sleep_s)
                continue

            return False, f"שגיאת Gemini {r.status_code}: {r.text[:400]}"

        except Exception as e:
            sleep_s = min(16, 2 ** attempt)
            last_err = f"חריגה בתקשורת עם Gemini: {e}. ניסיון נוסף בעוד {sleep_s} שניות."
            log(last_err)
            time.sleep(sleep_s)

    return False, last_err or "Gemini נכשל ללא פירוט."

# -------- IMAP: fetch only relevant + strong dedupe --------
def get_recent_candidate_emails(
    mail: imaplib.IMAP4_SSL,
    lookback_minutes: int,
    max_count: int,
    answered_ids: Set[str]
) -> List[Dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    since_day = cutoff.strftime("%d-%b-%Y")

    # Gmail לא תמיד עקבי; עדיין נשתמש בזה + state מקומי
    result, data = mail.search(None, f'(SINCE {since_day} UNANSWERED)')
    if result != "OK":
        raise RuntimeError(f"IMAP search failed: {result} {data}")

    ids = data[0].split()
    ids = ids[-max_count:]

    out: List[Dict[str, Any]] = []
    me = (EMAIL_ACCOUNT or "").lower().strip()

    for num in reversed(ids):
        result, msg_data = mail.fetch(num, "(RFC822)")
        if result != "OK":
            continue

        msg = email.message_from_bytes(msg_data[0][1])

        # תאריך אמיתי (UTC)
        try:
            dt = email.utils.parsedate_to_datetime(msg.get("Date"))
            if dt and dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_utc = (dt or datetime.now(timezone.utc)).astimezone(timezone.utc)
        except Exception:
            dt_utc = datetime.now(timezone.utc)

        if dt_utc < cutoff:
            continue

        sender = parseaddr(msg.get("From", ""))[1].lower().strip()
        if me and sender == me:
            # לא עונים לעצמנו
            continue

        subject = decode_mime_header(msg.get("Subject")) or "(ללא נושא)"
        message_id = (msg.get("Message-ID") or "").strip()
        in_reply_to = (msg.get("In-Reply-To") or "").strip()

        # חייב Message-ID כדי לנעול כפילויות כמו שצריך
        if not message_id:
            continue

        # ✅ דה-דופ חזק בין ריצות
        if message_id in answered_ids:
            continue
        if in_reply_to and in_reply_to in answered_ids:
            continue

        body = extract_body_from_msg(msg)

        out.append({
            "imap_num": num,
            "from": sender,
            "subject": subject,
            "body": body,
            "message_id": message_id
        })

    return out

def mark_answered(mail: imaplib.IMAP4_SSL, imap_num: bytes) -> None:
    # מסמן כ-Answered וגם Seen כדי שלא יחזור שוב
    mail.store(imap_num, "+FLAGS", "\\Answered")
    mail.store(imap_num, "+FLAGS", "\\Seen")

# -------- main --------
def main():
    if not EMAIL_ACCOUNT or not EMAIL_PASSWORD or not GEMINI_API_KEY:
        raise RuntimeError("Missing EMAIL_ACCOUNT / EMAIL_PASSWORD / GEMINI_API_KEY")

    log("Run start")

    trim_state_file(STATE_MAX_IDS)
    answered_ids = load_answered_ids()
    log(f"Loaded answered ids: {len(answered_ids)}")

    model_id = pick_model(PREFERRED_MODEL)
    log(f"Using model: {model_id}")

    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    mail.select("INBOX")

    emails = get_recent_candidate_emails(mail, LOOKBACK_MINUTES, MAX_EMAILS_PER_RUN, answered_ids)
    log(f"Candidate emails: {len(emails)}")

    for m in emails:
        user_text = (m["body"] or "").strip()

        if not user_text:
            reply = "קיבלתי הודעה ריקה. תכתוב בקשה קצרה וברורה."
            send_email(m["from"], f"Re: {m['subject']}", reply, m["message_id"])
            save_answered_id(m["message_id"])
            mark_answered(mail, m["imap_num"])
            continue

        prompt, max_tokens = build_prompt(user_text)
        ok, out = call_gemini(prompt, model_id, max_tokens)

        if not ok:
            reply = (
                "כרגע יש תקלה זמנית במנוע התשובות של גוגל.\n\n"
                f"פירוט: {out}\n\n"
                "נסה שוב בעוד דקה."
            )
        else:
            reply = out.strip()

        send_email(m["from"], f"Re: {m['subject']}", reply, m["message_id"])

        # ✅ הכי חשוב: לנעול כדי לא לענות שוב
        save_answered_id(m["message_id"])
        mark_answered(mail, m["imap_num"])
        log(f"Replied+locked: {m['from']} | {m['message_id']}")

    mail.logout()
    log("Run end")

if __name__ == "__main__":
    main()
