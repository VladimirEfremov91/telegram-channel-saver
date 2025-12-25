"""
Messages management module.
Handles operations related to Telegram channel messages.
"""
import os
import logging
import asyncio
import html
from datetime import datetime

from src.config import MESSAGES_BATCH_SIZE, BATCH_DELAY, SAVE_INTERVAL, MAX_RETRIES, MEDIA_DOWNLOAD_DELAY
from src.channels import get_active_channel
from src.database import save_database
from src.media import download_media_safely
from src.formatting import entities_to_dicts

logger = logging.getLogger(__name__)

async def save_channel_messages(client, db, db_path, limit=None, force_redownload=False, 
                              min_id=None, max_id=None, recent_count=None,
                              download_media=True, filter_word=None):
    """
    Save messages from active channel with support for ID ranges and rate limiting
    
    Args:
        client: Telegram client
        db: Database
        db_path: Path to database file
        limit: Maximum number of messages to download (None for all)
        force_redownload: Whether to redownload existing messages
        min_id: Minimum message ID to fetch (inclusive)
        max_id: Maximum message ID to fetch (exclusive)
        recent_count: Number of most recent messages to fetch
        download_media: Whether to download media (videos, photos, etc.) from messages
        filter_word: Optional keyword to filter messages (case-insensitive)
        
    Returns:
        bool: True if successful, False otherwise
    """
    active = get_active_channel(db)
    if not active:
        print("\nNo active channel selected!")
        return False
    
    try:
        print("\n" + "="*50)
        print(f"Channel: {active['title']}")
        print(f"Type: {active['type']}")
        print("="*50)
        
        # Initialize channel messages dict if doesn't exist
        channel_id = str(active['id'])
        if 'messages' not in db:
            db['messages'] = {}
        if channel_id not in db['messages']:
            db['messages'][channel_id] = {}
        
        # Get channel message boundaries
        print("\nAnalyzing channel messages...")
        first_message = None
        last_message = None
        
        # Get first message (oldest)
        async for msg in client.iter_messages(active['id'], limit=1, reverse=True):
            first_message = msg
            print(f"First message found: #{msg.id} ({msg.date})")
        
        # Get last message (newest) 
        async for msg in client.iter_messages(active['id'], limit=1):
            last_message = msg
            print(f"Last message found: #{msg.id} ({msg.date})")
            
        if not first_message or not last_message:
            print("\nNo messages found in channel!")
            return False
        
        # Determine message range to fetch
        fetch_min_id = min_id if min_id is not None else first_message.id
        fetch_max_id = max_id if max_id is not None else last_message.id + 1
        
        # Handle recent_count if specified
        if recent_count is not None:
            fetch_min_id = max(first_message.id, last_message.id - recent_count)
            fetch_max_id = last_message.id + 1
        
        # Ensure range is valid
        if fetch_min_id >= fetch_max_id:
            print("\nInvalid message range: min_id must be less than max_id")
            return False
        
        # Calculate total messages in range
        total_in_range = fetch_max_id - fetch_min_id
        if limit:
            total = min(total_in_range, limit)
        else:
            total = total_in_range
        
        filter_word_normalized = filter_word.strip().lower() if filter_word else None

        print(f"\nChannel Information:")
        print(f"First Message in Channel: #{first_message.id} ({first_message.date})")
        print(f"Last Message in Channel: #{last_message.id} ({last_message.date})")
        print(f"Message Range to Fetch: #{fetch_min_id} to #{fetch_max_id - 1}")
        print(f"Messages in Range: {total_in_range}")
        print(f"Messages to Process: {total}")
        print(f"Media Download: {'Enabled' if download_media else 'Disabled'}")
        print(f"Batch Size: {MESSAGES_BATCH_SIZE} messages")
        print(f"Delay between batches: {BATCH_DELAY} seconds")
        print(f"Rate Limit: Maximum 100 messages per request")
        print(f"Text Filter: {filter_word if filter_word_normalized else 'Disabled'}")
        
        confirm = input("\nProceed with message download? (y/N): ").lower()
        if confirm != 'y':
            print("\nOperation cancelled!")
            return False
        
        print("\nStarting message download...")
        print("="*50)
        
        # Counters
        saved = 0
        updated = 0
        skipped = 0
        skipped_by_filter = 0
        errors = 0
        retry_count = 0
        processed = 0
        media_downloaded = 0
        media_skipped = 0
        media_errors = 0
        
        # Progress tracking
        start_time = datetime.now()
        last_save_time = start_time
        
        # Process messages in batches from newest to oldest
        current_id = fetch_max_id - 1
        
        while current_id >= fetch_min_id and (limit is None or processed < limit):
            try:
                # Check if we need to adjust batch size to respect limits
                remaining = limit - processed if limit is not None else None
                batch_size = min(MESSAGES_BATCH_SIZE, remaining) if remaining is not None else MESSAGES_BATCH_SIZE
                
                print(f"\nFetching batch for messages <= {current_id}")
                print(f"Batch parameters:")
                print(f"- Channel ID: {active['id']}")
                print(f"- Limit: {batch_size}")
                print(f"- Min ID: {fetch_min_id}")
                print(f"- Max ID: {current_id + 1}")
                
                # Get batch of messages with Telegram's limit of 100 per request
                batch_messages = []
                async for message in client.iter_messages(
                    active['id'],
                    limit=batch_size,
                    max_id=current_id + 1,
                    min_id=fetch_min_id - 1  # -1 to include the min_id message
                ):
                    batch_messages.append(message)
                    processed += 1
                    if limit is not None and processed >= limit:
                        break
                
                print(f"Retrieved {len(batch_messages)} messages in batch")
                if batch_messages:
                    print(f"First message in batch: #{batch_messages[0].id}")
                    print(f"Last message in batch: #{batch_messages[-1].id}")
                
                if not batch_messages:
                    print("\nNo more messages in batch, breaking loop")
                    break
                
                # Update current_id for next batch
                current_id = min(msg.id for msg in batch_messages) - 1
                print(f"Next batch will start from ID: {current_id}")
                
                # Add delay to respect rate limits
                if len(batch_messages) == MESSAGES_BATCH_SIZE:
                    print(f"Waiting {BATCH_DELAY} seconds before next batch to respect rate limits...")
                    await asyncio.sleep(BATCH_DELAY)
                
                # Process batch
                for message in batch_messages:
                    try:
                        if filter_word_normalized:
                            combined_text = f"{message.text or ''}\n{message.raw_text or ''}".lower()
                            if filter_word_normalized not in combined_text:
                                skipped_by_filter += 1
                                continue

                        # Create message dict with all available fields
                        message_dict = {
                            'id': message.id,
                            'date': str(message.date),
                            'edit_date': str(message.edit_date) if message.edit_date else None,
                            'from_id': message.from_id.user_id if message.from_id else None,
                            'post_author': getattr(message, 'post_author', None),  # Get channel post author
                            'text': message.text,
                            'raw_text': message.raw_text,
                            'entities': entities_to_dicts(message.entities),  # Store native Telegram entities
                            'text_html': getattr(message, 'text_html', message.text),  # Get HTML representation
                            'out': message.out,
                            'mentioned': message.mentioned,
                            'media_unread': message.media_unread,
                            'silent': message.silent,
                            'post': message.post,
                            'from_scheduled': message.from_scheduled,
                            'legacy': message.legacy,
                            'edit_hide': message.edit_hide,
                            'pinned': message.pinned,
                            'noforwards': message.noforwards,
                            'views': getattr(message, 'views', 0),
                            'forwards': getattr(message, 'forwards', 0),
                            'has_media': bool(message.media),
                            'media_type': type(message.media).__name__ if message.media else None,
                            'media_file_path': None,  # Will store path to downloaded media
                            'grouped_id': str(message.grouped_id) if message.grouped_id else None,
                            'reactions': [],
                            'reply_to': message.reply_to.reply_to_msg_id if message.reply_to else None,
                            'last_update': str(datetime.now())
                        }
                        
                        # Add reactions if present
                        if hasattr(message, 'reactions') and message.reactions:
                            try:
                                for reaction in message.reactions.results:
                                    reaction_data = {
                                        'emoticon': reaction.reaction.emoticon if hasattr(reaction.reaction, 'emoticon') else None,
                                        'document_id': reaction.reaction.document_id if hasattr(reaction.reaction, 'document_id') else None,
                                        'count': reaction.count,
                                        # Only add chosen if it exists
                                        'chosen': getattr(reaction, 'chosen', False)
                                    }
                                    message_dict['reactions'].append(reaction_data)
                            except Exception as reaction_error:
                                logger.debug(f"Could not process reactions for message {message.id}: {str(reaction_error)}")
                                # Add basic reaction info without chosen status
                                for reaction in message.reactions.results:
                                    try:
                                        reaction_data = {
                                            'emoticon': reaction.reaction.emoticon if hasattr(reaction.reaction, 'emoticon') else None,
                                            'document_id': reaction.reaction.document_id if hasattr(reaction.reaction, 'document_id') else None,
                                            'count': reaction.count
                                        }
                                        message_dict['reactions'].append(reaction_data)
                                    except Exception as e:
                                        logger.debug(f"Skipping malformed reaction in message {message.id}: {str(e)}")
                                        continue
                        
                        msg_id = str(message.id)
                        
                        # Download media if requested and message has media
                        if download_media and message.media:
                            try:
                                # Check if we already have the media downloaded
                                existing_media_path = None
                                if msg_id in db['messages'][channel_id]:
                                    existing_media_path = db['messages'][channel_id][msg_id].get('media_file_path')
                                
                                if (not existing_media_path or 
                                    not os.path.exists(existing_media_path) or 
                                    force_redownload):
                                    
                                    # Create a filename based on message ID and date
                                    filename = f"media_{message.id}_{message.date.strftime('%Y%m%d_%H%M%S')}"
                                    
                                    # Download the media
                                    print(f"Downloading media from message #{message.id}...")
                                    
                                    # Get media file size if available
                                    file_size = None
                                    if hasattr(message.media, 'document'):
                                        file_size = getattr(message.media.document, 'size', None)
                                        if file_size:
                                            size_mb = file_size / (1024 * 1024)
                                            print(f"Media size: {size_mb:.2f} MB")
                                    
                                    # Use our enhanced download method
                                    download_result = await download_media_safely(
                                        client=client,
                                        message=message,
                                        filename=filename,
                                        file_size=file_size
                                    )
                                    
                                    if download_result['success']:
                                        file_path = download_result['file_path']
                                        print(f"Media saved to: {file_path}")
                                        # Update message dict with media path
                                        message_dict['media_file_path'] = file_path
                                        media_downloaded += 1
                                        
                                        # Also store in videos database if it's a video
                                        is_video = False
                                        mime_type = getattr(message.media.document, 'mime_type', '') if hasattr(message.media, 'document') else ''
                                        if mime_type and 'video' in mime_type:
                                            is_video = True
                                            
                                        if is_video:
                                            # Initialize videos dict if needed
                                            if 'videos' not in db:
                                                db['videos'] = {}
                                            if channel_id not in db['videos']:
                                                db['videos'][channel_id] = {}
                                                
                                            # Add video information
                                            video_info = {
                                                'id': message.id,
                                                'date': str(message.date),
                                                'from_id': message_dict['from_id'],
                                                'media_type': message_dict['media_type'],
                                                'file_path': file_path,
                                                'download_date': str(datetime.now()),
                                                'file_size': os.path.getsize(file_path) if os.path.exists(file_path) else None,
                                                'duration': getattr(message.media.document, 'duration', None) if hasattr(message.media, 'document') else None,
                                                'mime_type': mime_type,
                                                'size': getattr(message.media.document, 'size', None) if hasattr(message.media, 'document') else None,
                                            }
                                            db['videos'][channel_id][msg_id] = video_info
                                    else:
                                        # Handle download failure
                                        print(f"Failed to download media: {download_result['error']}")
                                        logger.warning(f"Media download failed for message {message.id}: {download_result['error']}")
                                        media_errors += 1
                                    
                                    # Add delay between media downloads to avoid rate limits
                                    await asyncio.sleep(MEDIA_DOWNLOAD_DELAY)
                                else:
                                    # Media already downloaded
                                    message_dict['media_file_path'] = existing_media_path
                                    print(f"Media for message #{message.id} already downloaded, skipping...")
                                    media_skipped += 1
                                    
                            except Exception as media_error:
                                print(f"Error downloading media from message #{message.id}: {str(media_error)}")
                                logger.error(f"Error downloading media: {str(media_error)}")
                                media_errors += 1
                        
                        if msg_id in db['messages'][channel_id] and not force_redownload:
                            # Check if message needs update
                            existing = db['messages'][channel_id][msg_id]
                            if (existing.get('views') != message_dict['views'] or 
                                existing.get('forwards') != message_dict['forwards'] or
                                existing.get('reactions') != message_dict['reactions'] or
                                (download_media and existing.get('media_file_path') != message_dict['media_file_path'])):
                                db['messages'][channel_id][msg_id].update(message_dict)
                                updated += 1
                            else:
                                skipped += 1
                        else:
                            # Add new message or force update
                            db['messages'][channel_id][msg_id] = message_dict
                            saved += 1
                        
                    except Exception as msg_error:
                        logger.error(f"Error processing message {message.id}: {str(msg_error)}")
                        errors += 1
                        continue
                
                # Update progress
                current_time = datetime.now()
                elapsed = current_time - start_time
                completed = saved + updated + skipped + skipped_by_filter
                speed = completed / elapsed.total_seconds() if elapsed.total_seconds() > 0 else 0
                
                # Save database periodically
                if (current_time - last_save_time).total_seconds() > SAVE_INTERVAL:
                    save_database(db_path, db)
                    last_save_time = current_time
                
                # Update display
                print("\033[F\033[K" * 8)
                print(f"Progress: {completed}/{total} messages ({current_time - start_time})")
                print(f"New: {saved} | Updated: {updated} | Skipped (unchanged): {skipped}")
                print(f"Skipped by filter: {skipped_by_filter} | Errors: {errors}")
                print(f"Speed: {speed:.1f} messages/second")
                print(f"Elapsed: {str(elapsed).split('.')[0]}")
                print(f"Current Batch: {len(batch_messages)} messages (ID: {current_id})")
                print(f"Retries: {retry_count}/{MAX_RETRIES}")
                print("-"*50)
                
                # Reset retry count on successful batch
                retry_count = 0
                
            except Exception as batch_error:
                # Enhance error logging
                print(f"\nDebug: Batch error details:")
                print(f"- Error type: {type(batch_error).__name__}")
                print(f"- Error message: {str(batch_error)}")
                print(f"- Current message ID: {current_id}")
                logger.error(f"Error processing batch: {str(batch_error)}", exc_info=True)
                
                retry_count += 1
                if retry_count >= MAX_RETRIES:
                    print(f"\nToo many errors, stopping download at message {current_id}")
                    break
                print(f"\nRetrying batch in {BATCH_DELAY * 2} seconds... ({retry_count}/{MAX_RETRIES})")
                await asyncio.sleep(BATCH_DELAY * 2)
        
        # Final save
        save_database(db_path, db)
        
        # Final statistics
        end_time = datetime.now()
        elapsed = end_time - start_time
        completed = saved + updated + skipped + skipped_by_filter
        speed = completed / elapsed.total_seconds() if elapsed.total_seconds() > 0 else 0
        
        print("\n" + "="*50)
        print("Download Completed!")
        print("="*50)
        print(f"\nFinal Statistics:")
        print(f"Total Processed: {completed}")
        print(f"New Messages: {saved}")
        print(f"Updated Messages: {updated}")
        print(f"Skipped Messages: {skipped}")
        print(f"Skipped by Filter: {skipped_by_filter}")
        print(f"Errors: {errors}")
        print(f"Total Retries: {retry_count}")
        print(f"\nMedia Statistics:")
        print(f"Media Downloaded: {media_downloaded}")
        print(f"Media Skipped: {media_skipped}")
        print(f"Media Errors: {media_errors}")
        print(f"\nTime Elapsed: {str(elapsed).split('.')[0]}")
        print(f"Average Speed: {speed:.1f} messages/second")
        print("="*50)
        
        return True
        
    except Exception as e:
        logger.error(f"Error saving channel messages: {e}")
        print(f"\nError saving messages: {str(e)}")
        return False

