import asyncio
import os
from utils.auth import run_headless_oauth
from email_handler.gmail_client import GmailClient
from database.db_handler import init_db, get_session, get_lead_by_email, add_lead, add_conversation
from ai_handler.openai_client import generate_reply
from ai_handler.prompt_handler import load_prompt_template, build_prompt
from utils.thread_manager import extract_thread_id
from datetime import datetime
from database.models import Conversation
import time

async def main():
    print("Starting Google Reply Sales Agent...")

    # Initialize database
    init_db()
    session = get_session()

    # Authenticate with Google
    creds = await run_headless_oauth()
    gmail_client = GmailClient(creds)

    # Load AI prompt template
    base_prompt = load_prompt_template()

    # Load processed message IDs from database to avoid duplicate replies after restart
    session = get_session()
    known_message_ids = set(
        msg_id for (msg_id,) in session.query(Conversation.message_id).all()
    )

    while True:
        print("Checking for new emails...")
        messages = gmail_client.list_messages(query="is:unread")
        for msg in messages:
            msg_id = msg['id']
            if msg_id in known_message_ids:
                continue

            full_msg = gmail_client.get_full_message(msg_id)
            if not full_msg:
                continue

            thread_id = full_msg.get('threadId')
            if not thread_id:
                thread_id = msg_id

            # Validate thread_id format (should be a non-empty string)
            if not isinstance(thread_id, str) or not thread_id.strip():
                print(f"Invalid thread_id detected: {thread_id}. Using msg_id instead.")
                thread_id = msg_id

            payload = full_msg.get('payload', {})
            headers = payload.get('headers', [])

            from_email = None
            to_email = None
            subject = None
            message_id = None
            date = None

            for header in headers:
                name = header.get('name', '').lower()
                value = header.get('value', '')
                if name == 'from':
                    from_email = value
                elif name == 'to':
                    to_email = value
                elif name == 'subject':
                    subject = value
                elif name == 'message-id':
                    message_id = value
                elif name == 'date':
                    date = value

            if not from_email:
                print(f"Warning: 'From' header not found for message {msg_id}")
                continue

            known_message_ids.add(msg_id)

            # Extract body (plain text or html) from full_msg payload parts
            body = ""
            payload = full_msg.get('payload', {})
            parts = payload.get('parts', [])
            if parts:
                for part in parts:
                    mime_type = part.get('mimeType', '')
                    if mime_type == 'text/plain':
                        body_data = part.get('body', {}).get('data')
                        if body_data:
                            import base64
                            body = base64.urlsafe_b64decode(body_data).decode()
                            break
                    elif mime_type == 'text/html':
                        body_data = part.get('body', {}).get('data')
                        if body_data:
                            import base64
                            body = base64.urlsafe_b64decode(body_data).decode()
            else:
                body_data = payload.get('body', {}).get('data')
                if body_data:
                    import base64
                    body = base64.urlsafe_b64decode(body_data).decode()

            # Get or create lead
            lead = get_lead_by_email(session, from_email)
            if not lead:
                lead = add_lead(session, from_email)

            # Save conversation
            add_conversation(session, lead, thread_id, message_id, from_email, to_email, subject, body, datetime.utcnow())

            # Build prompt with full conversation history from database
            conversations = session.query(
                Conversation
            ).filter_by(lead_id=lead.id).order_by(Conversation.timestamp.asc()).all()

            conversation_history = []
            for conv in conversations:
                conversation_history.append({"sender": conv.sender, "body": conv.body})

            lead_info = f"Lead email: {from_email}"
            prompt = build_prompt(conversation_history, lead_info, base_prompt)

            # Generate AI reply
            reply_text = generate_reply(prompt)

            # Generate AI reply with increased max_tokens
            reply_text = generate_reply(prompt, max_tokens=900)

            # Remove subject line from reply body if present (more robust)
            if subject:
                lines = reply_text.splitlines()
                filtered_lines = [line for line in lines if not line.strip().lower().startswith('subject:')]
                reply_text = '\n'.join(filtered_lines).strip()

            # Convert markdown-like reply text to HTML for proper email formatting
            import re

            def markdown_to_html(text):
                # Escape HTML special characters
                import html as html_lib
                text = html_lib.escape(text)

                # Convert **bold** to <strong>bold</strong>
                text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)

                # Convert *italic* to <em>italic</em>
                text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)

                # Convert line breaks to <br>
                text = text.replace('\n', '<br>')

                return text

            reply_text_html = markdown_to_html(reply_text)

            # Log the final reply text length and preview
            print(f"Reply text length: {len(reply_text_html)}")
            print(f"Reply text preview: {reply_text_html[:200]}")

            # Create reply email using original subject without "Re:" prefix
            reply_message = gmail_client.create_message(

            # Send reply
            sent = gmail_client.send_message(reply_message)
            if sent:
                print(f"Replied to {from_email} for thread {thread_id}")
            else:
                print(f"Failed to send reply to {from_email} for thread {thread_id}")

        print("Sleeping for 60 seconds before next check...")
        time.sleep(60)

            # Send reply
            sent = gmail_client.send_message(reply_message)
            if sent:
                print(f"Replied to {from_email} for thread {thread_id}")
