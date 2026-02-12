import os
import time
import imaplib
import email
import smtplib
import requests
import markdown
import re
import json

from email.mime.text import MIMEText
from email.header import decode_header
from email.utils import parseaddr
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple, Set

# ----------------- CONFIG -----------------
IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"
API_BASE = "https://generativelanguage.googleapis.com/v1beta"

EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PREFERRED_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "240"))
MAX_EMAILS_PER_RUN = int(os.getenv("MAX_EMAILS_PER_RUN", "25"))

# חשוב: להגדיל משמעותית כדי לצמצם "קיטועים"
DEFAULT_MAX_OUTPUT_TOKENS = int(os.getenv("DEFAULT_MAX_OUTPUT_TOKENS", "3000"))
GEMINI_RETRIES = int(os.getenv("GEMINI_RETRIES", "4"))

# state prevents duplicate replies
STATE_FILE = os.getenv("STATE_FILE", "answered_ids.txt")
STATE_MAX_IDS = int(os.getenv("STATE_MAX_IDS", "10000"))

# thread transcript controls (Gmail threads)
MAX_THREAD_MESSAGES = int(os.getenv("MAX_THREAD_MESSAGES", "20"))
MAX_TRANSCRIPT_CHARS = int(os.getenv("MAX_TRANSCRIPT_CHARS", "20000"))
MAX_SINGLE_MSG_CHARS = int(os.getenv("MAX_SINGLE_MSG_CHARS", "3500"))

# hard enforcement controls for exact word count
MAX_EXACT_FIX_ATTEMPTS = int(os.getenv("MAX_EXACT_FIX_ATTEMPTS", "4"))

# general quality: if user didn't request exact words, force decent length
MIN_NONEXACT_WORDS = int(os.getenv("MIN_NONEXACT_WORDS", "60"))
MAX_REWRITE_ATTEMPTS = int(os.getenv("MAX_REWRITE_ATTEMPTS", "2"))

# ----------------- LOG -----------------
def log(msg: str) -> None:
    print(f"[BOT] {msg}", flush=True)

# ----------------- TEXT HELPERS -----------------
def decode_mime_header(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    return "".join(
        t.decode(enc or "utf-8", errors="ignore") if isinstance(t, bytes) else t
        for t, enc in parts
    ).strip()

def normalize_text(t: str) -> str:
    t = (t or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

def clip_tail(t: str, max_chars: int) -> str:
    t = normalize_text(t)
    if len(t) <= max_chars:
        return t
    return t[-max_chars:]

def count_words(text: str) -> int:
    return len(re.findall(r"\S+", (text or "").strip()))

def extract_exact_words_request(text: str) -> Optional[int]:
    """
    Detect "50 מילים" / "ב 50 מילים" etc.
    """
    m = re.search(r"(\d+)\s*מילים", text or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None

# ----------------- EMAIL BODY EXTRACTION -----------------
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

    return normalize_text(body)

# ----------------- STATE -----------------
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

# ----------------- SMTP SEND -----------------
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

# ----------------- GEMINI MODEL SELECTION -----------------
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

# ----------------- GEMINI CALL -----------------
def extract_gemini_text_finish(data: dict) -> Tuple[str, str]:
    text = ""
    finish = ""
    try:
        cand = (data.get("candidates") or [{}])[0]
        finish = (cand.get("finishReason") or "").strip()
        content = cand.get("content") or {}
        parts = content.get("parts", []) or []
        chunks = []
        for p in parts:
            t = p.get("text")
            if t:
                chunks.append(t)
        text = "\n".join(chunks).strip()
    except Exception:
        pass
    return text, finish

def gemini_generate(prompt: str, model_id: str, max_tokens: int) -> Tuple[bool, str, str]:
    url = f"{API_BASE}/models/{model_id}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.6,   # פחות "קופץ" ל-10 מילים
            "topP": 0.95,
            "maxOutputTokens": max_tokens,
            "candidateCount": 1
        },
    }

    last_err = ""
    for attempt in range(1, GEMINI_RETRIES + 1):
        try:
            r = requests.post(url, json=payload, timeout=75)

            if r.status_code == 200:
                data = r.json()
                text, finish = extract_gemini_text_finish(data)
                return True, text, finish

            if r.status_code == 404:
                return False, f"שגיאת Gemini 404: המודל '{model_id}' לא נמצא/לא זמין.", ""
            if r.status_code in (401, 403):
                return False, f"שגיאת Gemini {r.status_code}: בעיית הרשאה/מפתח API.", ""

            if r.status_code in (429, 500, 502, 503, 504):
                sleep_s = min(16, 2 ** attempt)
                last_err = f"שגיאת Gemini {r.status_code}: עומס/מגבלה זמנית. ניסיון נוסף בעוד {sleep_s} שניות."
                log(last_err)
                time.sleep(sleep_s)
                continue

            return False, f"שגיאת Gemini {r.status_code}: {r.text[:400]}", ""

        except Exception as e:
            sleep_s = min(16, 2 ** attempt)
            last_err = f"חריגה בתקשורת עם Gemini: {e}. ניסיון נוסף בעוד {sleep_s} שניות."
            log(last_err)
            time.sleep(sleep_s)

    return False, last_err or "Gemini נכשל ללא פירוט.", ""

# ----------------- THREAD TRANSCRIPT (GMAIL IMAP) -----------------
def fetch_msg_by_imap_num(mail: imaplib.IMAP4_SSL, imap_num: bytes) -> Optional[email.message.Message]:
    res, msg_data = mail.fetch(imap_num, "(RFC822)")
    if res != "OK" or not msg_data or not msg_data[0]:
        return None
    try:
        return email.message_from_bytes(msg_data[0][1])
    except Exception:
        return None

def parse_msg_date_utc(msg: email.message.Message) -> datetime:
    try:
        dt = email.utils.parsedate_to_datetime(msg.get("Date"))
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)

