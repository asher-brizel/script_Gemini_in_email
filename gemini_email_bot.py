import imaplib
import email
import smtplib
from email.mime.text import MIMEText
import requests
import os
import json

IMAP_SERVER = 'imap.gmail.com'  # אם המייל שלך בגוגל, אם לא יש לשנות בהתאם
SMTP_SERVER = 'smtp.gmail.com'
EMAIL_ACCOUNT = os.getenv('EMAIL_ACCOUNT')
EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD')  # סיסמת אפליקציה
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_API_URL = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent'


def get_unread_emails():
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
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == 'text/plain':
                            body = part.get_payload(decode=True).decode()
                            break
                else:
                    body = msg.get_payload(decode=True).decode()
                emails.append({'from': from_, 'subject': subject, 'body': body, 'id': mail_id})
    mail.logout()
    return emails


def send_email(to_email, subject, body_text):
    # טקסט משופר עם כדורים והדגשה ב-bold, מיושר לימין עם CSS ב-HTML
    html_body = f"""
    <html>
      <body style="direction: rtl; text-align: right; font-family: Arial, sans-serif;">
        <p>אני<br>
        אני מודל שפה גדול, שאומן על ידי גוגל. אני יכול לעזור לך במגוון רחב של משימות, כמו:</p>
        <ul>
          <li><b>יצירת טקסט:</b> אני יכול לכתוב סיפורים, שירים, תסריטים, מיילים, מכתבים, ועוד.</li>
          <li><b>תרגום שפות:</b> אני יכול לתרגם טקסט משפה אחת לשפה אחרת.</li>
          <li><b>סיכום טקסט:</b> אני יכול לסכם טקסט ארוך באופן תמציתי.</li>
          <li><b>מענה על שאלות:</b> אני יכול לענות על שאלות על נושאים שונים.</li>
          <li><b>מתן מידע:</b> אני יכול לספק מידע על נושאים שונים.</li>
          <li><b>יצירת רעיונות:</b> אני יכול לעזור לך ליצור רעיונות חדשים.</li>
        </ul>
        <p>אני עדיין לומד ומשתפר כל הזמן, אבל אני תמיד שמח לעזור!<br>
        יש משהו ספציפי שאתה רוצה שאעשה?</p>
      </body>
    </html>
    """

    msg = MIMEText(html_body, _subtype='html', _charset='utf-8')
    msg['From'] = EMAIL_ACCOUNT
    msg['To'] = to_email
    msg['Subject'] = subject

    server = smtplib.SMTP_SSL(SMTP_SERVER, 465)
    server.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    server.sendmail(EMAIL_ACCOUNT, to_email, msg.as_string())
    server.quit()


def query_gemini_api(prompt):
    headers = {
        'Content-Type': 'application/json',
        'X-goog-api-key': GEMINI_API_KEY,
    }
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ]
    }
    response = requests.post(GEMINI_API_URL, json=payload, headers=headers)
    if response.status_code == 200:
        response_json = response.json()
        # הדפסה לבדיקת מבנה התגובה (אפשר להסיר אחרי שמוודאים תקינות)
        print("Gemini API response:", json.dumps(response_json, ensure_ascii=False, indent=2))
        try:
            candidates = response_json.get('candidates', [])
            if candidates:
                content = candidates[0].get('content', {})
                # מבנה התוכן: מפתחות כמו 'parts' עם רשימת dict שכוללים טקסט
                if isinstance(content, dict):
                    parts = content.get('parts', [])
                    if parts and isinstance(parts[0], dict):
                        text = parts[0].get('text', '')
                        # ניקוי רווחים מיותרים מסביב
                        return text.strip()
                    else:
                        return str(content).strip()
                elif isinstance(content, str):
                    return content.strip()
                else:
                    return str(content).strip()
            else:
                return 'No candidates in response from Gemini'
        except Exception as e:
            return f'Error parsing Gemini response: {e}'
    else:
        return f'Error from Gemini API: {response.status_code} - {response.text}'


def main():
    emails = get_unread_emails()
    for mail in emails:
        print(f"Processing email from {mail['from']} with subject: {mail['subject']}")
        response = query_gemini_api(mail['body'])
        send_email(mail['from'], f"Re: {mail['subject']}", response)
        print("Response sent.")


if __name__ == '__main__':
    main()