async def search_messages(db):
    """
    Search in saved messages
    
    Args:
        db: Database
    """
    active = get_active_channel(db)
    if not active:
        print("\nNo active channel selected!")
        return
        
    channel_id = str(active['id'])
    if channel_id not in db.get('messages', {}):
        print("\nNo saved messages for this channel!")
        return
        
    messages = db['messages'][channel_id]
    if not messages:
        print("\nNo messages found!")
        return
    
    print("\nSearch Options:")
    print("1. Search by text")
    print("2. Search by date range")
    print("3. Search by message ID")
    print("4. Show messages with reactions")
    print("5. Show messages with media")
    print("6. Show user's last messages")
    print("7. Back to main menu")
    
    choice = input("\nEnter your choice (1-7): ")
    
    if choice == '1':
        query = input("\nEnter search text: ").lower()
        results = []
        
        for msg_id, msg in messages.items():
            if msg.get('text') and query in msg['text'].lower():
                results.append(msg)
                
        _display_message_results(results, f"Messages containing '{query}'")
        
    elif choice == '2':
        from_date = input("\nEnter start date (YYYY-MM-DD): ")
        to_date = input("Enter end date (YYYY-MM-DD): ")
        
        try:
            from_dt = datetime.strptime(from_date, '%Y-%m-%d')
            to_dt = datetime.strptime(to_date, '%Y-%m-%d')
            
            results = []
            for msg_id, msg in messages.items():
                msg_date = datetime.strptime(msg['date'].split('+')[0], '%Y-%m-%d %H:%M:%S')
                if from_dt <= msg_date <= to_dt:
                    results.append(msg)
                    
            _display_message_results(results, f"Messages from {from_date} to {to_date}")
            
        except ValueError:
            print("\nInvalid date format! Use YYYY-MM-DD")
            
    elif choice == '3':
        msg_id = input("\nEnter message ID: ")
        if msg_id in messages:
            _display_message_results([messages[msg_id]], "Message found")
        else:
            print("\nMessage not found!")
            
    elif choice == '4':
        results = []
        for msg_id, msg in messages.items():
            if msg.get('reactions') and len(msg['reactions']) > 0:
                results.append(msg)
                
        _display_message_results(results, "Messages with reactions")
        
    elif choice == '5':
        results = []
        for msg_id, msg in messages.items():
            if msg.get('has_media'):
                results.append(msg)
                
        _display_message_results(results, "Messages with media")
        
    elif choice == '6':
        # Show users to choose from
        if channel_id not in db.get('users', {}):
            print("\nNo saved users for this channel! Please save users first.")
            return
            
        users = db['users'][channel_id]
        print("\nAvailable Users:")
        print("-" * 60)
        print(f"{'ID':<12} | {'Username':<15} | {'Name':<20}")
        print("-" * 60)
        
        # Show users sorted by username
        for user_id, user in sorted(users.items(), key=lambda x: x[1].get('username') or ''):
            username = f"@{user['username']}" if user['username'] else '-'
            name = f"{user['first_name'] or ''} {user['last_name'] or ''}".strip() or '-'
            print(f"{user_id:<12} | {username:<15} | {name[:20]:<20}")
        
        # Get user choice
        user_id = input("\nEnter user ID (or username with @): ")
        
        # Find user by ID or username
        target_user_id = None
        if user_id.startswith('@'):
            username = user_id[1:]
            for uid, user in users.items():
                if user.get('username') == username:
                    target_user_id = uid
                    break
        else:
            target_user_id = user_id
        
        if target_user_id not in users:
            print("\nUser not found!")
            return
        
        # Find user's messages
        user_messages = []
        target_user_id_str = str(target_user_id)  # Convert to string for comparison
        for msg_id, msg in messages.items():
            # Check both from_id and sender_id (for compatibility)
            msg_from_id = msg.get('from_id')
            if msg_from_id is not None:
                msg_from_id_str = str(msg_from_id)  # Convert to string
                if msg_from_id_str == target_user_id_str:
                    user_messages.append(msg)
        
        if not user_messages:
            user = users[target_user_id]
            username = f"@{user['username']}" if user['username'] else 'No username'
            name = f"{user['first_name'] or ''} {user['last_name'] or ''}".strip() or 'No name'
            print(f"\nNo messages found for user {name} ({username})")
            
            # Debug info
            print("\nDebug info:")
            print(f"Looking for user ID: {target_user_id_str}")
            print(f"Total messages in channel: {len(messages)}")
            print(f"Sample message from_ids: {[str(msg.get('from_id')) for msg in list(messages.values())[:5]]}")
            return
        
        # Sort by date (newest first) and take last 10
        user_messages.sort(key=lambda x: x['date'], reverse=True)
        last_messages = user_messages[:10]
        
        # Display results
        user = users[target_user_id]
        username = f"@{user['username']}" if user['username'] else 'No username'
        name = f"{user['first_name'] or ''} {user['last_name'] or ''}".strip() or 'No name'
        
        _display_message_results(
            last_messages,
            f"Last 10 messages from {name} ({username})"
        )
        
        # Show statistics
        print(f"\nUser Message Statistics:")
        print(f"Total messages: {len(user_messages)}")
        if user_messages:
            first_msg_date = min(msg['date'] for msg in user_messages)
            last_msg_date = max(msg['date'] for msg in user_messages)
            print(f"First message: {first_msg_date}")
            print(f"Last message: {last_msg_date}")
            
            # Count messages with media
            media_count = sum(1 for msg in user_messages if msg.get('has_media'))
            print(f"Messages with media: {media_count}")
            
            # Count reactions received
            total_reactions = sum(
                sum(r['count'] for r in msg.get('reactions', []))
                for msg in user_messages
            )
            print(f"Total reactions received: {total_reactions}")
    
    elif choice == '7':
        return
    else:
        print("\nInvalid choice!")

