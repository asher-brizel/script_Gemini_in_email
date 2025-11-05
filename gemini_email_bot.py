import imaplib
import email
import smtplib
from email.mime.text import MIMEText
import requests
import os
import json

# ---------------- הגדרות ----------------

IMAP_SERVER = 'imap.gmail.com'  # אם המייל שלך בגוגל
SMTP_SERVER = 'smtp.gmail.com'
EMAIL_ACCOUNT = os.getenv('EMAIL_ACCOUNT')
EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD')  # סיסמת אפליקציה
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_API_URL = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent'


# ---------------- פונקציות עזר ----------------

def get_unread_emails():
    """שולף מיילים שלא נקראו מתיבת הדואר הנכנס"""
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        mail.select('inbox')

        typ, data = mail.search(None, 'UNSEEN')
        mail_ids = data[0].split()

        emails = []
        for mail_id in mail_ids:
            typ, msg_data = mail.fetch(mail_id, '(RFC822)')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    subject = msg['subject']
                    from_ = email.utils.parseaddr(msg['from'])[1]
                    body = ""

                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == 'text/plain':
                                body = part.get_payload(decode=True).decode(errors='ignore')
                                break
                    else:
                        body = msg.get_payload(decode=True).decode(errors='ignore')

                    emails.append({'from': from_, 'subject': subject, 'body': body, 'id': mail_id})

        mail.logout()
        return emails

    except Exception as e:
        print(f"[!] Error fetching emails: {e}")
        return []


def send_email(to_email, subject, body_text):
    """שולח מייל HTML עם תגובת Gemini בלבד"""
    try:
        html_body = f"""
        <html>
          <body style="direction: rtl; text-align: right; font-family: Arial, sans-serif;">
            {body_text.replace('\n', '<br>')}
          </body>
        </html>
        """

        msg = MIMEText(html_body, _subtype='html', _charset='utf-8')
        msg['From'] = EMAIL_ACCOUNT
        msg['To'] = to_email
        msg['Subject'] = subject

        with smtplib.SMTP_SSL(SMTP_SERVER, 465) as server:
            server.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
            server.sendmail(EMAIL_ACCOUNT, to_email, msg.as_string())

        print(f"[✔] Sent reply to {to_email}")

    except Exception as e:
        print(f"[!] Error sending email to {to_email}: {e}")


def query_gemini_api(prompt):
    """שולח את גוף המייל ל-Gemini ומחזיר את התגובה"""
    try:
        headers = {
            'Content-Type': 'application/json',
            'X-goog-api-key': GEMINI_API_KEY,
        }
        payload = {
            "contents": [
                {"parts": [{"text": prompt}]}
            ]
        }

        response = requests.post(GEMINI_API_URL, json=payload, headers=headers, timeout=30)

        if response.status_code == 200:
            response_json = response.json()
            candidates = response_json.get('candidates', [])
            if candidates:
                content = candidates[0].get('content', {})
                if isinstance(content, dict):
                    parts = content.get('parts', [])
                    if parts and isinstance(parts[0], dict):
                        return parts[0].get('text', '').strip()
                return str(content).strip()
            return 'No candidates found in Gemini response.'
        else:
            return f'Error from Gemini API: {response.status_code} - {response.text}'

    except Exception as e:
        return f'Error querying Gemini API: {e}'


# ---------------- תהליך ראשי ----------------

def main():
    print("🔍 Checking for new emails...")
    emails = get_unread_emails()

    if not emails:
        print("📭 No new emails. Exiting quickly.")
        return  # יציאה מהירה כדי לא לבזבז זמן ריצה

    print(f"📬 Found {len(emails)} new email(s). Processing...\n")

    for mail in emails:
        try:
            print(f"➡️  From: {mail['from']} | Subject: {mail['subject']}")
            response = query_gemini_api(mail['body'])
            print("💬 Gemini response received.")
            send_email(mail['from'], f"Re: {mail['subject']}", response)
            print("✅ Response sent.\n")

        except Exception as e:
            print(f"[!] Error processing email from {mail['from']}: {e}\n")


if __name__ == '__main__':
    main()