def fetch_gmail_thrid(mail: imaplib.IMAP4_SSL, imap_num: bytes) -> Optional[str]:
    """
    Gmail IMAP extension: X-GM-THRID
    """
    try:
        res, data = mail.fetch(imap_num, "(X-GM-THRID)")
        if res != "OK" or not data or not data[0]:
            return None
        raw = data[0].decode("utf-8", "ignore") if isinstance(data[0], (bytes, bytearray)) else str(data[0])
        m = re.search(r"X-GM-THRID\s+(\d+)", raw)
        if m:
            return m.group(1)
    except Exception:
        return None
    return None

def build_transcript_from_thread(mail: imaplib.IMAP4_SSL, thrid: str) -> str:
    """
    Fetch last N messages from the Gmail thread and build chronological transcript.
    """
    if not thrid:
        return ""

    res, data = mail.search(None, f'(X-GM-THRID {thrid})')
    if res != "OK":
        return ""

    ids = data[0].split()
    if not ids:
        return ""

    ids = ids[-MAX_THREAD_MESSAGES:]

    me = (EMAIL_ACCOUNT or "").lower().strip()
    items: List[Tuple[datetime, str]] = []

    for num in ids:
        msg = fetch_msg_by_imap_num(mail, num)
        if not msg:
            continue

        dt = parse_msg_date_utc(msg)
        frm = parseaddr(msg.get("From", ""))[1].lower().strip()
        who = "אני" if (me and frm == me) else (frm or "שולח")

        body = extract_body_from_msg(msg)
        body = clip_tail(body, MAX_SINGLE_MSG_CHARS)

        block = f"[{dt.strftime('%Y-%m-%d %H:%M UTC')}] {who}:\n{body}\n"
        items.append((dt, block))

    items.sort(key=lambda x: x[0])
    transcript = "\n\n".join(b for _, b in items)
    transcript = clip_tail(transcript, MAX_TRANSCRIPT_CHARS)
    return transcript

