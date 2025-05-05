import asyncio
import os
from utils.auth import run_headless_oauth
from email_handler.gmail_client import GmailClient
from database.db_handler import init_db, get_session, get_lead_by_email, add_lead, add_conversation
from ai_handler.openai_client import generate_reply
from ai_handler.prompt_handler import load_prompt_template, build_prompt, load_follow_up_prompt_template, build_follow_up_prompt
from utils.thread_manager import extract_thread_id
from datetime import datetime, timedelta
from database.models import Conversation, Lead
import time
from sqlalchemy import and_, or_, not_

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
    last_follow_up_check = time.time()

    # Load processed message IDs from database to avoid duplicate replies after restart
    session = get_session()
    known_message_ids = set(
        msg_id for (msg_id,) in session.query(Conversation.message_id).all()
    )

    while True:
        print("Checking for followups")
        if time.time() - last_follow_up_check >= 0:  # Change to 1hr = 3600
            await check_and_send_followups(session, gmail_client)
            last_follow_up_check = time.time()
        
        print("Checking for new emails...")
        # Monitor inbox for unread emails
        messages = gmail_client.list_messages(query="is:unread")
        print("Number of unread emails: ", len(messages))
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

            ## Marking emails as stop
            #if any(phrase in body.lower() for phrase in ['not interested', 'unsubscribe', 'stop emails']):
            #    lead.follow_up_status = 'not_interested' ## BAD WAY
            #    session.commit()
            #    print(f"Marked {from_email} as not interested")
            #    continue

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
                conv = add_conversation(
                    session=session,
                    lead=lead,
                    thread_id=thread_id,
                    message_id=reply_message_id,
                    parent_message_id=message_id,
                    sender=to_email,  # our email address (recipient of original)
                    recipient=from_email,
                    subject=clean_subject,
                    body=reply_text_html,
                    timestamp=datetime.utcnow(),
                    follow_up_status = 'pending',
                    last_message_owner = 'agent',
                    last_message_time = datetime.utcnow()
                )
                # Mark the email as read after processing
                gmail_client.mark_as_read(msg_id)
            else:
                print(f"Failed to send reply to {from_email} for message {message_id}")

        # Monitor sent box for unread sent emails to extract CC leads
        print("Checking for new sent emails...")
        # Changed query to "in:sent" to capture all sent emails, as sent emails are usually read
        sent_messages = gmail_client.list_messages(query="in:sent")
        print(f"Found {len(sent_messages)} sent messages")
        for sent_msg in sent_messages:
            sent_msg_id = sent_msg['id']
            ##print(f"Processing sent message ID: {sent_msg_id}")
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
                value = header.get('value', '')
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

        print("Sleeping for 30 seconds before next check...")
        time.sleep(30)





def get_leads_needing_followup(session):
    # For testing: 5 minutes (change to hours=24 for production)
    cutoff_time = datetime.utcnow() - timedelta(minutes=2)

    # First get all active leads (status='initial' or 'progress')
    active_leads = session.query(Lead).filter(
        or_(
            Lead.status == 'Initial',
            Lead.status == 'Progress'
        )
    ).all()
    
    leads_to_followup = []

    print("Active leads", len(active_leads))
    
    for lead in active_leads:
        # Get all conversations for this lead, ordered by newest first
        conversations = session.query(Conversation).filter_by(
            lead_id=lead.id
        ).order_by(Conversation.timestamp.desc()).all()
        
        if not conversations:
            print("no conversation for this lead", lead.email, lead.id)
            continue  # No conversations yet
            
        latest_conversation = conversations[0]
        
        # Check conditions:
        # 1. Last message was from us (agent)
        # 2. It's been more than 5 minutes (24hrs in production)
        # 3. No follow-up already sent
        if (latest_conversation.last_message_owner == 'agent' and
            latest_conversation.timestamp < cutoff_time):
            
            # Get the full thread for this conversation
            thread_messages = session.query(Conversation).filter_by(
                thread_id=latest_conversation.thread_id
            ).order_by(Conversation.timestamp.desc()).all()
            
            # Double-check the last message in thread is indeed from us
            if thread_messages and thread_messages[0].sender != lead.email:
                leads_to_followup.append({
                    'lead': lead,
                    'last_conversation': latest_conversation,
                    'thread_messages': thread_messages
                })
        else:
            print("filter not met")

    print("NO OF LEADS TO FOLLOWUP - ", len(leads_to_followup))
    return leads_to_followup

