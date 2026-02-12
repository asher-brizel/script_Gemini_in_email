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

# תשובת fallback למשתמש במקרה תקלה
FALLBACK_REPLY = "משהו השתבש אצלי רגע 🤖😅 נסה שוב עוד דקה."

DEFAULT_MAX_OUTPUT_TOKENS = 512
GEMINI_RETRIES = 3
RETRY_SLEEP = 2


def log(msg):
    print(f"[BOT] {msg}", flush=True)


def decode_mime_header(value):
    if not value:
        return ""
    parts = decode_header(value)
    return "".join(
        t.decode(enc or "utf-8", errors="ignore") if isinstance(t, bytes) else t
        for t, enc in parts
    ).strip()


def clean_email_body(text):
    patterns = [
        r"-----Original Message-----.*",
        r"^>.*$",
        r"^\s*On .*wrote:\s*$",
        r"--\s*$",
    ]
    for p in patterns:
        text = re.split(p, text, flags=re.IGNORECASE | re.MULTILINE)[0]
    return text.strip()


def extract_length_instruction(text):
    m = re.search(r"(\d+)\s*מילים", text)
    if m:
        return f"כתוב בדיוק {m.group(1)} מילים.", True

    if "קצר" in text:
        return "כתוב תשובה קצרה מאוד (שורה–שתיים).", False
    if "ארוך" in text:
        return "כתוב תשובה מפורטת יותר מהרגיל (אבל בלי חפירות).", False

    return None, False


def build_prompt(user_text):
    rule, exact = extract_length_instruction(user_text)

    prompt = (
        "ענה בעברית, בטון טבעי ולא רשמי.\n"
        "ענה רק למה שהתבקש.\n"
        "בלי תקצירים, בלי סעיפים קבועים, בלי הרצאות.\n"
    )

    max_tokens = DEFAULT_MAX_OUTPUT_TOKENS

    if rule:
        prompt += rule + "\n"
        if exact:
            max_tokens = 768

    prompt += "\nהבקשה:\n" + user_text.strip()
    return prompt, max_tokens


def get_unread_emails():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    mail.select("inbox")

    _, data = mail.search(None, "(UNSEEN)")
    messages = []

    for num in data[0].split():
        _, msg_data = mail.fetch(num, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])

        sender = parseaddr(msg.get("From", ""))[1]
        subject = decode_mime_header(msg.get("Subject")) or "(ללא נושא)"
        message_id = msg.get("Message-ID")

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
                body = decoded

        if not body and html:
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


def call_gemini(prompt, max_tokens):
    url = f"{API_BASE}/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.9,
            "maxOutputTokens": max_tokens
        }
    }

    for i in range(GEMINI_RETRIES):
        try:
            r = requests.post(url, json=payload, timeout=60)
            if r.status_code == 200:
                return True, r.json()["candidates"][0]["content"]["parts"][0]["text"]
            log(f"Gemini error {r.status_code}, retry {i+1}")
        except Exception as e:
            log(f"Gemini exception: {e}")
        time.sleep(RETRY_SLEEP * (i + 1))

    return False, None


def main():
    log("Run start")
    emails = get_unread_emails()

    if not emails:
        log("No new emails.")
        return

    for m in emails:
        log(f"Email from={m['from']} subject={m['subject']}")

        if not m["body"]:
            send_email(
                m["from"],
                f"Re: {m['subject']}",
                "קיבלתי הודעה ריקה 😅 תכתוב לי מה אתה צריך.",
                m["message_id"]
            )
            continue

        prompt, max_tokens = build_prompt(m["body"])
        ok, reply = call_gemini(prompt, max_tokens)

        if not ok or not reply:
            reply = FALLBACK_REPLY

        send_email(
            m["from"],
            f"Re: {m['subject']}",
            reply.strip(),
            m["message_id"]
        )

    log("Run end")


if __name__ == "__main__":
    main()