# ----------------- PROMPT BUILDING (CRITICAL) -----------------
def build_prompt(subject: str, transcript: str, user_last: str, exact_words: Optional[int]) -> str:
    """
    Key behavior:
    - Always give full thread transcript so "עוד" is meaningful.
    - If user says "עוד/another" -> interpret based on what assistant previously produced in transcript.
    """
    subject = (subject or "").strip() or "(ללא נושא)"
    transcript = normalize_text(transcript)
    user_last = normalize_text(user_last)

    p = (
        "ענה בעברית מלאה.\n"
        "אתה מקבל תמליל מלא של השרשור (כולל מה שהמשתמש כתב ומה שאתה ענית).\n"
        "חובה להבין בקשות יחסיות כמו: 'עוד', 'תמשיך', 'תסיים', 'כמו הקודם'.\n"
        "כלומר: 'עוד' = עוד מאותו סוג תוכן שניתן קודם בשרשור.\n"
        "אל תענה בשאלות כמו 'מה זה עוד?'. אם חסר מידע – תנחש בצורה סבירה מההקשר.\n"
        "אל תקטע משפטים. אל תפסיק באמצע.\n"
    )

    if exact_words is not None:
        p += f"חובה: כתוב בדיוק {exact_words} מילים. לא פחות ולא יותר.\n"
        p += "אם צריך, ערוך את התשובה פנימית עד שהספירה מדויקת.\n"
    else:
        p += f"אם לא נאמר אחרת – כתוב תשובה מלאה (לפחות {MIN_NONEXACT_WORDS} מילים).\n"

    p += (
        "\n---\n"
        f"נושא: {subject}\n\n"
        "תמליל השרשור (לפי סדר זמן):\n"
        f"{transcript}\n\n"
        "הודעת המשתמש האחרונה (עליה צריך לענות עכשיו):\n"
        f"{user_last}\n"
        "---\n"
        "התשובה שלך עכשיו:"
    )
    return p

# ----------------- HARD ENFORCEMENT -----------------
def hard_enforce_exact_words(
    model_id: str,
    base_prompt: str,
    first_answer: str,
    exact_n: int
) -> Tuple[bool, str]:
    """
    Hard mode:
    - We do NOT accept partial/short output.
    - We iterate until word-count matches exactly.
    """
    ans = normalize_text(first_answer)
    for attempt in range(1, MAX_EXACT_FIX_ATTEMPTS + 1):
        wc = count_words(ans)
        if wc == exact_n and wc > 0:
            return True, ans

        fix_prompt = (
            f"{base_prompt}\n\n"
            f"דרישה קשיחה: התשובה חייבת להיות בדיוק {exact_n} מילים.\n"
            f"כרגע היא {wc} מילים.\n"
            "שכתב/ערוך את התשובה כך שתהיה בדיוק במספר המילים.\n"
            "אל תוסיף כותרות. החזר תשובה בלבד.\n\n"
            f"התשובה לשכתוב:\n{ans}\n\n"
            "תשובה מתוקנת:"
        )

        ok, out, _ = gemini_generate(fix_prompt, model_id, min(DEFAULT_MAX_OUTPUT_TOKENS, 2600))
        if not ok or not out.strip():
            ans = ans  # keep previous
        else:
            ans = normalize_text(out)

    # if failed after attempts, better to notify than send cut output
    return False, ans

def needs_rewrite_nonexact(answer: str, finish: str) -> bool:
    """
    If user didn't request exact words:
    enforce "not short" + "not cut"
    """
    a = normalize_text(answer)
    if not a:
        return True
    if finish.upper() == "MAX_TOKENS":
        return True
    if count_words(a) < MIN_NONEXACT_WORDS:
        return True
    if not re.search(r"[\.!\?״\"]\s*$", a):
        return True
    return False

def rewrite_to_full_nonexact(model_id: str, base_prompt: str, bad_answer: str) -> str:
    """
    Rewrite (not continue) to avoid duplication.
    """
    prompt = (
        f"{base_prompt}\n\n"
        "התשובה הקודמת קצרה/נקטעה/לא טובה.\n"
        "שכתב אותה לתשובה מלאה וברורה.\n"
        f"חובה: לפחות {MIN_NONEXACT_WORDS} מילים.\n"
        "סיים בצורה סגורה.\n"
        "אל תחזור על חלקים מיותרים. תן תשובה ישירה.\n\n"
        f"תשובה קודמת (לא טובה):\n{normalize_text(bad_answer)}\n\n"
        "תשובה חדשה:"
    )
    ok, out, _ = gemini_generate(prompt, model_id, min(DEFAULT_MAX_OUTPUT_TOKENS, 2600))
    if not ok or not out.strip():
        return normalize_text(bad_answer)
    return normalize_text(out)

