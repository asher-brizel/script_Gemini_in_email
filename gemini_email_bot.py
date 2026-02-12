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

LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "180"))
MAX_EMAILS_PER_RUN = int(os.getenv("MAX_EMAILS_PER_RUN", "30"))

DEFAULT_MAX_OUTPUT_TOKENS = int(os.getenv("DEFAULT_MAX_OUTPUT_TOKENS", "1200"))
GEMINI_RETRIES = int(os.getenv("GEMINI_RETRIES", "4"))

# Persistent state to prevent duplicates across runs (critical on GitHub Actions)
STATE_FILE = os.getenv("STATE_FILE", "answered_ids.txt")
STATE_MAX_IDS = int(os.getenv("STATE_MAX_IDS", "5000"))

# Continuation settings
MAX_CONTINUATIONS = int(os.getenv("MAX_CONTINUATIONS", "2"))  # max 2 "continue" calls
MIN_NORMAL_CHARS = int(os.getenv("MIN_NORMAL_CHARS", "180"))  # below this likely truncated

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
    # gentle cleanup, not aggressive
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
    with open(STATE_FILE, "a", encoding="utf-8") as f:
        f.write(msg_id + "\n")

def trim_state_file(max_ids: int) -> None:
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

# -------- prompt rules --------
def extract_length_instruction(text: str) -> Tuple[Optional[str], bool, bool]:
    """
    returns: (instruction, exact_word_count, user_requested_short)
    """
    m = re.search(r"(\d+)\s*מילים", text)
    if m:
        return f"כתוב בדיוק {m.group(1)} מילים.", True, False

    # If the user says "קצר" we still want a complete short answer (not 7-12 words)
    if re.search(r"\bקצר\b", text):
        return "כתוב תשובה קצרה אבל שלמה (לפחות 2–3 משפטים מלאים).", False, True

    if re.search(r"\bארוך\b", text):
        return "כתוב תשובה מפורטת יותר מהרגיל (אבל בלי חפירות).", False, False

    return None, False, False

def build_prompt(user_text: str) -> Tuple[str, int, bool]:
    """
    returns: (prompt, max_tokens, exact_word_mode)
    """
    rule, exact, requested_short = extract_length_instruction(user_text)

    prompt = (
        "ענה בעברית מלאה וזורמת.\n"
        "אל תקטע משפטים.\n"
        "אל תכתוב תשובה טלגרפית.\n"
        "אם לא נאמר אחרת – כתוב לפחות 3–5 משפטים מלאים.\n"
        "בלי תקצירים ובלי מבני סעיפים קבועים אלא אם התבקש.\n"
        "ענה רק למה שהתבקש.\n"
        "אם המשתמש ביקש אורך מפורש (למשל '100 מילים') חובה לציית.\n"
    )

    max_tokens = DEFAULT_MAX_OUTPUT_TOKENS

    if rule:
        prompt += rule + "\n"
        if exact:
            max_tokens = max(max_tokens, 1400)
        elif requested_short:
            max_tokens = min(max_tokens, 600)

    prompt += "\nהבקשה:\n" + user_text.strip()
    return prompt, max_tokens, exact

# -------- SMTP send --------
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

    # Anti-loop headers
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

# -------- Gemini response parsing + continuation --------
def extract_gemini_text(data: dict) -> str:
    """
    Gemini may return multiple parts. We join all text parts.
    """
    try:
        parts = data["candidates"][0]["content"].get("parts", [])
        texts = []
        for p in parts:
            t = p.get("text")
            if t:
                texts.append(t)
        return "\n".join(texts).strip()
    except Exception:
        return ""

def looks_truncated(text: str) -> bool:
    """
    Heuristics for a cut answer:
    - too short
    - ends with ellipsis/colon/comma/dash
    - does not end with a closing punctuation mark
    """
    t = (text or "").strip()
    if len(t) < MIN_NORMAL_CHARS:
        return True
    if t.endswith(("...", "…", ":", ",", "–", "-", "שלושה מהנדסים")):
        return True
    if not re.search(r"[\.!\?״\"]\s*$", t):
        return True
    return False

def build_continue_prompt(original_user_text: str, partial_answer: str) -> str:
    return (
        "ענה בעברית מלאה וזורמת.\n"
        "המשך בדיוק מהמקום שבו עצרת, בלי לחזור להתחלה.\n"
        "אל תקטע משפטים. סיים תשובה בצורה טבעית.\n"
        "אל תוסיף כותרות.\n\n"
        "בקשת המשתמש:\n"
        f"{original_user_text.strip()}\n\n"
        "התשובה עד עכשיו:\n"
        f"{partial_answer.strip()}\n\n"
        "המשך עכשיו:"
    )