def _display_message_results(messages, title):
    """
    Helper method to display message search results
    
    Args:
        messages: List of messages to display
        title: Title to display
    """
    if not messages:
        print("\nNo messages found!")
        return
        
    print(f"\n{title}")
    print(f"Found {len(messages)} messages")
    print("-" * 80)
    
    # Sort messages by date
    messages.sort(key=lambda x: x['date'])
    
    for msg in messages:
        print(f"\nMessage #{msg['id']} ({msg['date']})")
        print(f"{'='*40}")
        
        if msg.get('text'):
            print(f"Text: {msg['text'][:200]}{'...' if len(msg['text']) > 200 else ''}")
        
        if msg.get('has_media'):
            print(f"Media: {msg['media_type']}")
        
        if msg.get('reactions'):
            reactions = []
            for reaction in msg['reactions']:
                emoji = reaction.get('emoticon') or f"Custom({reaction.get('document_id')})"
                reactions.append(f"{emoji}({reaction['count']})")
            print(f"Reactions: {' '.join(reactions)}")
        
        if msg.get('views'):
            print(f"Views: {msg['views']}")
        
        if msg.get('forwards'):
            print(f"Forwards: {msg['forwards']}")
        
        print("-" * 80)
    
    print(f"\nTotal results: {len(messages)}")