# ----------------- IMAP FETCH UNANSWERED + DEDUPE -----------------
def get_recent_unanswered_emails(mail: imaplib.IMAP4_SSL, lookback_minutes: int, max_count: int, answered_ids: Set[str]) -> List[Dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    since_day = cutoff.strftime("%d-%b-%Y")

    res, data = mail.search(None, f'(SINCE {since_day} UNANSWERED)')
    if res != "OK":
        raise RuntimeError(f"IMAP search failed: {res} {data}")

    ids = data[0].split()
    ids = ids[-max_count:]

    out: List[Dict[str, Any]] = []
    me = (EMAIL_ACCOUNT or "").lower().strip()

    for num in reversed(ids):
        res, msg_data = mail.fetch(num, "(RFC822)")
        if res != "OK":
            continue

        msg = email.message_from_bytes(msg_data[0][1])

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
            continue

        subject = decode_mime_header(msg.get("Subject")) or "(ללא נושא)"
        message_id = (msg.get("Message-ID") or "").strip()
        in_reply_to = (msg.get("In-Reply-To") or "").strip()

        if not message_id:
            continue

        # avoid duplicates across runs
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
            "message_id": message_id,
        })

    return out

def mark_answered(mail: imaplib.IMAP4_SSL, imap_num: bytes) -> None:
    mail.store(imap_num, "+FLAGS", "\\Answered")
    mail.store(imap_num, "+FLAGS", "\\Seen")

# ----------------- MAIN -----------------
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

    emails = get_recent_unanswered_emails(mail, LOOKBACK_MINUTES, MAX_EMAILS_PER_RUN, answered_ids)
    log(f"Unanswered emails: {len(emails)}")

    for m in emails:
        user_text = (m["body"] or "").strip()
        if not user_text:
            reply = "קיבלתי הודעה ריקה. תכתוב בקשה קצרה וברורה."
            send_email(m["from"], f"Re: {m['subject']}", reply, m["message_id"])
            save_answered_id(m["message_id"])
            mark_answered(mail, m["imap_num"])
            continue

        # Build full thread transcript
        thrid = fetch_gmail_thrid(mail, m["imap_num"])
        transcript = ""
        if thrid:
            transcript = build_transcript_from_thread(mail, thrid)
        else:
            # fallback: at least show current msg (better than nothing)
            transcript = f"[הודעה נוכחית]\n{clip_tail(user_text, MAX_SINGLE_MSG_CHARS)}\n"

        exact_n = extract_exact_words_request(user_text)
        base_prompt = build_prompt(m["subject"], transcript, user_text, exact_n)

        ok, out, finish = gemini_generate(base_prompt, model_id, DEFAULT_MAX_OUTPUT_TOKENS)
        if not ok:
            reply = (
                "כרגע יש תקלה זמנית במנוע התשובות של גוגל.\n\n"
                f"פירוט: {out}\n\n"
                "נסה שוב בעוד דקה."
            )
        else:
            answer = normalize_text(out)

            # HARD FIX: exact words must be exact, no exceptions
            if exact_n is not None:
                ok_exact, fixed = hard_enforce_exact_words(model_id, base_prompt, answer, exact_n)
                if not ok_exact:
                    # do not send cut/invalid output silently
                    reply = (
                        f"ניסיתי לכתוב בדיוק {exact_n} מילים אבל המודל לא הגיע בדיוק אחרי כמה ניסיונות.\n"
                        "נסה שוב, או בקש טווח (למשל 45–55 מילים) במקום מספר מדויק.\n\n"
                        "הטיוטה האחרונה:\n"
                        f"{fixed}"
                    )
                else:
                    reply = fixed
            else:
                # Non-exact: enforce not-short and not-cut by rewrite (NOT continuation)
                final_ans = answer
                fr = finish
                for _ in range(MAX_REWRITE_ATTEMPTS):
                    if not needs_rewrite_nonexact(final_ans, fr):
                        break
                    final_ans = rewrite_to_full_nonexact(model_id, base_prompt, final_ans)
                    fr = ""  # after rewrite, ignore previous finishReason
                reply = final_ans if final_ans else "לא הצלחתי לייצר תשובה הפעם. נסה לנסח מחדש."

        send_email(m["from"], f"Re: {m['subject']}", reply, m["message_id"])

        save_answered_id(m["message_id"])
        mark_answered(mail, m["imap_num"])
        log(f"Replied+locked: {m['from']} | {m['message_id']}")

    mail.logout()
    log("Run end")

if __name__ == "__main__":
    main()
