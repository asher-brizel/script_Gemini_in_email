import os
import time
import imaplib
import email
import smtplib
import requests
import markdown
from email.mime.text import MIMEText
import re
from email.header import decode_header
from email.utils import parseaddr

IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"
API_BASE = "https://generativelanguage.googleapis.com/v1beta"

EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")  # לקבלת שגיאות בלבד

# תגובה כש-Gemini נופל (כדי שלא יהיה “לא עונה”)
FALLBACK_REPLY = "משהו תקוע אצלי רגע 🤖💥 נסה שוב עוד דקה."

# הגבלת אורך תשובות (כי ביקשת שלא יהיה משעמם/ארוך מדי)
DEFAULT_MAX_OUTPUT_TOKENS = 512

# retries ל-Gemini (בעיקר 429/5xx)
GEMINI_RETRIES = 3
GEMINI_RETRY_SLEEP_SECONDS = 2


def log(msg: str) -> None:
    print(f"[BOT] {msg}", flush=True)


def decode_mime_header(value):
    if not value:
        return ""
    parts = decode_header(value)
    return "".join(
        t.decode(enc or "utf-8", errors="ignore") if isinstance(t, bytes) else t
        for t, enc in parts
    ).strip()


def clean_email_body(text: str) -> str:
    # ניקוי בסיסי כדי לא לכלול ציטוטים/חתימות (לא אגרסיבי מדי)
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


def build_prompt(user_text: str) -> tuple[str, int]:
    length_rule, exact_words = extract_length_instruction(user_text)

    # ברירת מחדל: טבעי, לא רשמי, לא חופר
    base = (
        "ענה בעברית, בטון טבעי ולא רשמי.\n"
        "ענה רק למה שהתבקש. בלי 'תקציר', בלי סעיפים קבועים, בלי הרצאות.\n"
        "אם הבקשה היא בדיחה — תן בדיחה.\n"
    )

    max_tokens = DEFAULT_MAX_OUTPUT_TOKENS

    if length_rule:
        base += length_rule + "\n"
        # אם ביקש בדיוק X מילים, נעלה טוקנים קצת כדי לא להיתקע (אבל עדיין מוגבל)
        max_tokens = 768 if exact_words else DEFAULT_MAX_OUTPUT_TOKENS

    prompt = base + "\nהבקשה:\n" + user_text.strip()
    return prompt, max_tokens


def get_unread_emails():
    if not EMAIL_ACCOUNT or not EMAIL_PASSWORD:
        raise RuntimeError("Missing EMAIL_ACCOUNT/EMAIL_PASSWORD env vars")

    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    mail.select("inbox")

    result, data = mail.search(None, "(UNSEEN)")
    if result != "OK":
        mail.logout()
        raise RuntimeError(f"IMAP search failed: {result} {data}")

    messages = []
    nums = data[0].split()
    log(f"UNSEEN count: {len(nums)}")

    for num in nums:
        result, msg_data = mail.fetch(num, "(RFC822)")
        if result != "OK":
            continue

        msg = email.message_from_bytes(msg_data[0][1])

        sender = parseaddr(msg.get("From", ""))[1]
        subject = decode_mime_header(msg.get("Subject")) or "(ללא נושא)"
        message_id = msg.get("Message-ID")

        # גוף: עדיפות text/plain, fallback ל-HTML stripped
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
            # strip html minimally
            html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
            html = re.sub(r"</p\s*>", "\n", html, flags=re.IGNORECASE)
            html = re.sub(r"<[^>]+>", "", html)
            body = html

        body = clean_email_body(body)

        messages.append({
            "from": sender,
            "subject": subject,
            "body": body,
            "message_id": message_id
        })

    mail.logout()
    return messages


def send_email(to, subject, body, reply_to=None):
    if not EMAIL_ACCOUNT or not EMAIL_PASSWORD:
        raise RuntimeError("Missing EMAIL_ACCOUNT/EMAIL_PASSWORD env vars")

    html = markdown.markdown(body)
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


def call_gemini(prompt: str, max_tokens: int) -> tuple[bool, str]:
    if not GEMINI_API_KEY:
        return False, "Missing GEMINI_API_KEY"

    url = f"{API_BASE}/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.9,
            "topP": 0.95,
            "maxOutputTokens": max_tokens
        }
    }

    last_err = ""
    for attempt in range(1, GEMINI_RETRIES + 1):
        try:
            r = requests.post(url, json=payload, timeout=60)
            if r.status_code == 200:
                data = r.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return True, text

            last_err = f"HTTP {r.status_code}: {r.text[:4000]}"
            # retry רק על בעיות זמניות
            if r.status_code in (429, 500, 502, 503, 504):
                log(f"Gemini retry {attempt}/{GEMINI_RETRIES} due to {r.status_code}")
                time.sleep(GEMINI_RETRY_SLEEP_SECONDS * attempt)
                continue

            return False, last_err

        except Exception as e:
            last_err = f"Exception: {e}"
            log(f"Gemini exception retry {attempt}/{GEMINI_RETRIES}: {e}")
            time.sleep(GEMINI_RETRY_SLEEP_SECONDS * attempt)

    return False, last_err or "Unknown Gemini error"


def notify_admin(subject: str, body: str):
    if not ADMIN_EMAIL:
        return
    try:
        send_email(ADMIN_EMAIL, subject, body, None)
    except Exception as e:
        log(f"Admin notify failed: {e}")


def main():
    log("Run start")
    log(f"Model={GEMINI_MODEL} | account-set={bool(EMAIL_ACCOUNT)} | api-key-set={bool(GEMINI_API_KEY)}")

    try:
        emails = get_unread_emails()
    except Exception as e:
        log(f"IMAP failed: {e}")
        notify_admin("Gemini Bot IMAP failed", str(e))
        return

    if not emails:
        log("No new emails.")
        return

    for m in emails:
        log(f"Email from={m['from']} subject={m['subject']} body_len={len(m['body'])}")

        if not m["body"]:
            # אם גוף ריק – עדיין עונים משהו ולא “שותקים”
            reply = "קיבלתי הודעה ריקה 😅 תכתוב לי משפט אחד מה אתה צריך."
            try:
                send_email(m["from"], f"Re: {m['subject']}", reply, m["message_id"])
            except Exception as e:
                log(f"SMTP failed (empty body reply): {e}")
                notify_admin("Gemini Bot SMTP failed", str(e))
            continue

        prompt, max_tokens = build_prompt(m["body"])
        ok, text_or_err = call_gemini(prompt, max_tokens)

        if not ok:
            log(f"Gemini failed: {text_or_err}")
            notify_admin(
                "Gemini Bot Gemini failed",
                f"From: {m['from']}\nSubject: {m['subject']}\n\nError:\n{text_or_err}\n\nPrompt:\n{prompt[:1500]}"
            )
            reply = FALLBACK_REPLY
        else:
            reply = text_or_err.strip()

        try:
            send_email(m["from"], f"Re: {m['subject']}", reply, m["message_id"])
            log("Sent.")
        except Exception as e:
            log(f"SMTP failed: {e}")
            notify_admin("Gemini Bot SMTP failed", str(e))

    log("Run end")


if __name__ == "__main__":
    main()
