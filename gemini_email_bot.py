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

DEFAULT_MAX_OUTPUT_TOKENS = int(os.getenv("DEFAULT_MAX_OUTPUT_TOKENS", "1400"))
GEMINI_RETRIES = int(os.getenv("GEMINI_RETRIES", "4"))

# Persistent state to prevent duplicates across runs
STATE_FILE = os.getenv("STATE_FILE", "answered_ids.txt")
STATE_MAX_IDS = int(os.getenv("STATE_MAX_IDS", "5000"))

# Continuation settings
MAX_CONTINUATIONS = int(os.getenv("MAX_CONTINUATIONS", "3"))  # up to 3 continue calls
MIN_NORMAL_CHARS = int(os.getenv("MIN_NORMAL_CHARS", "220"))  # below this likely truncated

# Prompt context limits
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "6000"))

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

def extract_body_from_msg(msg: email.message.Message) -> str:
    """
    IMPORTANT: Do NOT aggressively strip quoted text.
    We want thread context when user replies with "finish it" etc.
    We still do light cleanup of HTML -> text when needed.
    """
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
        # very light html->text
        html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
        html = re.sub(r"</p\s*>", "\n", html, flags=re.IGNORECASE)
        html = re.sub(r"<[^>]+>", "", html)
        body = html

    # normalize whitespace only (no quote stripping!)
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    return body

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

# -------- word counting helpers --------
def count_hebrew_words(text: str) -> int:
    # simple whitespace tokenization; good enough for "בדיוק 100 מילים"
    tokens = re.findall(r"\S+", (text or "").strip())
    return len(tokens)

# -------- prompt rules --------
def extract_length_instruction(text: str) -> Tuple[Optional[str], Optional[int], bool]:
    """
    returns: (instruction, exact_word_count_number, user_requested_short)
    """
    m = re.search(r"(\d+)\s*מילים", text)
    if m:
        n = int(m.group(1))
        return f"כתוב בדיוק {n} מילים.", n, False

    if re.search(r"\bקצר\b", text):
        return "כתוב תשובה קצרה אבל שלמה (לפחות 2–3 משפטים מלאים).", None, True

    if re.search(r"\bארוך\b", text):
        return "כתוב תשובה מפורטת יותר מהרגיל (אבל בלי חפירות).", None, False

    return None, None, False

def clip_text(t: str, max_chars: int) -> str:
    t = (t or "").strip()
    if len(t) <= max_chars:
        return t
    return t[-max_chars:]  # keep the most recent tail (usually contains the latest replies)

def build_prompt(subject: str, user_text: str, thread_context: str) -> Tuple[str, int, Optional[int]]:
    """
    returns: (prompt, max_tokens, exact_words_number)
    """
    rule, exact_n, requested_short = extract_length_instruction(user_text)

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
        if exact_n is not None:
            max_tokens = max(max_tokens, 1800)
        elif requested_short:
            max_tokens = min(max_tokens, 650)

    # Thread-aware: include subject + thread context + current message
    subject = (subject or "").strip()
    if not subject:
        subject = "(ללא נושא)"

    ctx = clip_text(thread_context, MAX_CONTEXT_CHARS)
    ut = clip_text(user_text, MAX_CONTEXT_CHARS)

    prompt += (
        "\n---\n"
        f"נושא המייל: {subject}\n\n"
        "הקשר מהשרשור (הודעות קודמות, אם קיימות):\n"
        f"{ctx if ctx else '(אין)'}\n\n"
        "ההודעה האחרונה של המשתמש:\n"
        f"{ut}\n"
        "---\n"
        "ענה עכשיו:"
    )
    return prompt, max_tokens, exact_n