async def browse_messages(db):
    """
    Browse messages in the active channel with pagination
    
    Args:
        db: Database
    """
    active = get_active_channel(db)
    if not active:
        print("\nNo active channel selected!")
        return
        
    channel_id = str(active['id'])
    if channel_id not in db.get('messages', {}):
        print("\nNo saved messages for this channel!")
        return
        
    messages = db['messages'][channel_id]
    if not messages:
        print("\nNo messages found!")
        return
    
    # Convert messages dictionary to list and sort by ID
    message_list = [msg for msg_id, msg in messages.items()]
    message_list.sort(key=lambda x: int(x['id']))
    
    page_size = 10  # Number of messages per page
    current_page = 0
    total_pages = (len(message_list) + page_size - 1) // page_size
    
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        start_idx = current_page * page_size
        end_idx = min(start_idx + page_size, len(message_list))
        current_messages = message_list[start_idx:end_idx]
        
        print(f"\nMessage Index for {active['title']} - Page {current_page + 1}/{total_pages}")
        print(f"Total Messages: {len(message_list)}")
        print("-" * 80)
        print(f"{'ID':<10} | {'Date':<20} | {'From':<15} | {'Preview':<30}")
        print("-" * 80)
        
        for msg in current_messages:
            msg_id = msg['id']
            date = msg['date'].split(' ')[0] if ' ' in msg.get('date', '') else msg.get('date', 'N/A')
            
            # Get sender info
            from_id = msg.get('from_id', 'Unknown')
            # Check for post_author first (for channel posts)
            if msg.get('post_author'):
                from_text = msg['post_author']
            elif from_id and from_id != 'Unknown':
                from_text = f"User_{from_id}"
                if channel_id in db.get('users', {}) and str(from_id) in db.get('users', {}).get(channel_id, {}):
                    user = db['users'][channel_id][str(from_id)]
                    if user.get('username'):
                        from_text = f"@{user['username']}"
                    else:
                        from_text = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or from_text
            else:
                if active['type'] == 'Channel':
                    from_text = active['title']  # Use channel name for channel posts
                else:
                    from_text = "Unknown sender"
            
            # Get preview text (first line or part of it)
            preview = msg.get('text', '')
            if preview:
                preview = preview.split('\n')[0][:30]  # First line, max 30 chars
                if len(preview) < len(msg.get('text', '')):
                    preview += "..."
            elif msg.get('has_media'):
                preview = f"[{msg.get('media_type', 'Media')}]"
            else:
                preview = "[Empty message]"
                
            print(f"{msg_id:<10} | {date:<20} | {from_text[:15]:<15} | {preview:<30}")
        
        print("-" * 80)
        print("\nNavigation:")
        print("n - Next page")
        print("p - Previous page")
        print("g - Go to page")
        print("j - Jump to message ID")
        print("v - View message HTML source")
        print("q - Return to main menu")
        
        choice = input("\nEnter your choice: ").lower()
        
        if choice == 'n':
            if current_page < total_pages - 1:
                current_page += 1
        elif choice == 'p':
            if current_page > 0:
                current_page -= 1
        elif choice == 'g':
            try:
                page = int(input("\nEnter page number: "))
                if 1 <= page <= total_pages:
                    current_page = page - 1
                else:
                    print(f"\nPage number must be between 1 and {total_pages}")
                    input("Press Enter to continue...")
            except ValueError:
                print("\nInvalid page number!")
                input("Press Enter to continue...")
        elif choice == 'j':
            msg_id = input("\nEnter message ID: ")
            if msg_id in messages:
                # Find the page containing this message
                for i, msg in enumerate(message_list):
                    if str(msg['id']) == msg_id:
                        current_page = i // page_size
                        break
                # Highlight the message on the next iteration
            else:
                print("\nMessage ID not found!")
                input("Press Enter to continue...")
        elif choice == 'v':
            msg_id = input("\nEnter message ID to view: ")
            if msg_id in messages:
                view_message_html(messages[msg_id])
                input("\nPress Enter to return to browsing...")
            else:
                print("\nMessage ID not found!")
                input("Press Enter to continue...")
        elif choice == 'q':
            return
        