async def check_and_send_followups(session, gmail_client):
    print("Checking for follow-up candidates...")

    for item in get_leads_needing_followup(session):
        lead = item['lead']
        last_conv = item['last_conversation']
        thread_msgs = item['thread_messages']
        conversation_history = []
        for conv in thread_msgs:
            print("CONVERSATION:::::::",conv)
            conversation_history.append({"sender": conv.sender, "body": conv.body})
    
        base_prompt = load_follow_up_prompt_template()
        follow_up_prompt = build_follow_up_prompt(conversation_history, {lead.email}, base_prompt)
        follow_up_text = generate_reply(follow_up_prompt)


        
        lines = follow_up_text.splitlines()
        filtered_lines = [line for line in lines if not line.strip().lower().startswith('subject:')]
        follow_up_text = '\n'.join(filtered_lines).strip()

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

        follow_up_text_html = markdown_to_html(follow_up_text)
        # Log the final reply text length and preview
        print(f"follow_up_text_html length: {len(follow_up_text_html)}")
        print(f"follow_up_text_html preview: {follow_up_text_html[:200]}")


        # Send follow-up in the same thread
        follow_up_msg = gmail_client.create_message(
            to=lead.email,
            subject=last_conv.subject,
            message_text=follow_up_text_html,
            thread_id=last_conv.thread_id,
            in_reply_to=last_conv.message_id,
            references=last_conv.message_id
        )

        sent = gmail_client.send_message(follow_up_msg)
        if sent:
            print(f"Replied to {lead.email} for message {last_conv.message_id}")


        import uuid
        follow_up_conv = Conversation(
            lead_id=lead.id,
            thread_id=last_conv.thread_id,
            message_id=str(uuid.uuid4()),
            parent_message_id=last_conv.message_id,
            sender=last_conv.sender,  # Our email
            recipient=lead.email,
            subject=last_conv.subject,
            body=follow_up_text,
            timestamp=datetime.utcnow(),
            follow_up_status='sent',
            last_message_owner='agent',
            last_message_time=datetime.utcnow()
        )
        session.add(follow_up_conv)
        session.commit()



    # cutoff_time = datetime.utcnow() - timedelta(minutes=2)  # Change to hours=24 for production
    
    # # Get all leads with pending follow-ups
    # leads_needing_followup = session.query(Lead).filter(
    #     Lead.email.notlike('%executive@buildyoursocials.com%')  # Skip our own emails
    # ).all()

    # print(f"Checking {len(leads_needing_followup)} leads for follow-up eligibility...")
    
    # for lead in leads_needing_followup:
    #     print("LEAD ID: ",lead.id)
    #     # Get all conversations for this lead, ordered newest first
    #     lead_conversations = session.query(Conversation).filter_by(
    #         lead_id=lead.id,
    #         lead.status.no
    #     ).order_by(Conversation.timestamp.desc()).all()

    #     if not lead_conversations:
    #         print("no lead conversations")
    #         continue
        
    #     # Get the most recent conversation
    #     latest_conv = lead_conversations[0]
        
    #     # Skip if last message was from lead (they responded)
    #     if latest_conv.sender == lead.email:
    #         print("Skip if last message was from lead")
    #         continue
            
    #     # Skip if we've already sent a follow-up recently
    #     if latest_conv.follow_up_status == 'sent':
    #         print("Skip if we've already sent a follow-up recently")
    #         continue
            
    #     # Skip if lead has received a welcome message
    #     if any('welcome to build your socials' in conv.body.lower() 
    #            for conv in lead_conversations if conv.sender != lead.email):
    #         print(f"Skipping {lead.email} - received welcome message")
    #         continue
            
    #     # Skip if lead has expressed disinterest
    #     if any(phrase in conv.body.lower() 
    #            for conv in lead_conversations 
    #            for phrase in ['not interested', 'unsubscribe', 'stop emails', 'spam']):
    #         print(f"Skipping {lead.email} - expressed disinterest")
    #         lead.follow_up_status = 'not_interested'
    #         session.commit()
    #         continue

    #     # Check if last agent message is older than cutoff
    #     last_agent_message = next(
    #         (conv for conv in lead_conversations if conv.sender != lead.email),
    #         None
    #     )
        
    #     if not last_agent_message or last_agent_message.timestamp > cutoff_time:
    #         continue

    #     # Get the full thread for context
    #     thread_conversations = session.query(Conversation).filter_by(
    #         thread_id=latest_conv.thread_id
    #     ).order_by(Conversation.timestamp.asc()).all()

    #     conversation_history = []
    #     for conv in thread_conversations:
    #         conversation_history.append({"sender": conv.sender, "body": conv.body})

    #     # Build and send follow-up
    #     base_prompt = load_follow_up_prompt_template()
    #     follow_up_prompt = build_follow_up_prompt(conversation_history, lead.email, base_prompt)
    #     follow_up_text = generate_reply(follow_up_prompt)

    #     # Get the last message in thread for proper threading headers
    #     last_message_in_thread = thread_conversations[-1] if thread_conversations else latest_conv




    #     # Remove subject line from reply body if present (more robust)
    #     if latest_conv.subject:
    #         lines = follow_up_text.splitlines()
    #         filtered_lines = [line for line in lines if not line.strip().lower().startswith('subject:')]
    #         follow_up_text = '\n'.join(filtered_lines).strip()

    #     # Convert markdown-like reply text to HTML for proper email formatting
    #     import re

    #     def markdown_to_html(text):
    #         # Escape HTML special characters
    #         import html as html_lib
    #         text = html_lib.escape(text)

    #         # Convert **bold** to <strong>bold</strong>
    #         text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)

    #         # Convert *italic* to <em>italic</em>
    #         text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)

    #         # Convert line breaks to <br>
    #         text = text.replace('\n', '<br>')

    #         return text

    #     follow_up_text_html = markdown_to_html(follow_up_text)

    #     # Log the final reply text length and preview
    #     print(f"follow_up_text_html length: {len(follow_up_text_html)}")
    #     print(f"follow_up_text_html preview: {follow_up_text_html[:200]}")

    #     # Create reply email using original subject without "Re:" prefix
    #     clean_subject = latest_conv.subject
    #     if clean_subject and clean_subject.lower().startswith("re:"):
    #         clean_subject = clean_subject[3:].strip()




    #     # Send follow-up in the same thread
    #     follow_up_msg = gmail_client.create_message(
    #         to=lead.email,
    #         subject=clean_subject,
    #         message_text=follow_up_text_html,
    #         thread_id=latest_conv.thread_id,
    #         in_reply_to=last_message_in_thread.message_id,
    #         references=last_message_in_thread.message_id
    #     )

    #     if gmail_client.send_message(follow_up_msg):
    #         print(f"Sent follow-up to {lead.email}")
            
    #         # Record the follow-up in database
    #         import uuid
    #         follow_up_conv = Conversation(
    #             lead_id=lead.id,
    #             thread_id=latest_conv.thread_id,
    #             message_id=str(uuid.uuid4()),
    #             parent_message_id=last_message_in_thread.message_id,
    #             sender=latest_conv.sender,  # Our email
    #             recipient=lead.email,
    #             subject=latest_conv.subject,
    #             body=follow_up_text,
    #             timestamp=datetime.utcnow(),
    #             follow_up_status='sent',
    #             last_message_owner='agent',
    #             last_message_time=datetime.utcnow()
    #         )
    #         session.add(follow_up_conv)
    #         session.commit()

if __name__ == "__main__":
    # Set OPENAI_API_KEY environment variable from config
    os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")
    asyncio.run(main())