# -------- SMTP send --------
def send_email(to: str, subject: str, body: str, reply_to: Optional[str] = None) -> None:
    html_content = markdown.markdown(body)
    html = f"""
    <html lang="he" dir="rtl">
      <body style="direction: rtl; text-align: right; font-family: Arial, sans-serif; white-space: normal;">
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

# -------- Gemini parsing + continuation --------
def extract_gemini_text_and_finish_reason(data: dict) -> Tuple[str, str]:
    """
    Join all 'parts' and read finishReason if present.
    """
    text = ""
    finish = ""
    try:
        cand = data.get("candidates", [{}])[0]
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

def looks_truncated(text: str, finish_reason: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if finish_reason.upper() == "MAX_TOKENS":
        return True
    if len(t) < MIN_NORMAL_CHARS:
        return True
    if t.endswith(("...", "…", ":", ",", "–", "-", "שלושה מהנדסים", "אני")):
        return True
    if not re.search(r"[\.!\?״\"]\s*$", t):
        return True
    return False

def build_continue_prompt(full_prompt_used: str, partial_answer: str) -> str:
    # Continue based on the same full prompt (includes context), not just user message
    return (
        f"{full_prompt_used}\n\n"
        "התחלת תשובתך (שכבר נשלחה חלקית/נעצרת):\n"
        f"{partial_answer.strip()}\n\n"
        "המשך בדיוק מהמקום שבו עצרת, בלי לחזור להתחלה. סיים תשובה בצורה טבעית:"
    )

def build_exact_words_fix_prompt(full_prompt_used: str, answer: str, exact_n: int) -> str:
    return (
        f"{full_prompt_used}\n\n"
        "יש חובה לציית לאורך.\n"
        f"ערוך/שכתב את התשובה כך שתהיה בדיוק {exact_n} מילים.\n"
        "אל תשנה את המשמעות.\n"
        "אל תוסיף כותרות.\n"
        "הנה התשובה לשכתוב:\n"
        f"{answer.strip()}\n\n"
        "החזר עכשיו את התשובה המתוקנת בלבד:"
    )

def call_gemini(full_prompt: str, model_id: str, max_tokens: int) -> Tuple[bool, str, str]:
    """
    returns: (ok, text, finish_reason)
    """
    url = f"{API_BASE}/models/{model_id}:generateContent?key={GEMINI_API_KEY}"

    payload = {
        "contents": [{"role": "user", "parts": [{"text": full_prompt}]}],
        "generationConfig": {
            "temperature": 0.9,
            "topP": 0.95,
            "maxOutputTokens": max_tokens,
            "candidateCount": 1,
        },
    }

    last_err = ""
    for attempt in range(1, GEMINI_RETRIES + 1):
        try:
            r = requests.post(url, json=payload, timeout=60)

            if r.status_code == 200:
                data = r.json()
                text, finish = extract_gemini_text_and_finish_reason(data)
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

def generate_with_auto_continue(full_prompt: str, model_id: str, max_tokens: int) -> Tuple[bool, str]:
    """
    1) call Gemini
    2) if looks truncated -> continue a few times
    """
    ok, text, finish = call_gemini(full_prompt, model_id, max_tokens)
    if not ok:
        return False, text

    full = (text or "").strip()
    fr = finish

    for _ in range(MAX_CONTINUATIONS):
        if not looks_truncated(full, fr):
            break
        cont_prompt = build_continue_prompt(full_prompt, full)
        ok2, text2, finish2 = call_gemini(cont_prompt, model_id, min(1200, max_tokens))
        if not ok2:
            break
        add = (text2 or "").strip()
        if not add:
            break
        full = (full.rstrip() + "\n" + add).strip()
        fr = finish2 or ""

    return True, full

def enforce_exact_words_if_needed(
    full_prompt: str,
    model_id: str,
    max_tokens: int,
    answer: str,
    exact_n: Optional[int]
) -> Tuple[bool, str]:
    """
    If exact word count requested, we verify and fix once (or twice).
    """
    if exact_n is None:
        return True, answer

    ans = (answer or "").strip()
    if not ans:
        return True, ans

    for _ in range(2):
        wc = count_hebrew_words(ans)
        if wc == exact_n:
            return True, ans
        fix_prompt = build_exact_words_fix_prompt(full_prompt, ans, exact_n)
        ok, fixed = generate_with_auto_continue(fix_prompt, model_id, min(1600, max_tokens))
        if not ok:
            return True, ans  # fall back to previous answer, better than failing
        ans = (fixed or "").strip()

    return True, ans

# -------- IMAP helpers: fetch message by Message-ID for thread context --------
def imap_search_by_message_id(mail: imaplib.IMAP4_SSL, msgid: str) -> Optional[bytes]:
    """
    Search message by Message-ID header. Returns IMAP sequence num (bytes) if found.
    """
    if not msgid:
        return None
    msgid = msgid.strip()
    # Ensure quotes safe
    q = msgid.replace('"', "")
    res, data = mail.search(None, f'(HEADER Message-ID "{q}")')
    if res != "OK":
        return None
    ids = data[0].split()
    if not ids:
        return None
    return ids[-1]

def fetch_body_by_imap_num(mail: imaplib.IMAP4_SSL, num: bytes) -> str:
    res, msg_data = mail.fetch(num, "(RFC822)")
    if res != "OK":
        return ""
    msg = email.message_from_bytes(msg_data[0][1])
    return extract_body_from_msg(msg)

def build_thread_context(mail: imaplib.IMAP4_SSL, current_msg: email.message.Message) -> str:
    """
    Build context from In-Reply-To / References chain (best-effort).
    We only fetch 1-2 previous messages to avoid heavy IMAP load.
    """
    parts: List[str] = []

    in_reply_to = (current_msg.get("In-Reply-To") or "").strip()
    refs = (current_msg.get("References") or "").strip()

    candidates: List[str] = []
    if in_reply_to:
        candidates.append(in_reply_to)

    # Sometimes References contains multiple ids; take the last one as nearest parent
    if refs:
        # split by whitespace, keep items that look like <...>
        ref_ids = [x.strip() for x in refs.split() if x.strip().startswith("<") and x.strip().endswith(">")]
        if ref_ids:
            candidates.append(ref_ids[-1])

    # de-dup keeping order
    seen = set()
    ordered = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)

    # fetch up to 2 parents
    for mid in ordered[:2]:
        imap_num = imap_search_by_message_id(mail, mid)
        if not imap_num:
            continue
        b = fetch_body_by_imap_num(mail, imap_num).strip()
        if b:
            parts.append(f"[הודעה קודמת בשרשור]\n{b}")

    return "\n\n".join(parts).strip()

# -------- IMAP fetch (strong dedupe) --------
def get_recent_candidate_emails(
    mail: imaplib.IMAP4_SSL,
    lookback_minutes: int,
    max_count: int,
    answered_ids: Set[str]
) -> List[Dict[str, Any]]:
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

        # build minimal thread context from parents
        thread_ctx = build_thread_context(mail, msg)

        out.append({
            "imap_num": num,
            "from": sender,
            "subject": subject,
            "body": body,
            "message_id": message_id,
            "thread_ctx": thread_ctx,
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
        subject = m.get("subject") or "(ללא נושא)"
        thread_ctx = (m.get("thread_ctx") or "").strip()

        if not user_text:
            reply = "קיבלתי הודעה ריקה. תכתוב בקשה קצרה וברורה."
            send_email(m["from"], f"Re: {subject}", reply, m["message_id"])
            save_answered_id(m["message_id"])
            mark_answered(mail, m["imap_num"])
            continue

        full_prompt, max_tokens, exact_n = build_prompt(subject, user_text, thread_ctx)

        ok, out = generate_with_auto_continue(full_prompt, model_id, max_tokens)
        if not ok:
            reply = (
                "כרגע יש תקלה זמנית במנוע התשובות של גוגל.\n\n"
                f"פירוט: {out}\n\n"
                "נסה שוב בעוד דקה."
            )
        else:
            ok2, final_out = enforce_exact_words_if_needed(full_prompt, model_id, max_tokens, out, exact_n)
            reply = (final_out or "").strip()

            if not reply:
                reply = "לא הצלחתי לייצר תשובה הפעם. נסה לנסח מחדש במשפט אחד."

        send_email(m["from"], f"Re: {subject}", reply, m["message_id"])

        # lock it so it never repeats
        save_answered_id(m["message_id"])
        mark_answered(mail, m["imap_num"])
        log(f"Replied+locked: {m['from']} | {m['message_id']}")

    mail.logout()
    log("Run end")

if __name__ == "__main__":
    main()
