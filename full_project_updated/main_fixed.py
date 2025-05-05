import asyncio
import os
from utils.auth import run_headless_oauth
from email_handler.gmail_client import GmailClient
from database.db_handler import init_db, get_session, get_lead_by_email, add_lead, add_conversation, get_pending_follow_ups, add_follow_up_conversation, delete_follow_ups_for_lead
from ai_handler.openai_client import generate_reply
from ai_handler.prompt_handler import load_prompt_template, build_prompt, load_follow_up_prompt_template, build_follow_up_prompt
from utils.thread_manager import extract_thread_id
from datetime import datetime
from database.models import Conversation, Lead
import time

async def main():
    print("Starting Google Reply Sales Agent...")

    # Initialize database
    init_db()
    session = get_session()

    # Authenticate with Google
    creds = await run_headless_oauth()
    gmail_client = GmailClient(creds)

    # Load AI prompt templates
    base_prompt = load_prompt_template()
    follow_up_prompt = load_follow_up_prompt_template()

    # Load processed message IDs from database to avoid duplicate replies after restart
    known_message_ids = set(
        msg_id for (msg_id,) in session.query(Conversation.message_id).all()
    )

    while True:
        print("Checking for new emails...")

        # Check and send follow-ups for pending leads
        pending_follow_ups = get_pending_follow_ups(session)
        for follow_up in pending_follow_ups:
            lead = session.query(Lead).filter(Lead.id == follow_up.lead_id).first()
            if not lead:
                continue

            # Build follow-up prompt
            lead_info = f"Lead email: {lead.email}"
            prompt = build_follow_up_prompt(lead_info, follow_up_prompt)

            # Generate follow-up reply
            reply_text = generate_reply(prompt, max_tokens=900)

            # Convert markdown-like reply text to HTML for proper email formatting
            import re

            def markdown_to_html(text):
                import html as html_lib
                text = html_lib.escape(text)
                text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
                text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
                text = text.replace('\n', '<br>')
                return text

            reply_text_html = markdown_to_html(reply_text)

            # Prepare reply email details
            clean_subject = follow_up.subject
            if clean_subject and clean_subject.lower().startswith("re:"):
                clean_subject = clean_subject[3:].strip()

            reply_message = gmail_client.create_message(
                to=lead.email,
                subject=clean_subject,
                message_text=reply_text_html,
                thread_id=follow_up.thread_id,
                in_reply_to=follow_up.message_id,
                references=follow_up.message_id
            )

            # Send follow-up email
            sent = gmail_client.send_message(reply_message)
            if sent:
                print(f"Sent follow-up to {lead.email} for message {follow_up.message_id}")

                # Save follow-up conversation entry
                import uuid
                follow_up_message_id = str(uuid.uuid4())
                add_follow_up_conversation(
                    session=session,
                    lead=lead,
                    thread_id=follow_up.thread_id,
                    message_id=follow_up_message_id,
                    sender=follow_up.recipient,  # our email address (sender of previous follow-up)
                    recipient=lead.email,
                    subject=clean_subject,
                    body=reply_text_html,
                    timestamp=datetime.utcnow(),
                    parent_message_id=follow_up.message_id
                )
            else:
                print(f"Failed to send follow-up to {lead.email} for message {follow_up.message_id}")

        # Monitor inbox for unread emails
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
            cc_email = None
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
                elif name == 'cc':
                    cc_email = value
                elif name == 'subject':
                    subject = value
                elif name == 'message-id':
                    message_id = value
                elif name == 'date':
                    date = value

            # Check if executive@buildyoursocials.com is in CC, skip processing if yes
            if cc_email and 'executive@buildyoursocials.com' in cc_email.lower():
                print(f"Skipping message {msg_id} because executive@buildyoursocials.com is in CC")
                continue

            if not from_email:
                print(f"Warning: 'From' header not found for message {msg_id}")
                continue

            known_message_ids.add(msg_id)

            # Assign lead based on from_email
            lead = session.query(Lead).filter(Lead.email == from_email).first()
            #f not lead:

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
            clean_subject = subject
            if clean_subject and clean_subject.lower().startswith("re:"):
                clean_subject = clean_subject[3:].strip()

            reply_message = gmail_client.create_message(
                to=from_email,
                subject=clean_subject,
                message_text=reply_text_html,
                thread_id=thread_id,
                in_reply_to=message_id,
                references=message_id
            )

            # Check if a reply has already been sent for this specific message
            existing_reply = session.query(Conversation).filter(
                Conversation.lead_id == lead.id,
                Conversation.parent_message_id == message_id,
                Conversation.sender != from_email  # sender not the lead, i.e., our reply
            ).first()

            if existing_reply:
                print(f"Reply already sent to {from_email} for message {message_id}, skipping.")
                continue

            # Send reply
            sent = gmail_client.send_message(reply_message)
            if sent:
                print(f"Replied to {from_email} for message {message_id}")

                # Save the reply as a conversation entry
                import uuid
                reply_message_id = str(uuid.uuid4())
                add_conversation(
                    session=session,
                    lead=lead,
                    thread_id=thread_id,
                    message_id=reply_message_id,
                    parent_message_id=message_id,
                    sender=to_email,  # our email address (recipient of original)
                    recipient=from_email,
                    subject=clean_subject,
                    body=reply_text_html,
                    timestamp=datetime.utcnow()
                )
            else:
                print(f"Failed to send reply to {from_email} for message {message_id}")

        # Monitor sent box for unread sent emails to extract CC leads
        print("Checking for new sent emails...")
        # Changed query to "in:sent" to capture all sent emails, as sent emails are usually read
        sent_messages = gmail_client.list_messages(query="in:sent")
        print(f"Found {len(sent_messages)} sent messages")
        for sent_msg in sent_messages:
            sent_msg_id = sent_msg['id']
            print(f"Processing sent message ID: {sent_msg_id}")
            if sent_msg_id in known_message_ids:
                print(f"Skipping sent message {sent_msg_id} as already known")
                continue

            full_sent_msg = gmail_client.get_full_message(sent_msg_id)
            if not full_sent_msg:
                continue

            sent_thread_id = full_sent_msg.get('threadId')
            if not sent_thread_id:
                sent_thread_id = sent_msg_id

            # Validate thread_id format
            if not isinstance(sent_thread_id, str) or not sent_thread_id.strip():
                print(f"Invalid thread_id detected: {sent_thread_id}. Using sent_msg_id instead.")
                sent_thread_id = sent_msg_id

            sent_payload = full_sent_msg.get('payload', {})
            sent_headers = sent_payload.get('headers', [])

            sent_from_email = None
            sent_to_email = None
            sent_cc_email = None
            sent_subject = None
            sent_message_id = None
            sent_date = None

            for header in sent_headers:
                name = header.get('name', '').lower()
                value = sent_headers.get('value', '')
                if name == 'from':
                    sent_from_email = value
                elif name == 'to':
                    sent_to_email = value
                elif name == 'cc':
                    sent_cc_email = value
                elif name == 'subject':
                    sent_subject = value
                elif name == 'message-id':
                    sent_message_id = value
                elif name == 'date':
                    sent_date = value

            if not sent_cc_email:
                continue

            # Skip processing if executive@buildyoursocials.com is in CC
            if 'executive@buildyoursocials.com' in sent_cc_email.lower():
                print(f"Skipping sent message {sent_msg_id} because executive@buildyoursocials.com is in CC")
                continue

            # Parse CC emails (comma separated)
            cc_emails = [email.strip() for email in sent_cc_email.split(',') if email.strip()]

            for cc in cc_emails:
                # Skip if CC email is same as main recipient (To)
                if sent_to_email and cc.lower() == sent_to_email.lower():
                    continue

                # Check if lead exists, if not add lead
                lead = get_lead_by_email(session, cc)
                if not lead:
                    lead = add_lead(session, cc)
                    print(f"Added new lead from sent CC: {cc}")

        print("Sleeping for 10 seconds before next check...")
        time.sleep(10)

if __name__ == "__main__":
    # Set OPENAI_API_KEY environment variable from config
    os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")
    asyncio.run(main())