def view_message_html(message):
    """
    Display HTML source of a message
    
    Args:
        message: Message dictionary to display
    """
    os.system('cls' if os.name == 'nt' else 'clear')
    
    print(f"\nMessage #{message['id']} HTML Source")
    print("=" * 80)
    
    # Show Telegram's HTML formatting if available
    if message.get('text_html'):
        print("\nTelegram HTML Formatting:")
        print("-" * 80)
        print(message['text_html'])
        print("-" * 80)
    
    # Create structured HTML representation
    html_content = "<div class='message'>\n"
    
    # Add message header with metadata
    html_content += f"  <div class='message-header'>\n"
    html_content += f"    <div class='message-id'>Message ID: {message['id']}</div>\n"
    html_content += f"    <div class='message-date'>Date: {message['date']}</div>\n"
    
    if message.get('post_author'):
        html_content += f"    <div class='message-from'>Author: {message['post_author']}</div>\n"
    elif message.get('from_id'):
        html_content += f"    <div class='message-from'>From: {message['from_id']}</div>\n"
    
    if message.get('views'):
        html_content += f"    <div class='message-views'>Views: {message['views']}</div>\n"
        
    if message.get('forwards'):
        html_content += f"    <div class='message-forwards'>Forwards: {message['forwards']}</div>\n"
    
    html_content += "  </div>\n"
    
    # Add message content
    html_content += "  <div class='message-content'>\n"
    
    # Use Telegram HTML if available, otherwise use our escaped version
    if message.get('text_html'):
        html_content += f"    <div class='message-text'>{message['text_html']}</div>\n"
    elif message.get('text'):
        escaped_text = html.escape(message['text'])
        html_content += f"    <div class='message-text'>{escaped_text}</div>\n"
    
    # Include media info
    if message.get('has_media'):
        html_content += f"    <div class='message-media'>\n"
        html_content += f"      <div class='media-type'>{message.get('media_type', 'Unknown media')}</div>\n"
        
        if message.get('media_file_path'):
            html_content += f"      <div class='media-path'>{message.get('media_file_path')}</div>\n"
            
        html_content += "    </div>\n"
    
    # Add reactions
    if message.get('reactions') and len(message.get('reactions', [])) > 0:
        html_content += "    <div class='message-reactions'>\n"
        
        for reaction in message.get('reactions', []):
            emoji = reaction.get('emoticon') or f"Custom({reaction.get('document_id')})"
            count = reaction.get('count', 0)
            html_content += f"      <span class='reaction'>{emoji} {count}</span>\n"
        
        html_content += "    </div>\n"
    
    html_content += "  </div>\n"
    html_content += "</div>"
    
    # Display the complete structured HTML content
    print("\nComplete Structured HTML:")
    print("-" * 80)
    print(html_content)
    
    # Show a raw version too
    print("\nRaw Message Data:")
    print("-" * 80)
    for key, value in message.items():
        if key not in ['reactions', 'text_html']:  # Skip complex nested structures and HTML
            print(f"{key}: {value}")
    
    if message.get('reactions'):
        print(f"reactions: {len(message['reactions'])} reactions") 