def call_gemini(prompt: str, model_id: str, max_tokens: int, exact_word_mode: bool) -> Tuple[bool, str]:
    url = f"{API_BASE}/models/{model_id}:generateContent?key={GEMINI_API_KEY}"

    def do_call(p: str, mt: int) -> Tuple[int, dict, str]:
        payload = {
            "contents": [{"role": "user", "parts": [{"text": p}]}],
            "generationConfig": {
                "temperature": 0.9,
                "topP": 0.95,
                "maxOutputTokens": mt,
                "candidateCount": 1
            },
        }
        r = requests.post(url, json=payload, timeout=60)
        data = {}
        text = ""
        if r.status_code == 200:
            data = r.json()
            text = extract_gemini_text(data)
        return r.status_code, data, text

    last_err = ""
    for attempt in range(1, GEMINI_RETRIES + 1):
        try:
            status, data, text = do_call(prompt, max_tokens)

            if status == 200:
                full = (text or "").strip()

                # If user asked EXACT word count -> do not continue automatically.
                if exact_word_mode:
                    return True, full

                # Otherwise, if it looks truncated, ask for continuation a couple of times.
                for _ in range(MAX_CONTINUATIONS):
                    if not full:
                        break
                    if not looks_truncated(full):
                        break
                    cont_prompt = build_continue_prompt(prompt, full)
                    status2, _, text2 = do_call(cont_prompt, min(900, max_tokens))
                    if status2 != 200 or not (text2 or "").strip():
                        break
                    full = (full.rstrip() + "\n" + text2.strip()).strip()

                return True, full

            if status == 404:
                return False, f"שגיאת Gemini 404: המודל '{model_id}' לא נמצא/לא זמין."
            if status in (401, 403):
                return False, f"שגיאת Gemini {status}: בעיית הרשאה/מפתח API."
            if status in (429, 500, 502, 503, 504):
                sleep_s = min(16, 2 ** attempt)
                last_err = f"שגיאת Gemini {status}: עומס/מגבלה זמנית. ניסיון נוסף בעוד {sleep_s} שניות."
                log(last_err)
                time.sleep(sleep_s)
                continue

            return False, f"שגיאת Gemini {status}: {json.dumps(data)[:400] if data else 'no body'}"

        except Exception as e:
            sleep_s = min(16, 2 ** attempt)
            last_err = f"חריגה בתקשורת עם Gemini: {e}. ניסיון נוסף בעוד {sleep_s} שניות."
            log(last_err)
            time.sleep(sleep_s)

    return False, last_err or "Gemini נכשל ללא פירוט."

# -------- IMAP fetch (strong dedupe) --------
def get_recent_candidate_emails(
    mail: imaplib.IMAP4_SSL,
    lookback_minutes: int,
    max_count: int,
    answered_ids: Set[str]
) -> List[Dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    since_day = cutoff.strftime("%d-%b-%Y")

    # Gmail is not perfectly consistent, so we combine this with our own STATE_FILE
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

        # filter by real date
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
            continue  # don't reply to ourselves

        subject = decode_mime_header(msg.get("Subject")) or "(ללא נושא)"
        message_id = (msg.get("Message-ID") or "").strip()
        in_reply_to = (msg.get("In-Reply-To") or "").strip()

        if not message_id:
            continue

        # strong dedupe across runs and threads
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

        prompt, max_tokens, exact_mode = build_prompt(user_text)
        ok, out = call_gemini(prompt, model_id, max_tokens, exact_mode)

        if not ok:
            reply = (
                "כרגע יש תקלה זמנית במנוע התשובות של גוגל.\n\n"
                f"פירוט: {out}\n\n"
                "נסה שוב בעוד דקה."
            )
        else:
            reply = (out or "").strip() or "לא הצלחתי לייצר תשובה הפעם. נסה לנסח מחדש במשפט אחד."

        send_email(m["from"], f"Re: {m['subject']}", reply, m["message_id"])

        # lock it so it never repeats
        save_answered_id(m["message_id"])
        mark_answered(mail, m["imap_num"])

        log(f"Replied+locked: {m['from']} | {m['message_id']}")

    mail.logout()
    log("Run end")

if __name__ == "__main__":
    main()
