"""
Search and replace functionality with message-by-message approval.
Preserves all Telegram message formatting using native entities.
Supports both local-only and channel editing modes.
"""
import os
import asyncio
from datetime import datetime

from src.channels import get_active_channel
from src.database import save_database
from src.formatting import (
    get_message_entities,
    apply_replacement_to_message,
    entities_to_markdown,
    get_entities_from_markdown,
    entities_to_dicts,
    dicts_to_entities,
    search_replace_with_entities
)


async def search_replace_messages(db, db_path, client=None):
    """
    Main entry point for search and replace feature.
    Provides interactive UI for finding and replacing text in messages.

    Args:
        db: Database dictionary
        db_path: Path to database file
        client: Optional Telegram client for channel editing mode
    """
    active = get_active_channel(db)
    if not active:
        print("\nNo active channel selected!")
        print("Please select a channel first (option 3 in main menu).")
        return

    channel_id = str(active['id'])
    if channel_id not in db.get('messages', {}):
        print("\nNo saved messages for this channel!")
        print("Please download messages first (option 10 in main menu).")
        return

    messages = db['messages'][channel_id]
    if not messages:
        print("\nNo messages found!")
        return

    print("\n" + "=" * 70)
    print("                      SEARCH AND REPLACE")
    print("=" * 70)
    print(f"\nActive channel: {active['title']}")
    print(f"Total messages: {len(messages)}")

    # Ask for edit mode
    print("\n" + "-" * 70)
    print("Edit Mode:")
    print("1. Local only (modify database, not Telegram)")
    print("2. Edit in channel (modify messages on Telegram)")
    print("-" * 70)

    mode_choice = input("Select mode (1/2): ").strip()
    edit_in_channel = mode_choice == '2'

    if edit_in_channel:
        if not client:
            print("\nError: Client not available for channel editing!")
            return
        print("\nWARNING: This will edit messages directly on Telegram!")
        print("You must have admin rights with 'Edit Messages' permission.")
        confirm = input("Continue? (y/N): ").strip().lower()
        if confirm != 'y':
            print("\nCancelled.")
            return

    # Get search parameters
    print("\n" + "-" * 70)
    search = input("Enter text to search for: ").strip()
    if not search:
        print("\nSearch text cannot be empty!")
        return

    replace = input("Enter replacement text: ").strip()

    case_choice = input("Case sensitive? (y/N): ").strip().lower()
    case_sensitive = case_choice == 'y'

    print(f"\nSearching for: '{search}'")
    print(f"Replace with: '{replace}'")
    print(f"Case sensitive: {'Yes' if case_sensitive else 'No'}")
    print(f"Mode: {'Edit in channel' if edit_in_channel else 'Local only'}")

    # Find matching messages
    print("\nSearching...")
    matches = find_matching_messages(db, channel_id, search, case_sensitive)

    if not matches:
        print(f"\nNo messages found containing '{search}'")
        return

    print(f"\nFound {len(matches)} message(s) with matches.")

    if edit_in_channel:
        # Channel editing mode
        await _process_channel_edits(
            client, db, db_path, active, channel_id,
            matches, search, replace, case_sensitive
        )
    else:
        # Local-only mode
        _process_local_edits(db, db_path, matches, search, replace, case_sensitive)


def find_matching_messages(db, channel_id, search, case_sensitive=True):
    """
    Find all messages containing the search term.

    Args:
        db: Database dictionary
        channel_id: Channel ID string
        search: Text to search for
        case_sensitive: Whether search is case sensitive

    Returns:
        list: List of (message_id, message_dict, preview_dict) tuples
    """
    matches = []
    messages = db.get('messages', {}).get(channel_id, {})

    for msg_id, msg in messages.items():
        preview = apply_replacement_to_message(msg, search, '', case_sensitive)
        if preview is not None:
            # Re-run with actual replacement to get proper preview
            preview = apply_replacement_to_message(msg, search, search, case_sensitive)
            matches.append((msg_id, msg, preview))

    # Sort by message ID (numeric)
    matches.sort(key=lambda x: int(x[0]))

    return matches


def _process_local_edits(db, db_path, matches, search, replace, case_sensitive=True):
    """
    Process search/replace with local-only edits (database only).

    Args:
        db: Database dictionary
        db_path: Path to database file
        matches: List of (msg_id, msg, preview) tuples
        search: Search text
        replace: Replacement text
        case_sensitive: Whether search is case sensitive
    """
    applied = 0
    skipped = 0
    total_replacements = 0

    for i, (msg_id, msg, _) in enumerate(matches, 1):
        action, preview_data = display_and_get_action(
            msg_id, msg, i, len(matches), search, replace,
            case_sensitive=case_sensitive
        )

        if action == 'approve':
            try:
                # Store original in edit history
                if 'edit_history' not in msg:
                    msg['edit_history'] = []

                msg['edit_history'].append({
                    'date': str(datetime.now()),
                    'action': 'search_replace',
                    'search': search,
                    'replace': replace,
                    'original_raw_text': msg.get('raw_text', ''),
                    'original_text': msg.get('text', ''),
                    'original_entities': msg.get('entities', [])
                })

                # Apply new values
                msg['raw_text'] = preview_data['raw_text']
                msg['text'] = preview_data['text']
                msg['entities'] = preview_data['entities']
                msg['last_update'] = str(datetime.now())

                # Save database immediately
                save_database(db_path, db)

                applied += 1
                total_replacements += preview_data['replacement_count']
                print(f"  Message #{msg_id} updated locally.")

            except Exception as e:
                print(f"  Error updating message #{msg_id}: {e}")

        elif action == 'skip':
            skipped += 1
            print(f"  Message #{msg_id} skipped.")
        elif action == 'quit':
            print("\nStopping review.")
            break
        elif action == 'cancel':
            print("\nCancelled.")
            break

    # Summary
    _print_summary(len(matches), applied, skipped, total_replacements, channel_edited=False)


async def _process_channel_edits(client, db, db_path, active, channel_id,
                                  matches, search, replace, case_sensitive):
    """
    Process search/replace with channel editing (edit messages on Telegram).

    Args:
        client: Telegram client
        db: Database dictionary
        db_path: Path to database file
        active: Active channel dict
        channel_id: Channel ID string
        matches: List of (msg_id, msg, preview) tuples
        search: Search text
        replace: Replacement text
        case_sensitive: Whether search is case sensitive
    """
    applied = 0
    skipped = 0
    errors = 0
    restored = 0
    total_replacements = 0

    # Track last edited message for undo functionality
    last_edit = None  # Will store {'msg_id', 'msg', 'backup_entry', 'count'}

    for i, (msg_id, msg, _) in enumerate(matches, 1):
        action, preview_data = display_and_get_action(
            msg_id, msg, i, len(matches), search, replace,
            channel_edit_mode=True, case_sensitive=case_sensitive,
            last_edit=last_edit  # Pass last edit info for undo option
        )

        # Handle undo of previous edit
        if action == 'undo' and last_edit:
            undo_result = await _undo_last_edit(client, active, db, db_path, last_edit)
            if undo_result:
                applied -= 1
                total_replacements -= last_edit['count']
                restored += 1
                print(f"  Message #{last_edit['msg_id']} restored to original!")
            last_edit = None
            # Re-show current message after undo
            i -= 1  # This won't work in for loop, so we use continue and let it show again
            continue

        if action == 'approve':
            try:
                # Fetch fresh message from Telegram to get current entities
                print(f"  Fetching message #{msg_id} from Telegram...")
                telegram_msg = await client.get_messages(active['id'], ids=int(msg_id))

                if not telegram_msg:
                    print(f"  Error: Message #{msg_id} not found on Telegram!")
                    errors += 1
                    continue

                # Get fresh text and entities from Telegram
                fresh_raw_text = telegram_msg.raw_text or ''
                fresh_entities = telegram_msg.entities or []

                if not fresh_raw_text:
                    print(f"  Error: Message #{msg_id} has no text content!")
                    errors += 1
                    continue

                # Apply replacement to fresh data
                new_raw_text, new_entities_dicts, count = search_replace_with_entities(
                    fresh_raw_text, fresh_entities, search, replace, case_sensitive
                )

                # Check if URLs were changed (even if text count is 0)
                fresh_entities_dicts = entities_to_dicts(fresh_entities)
                old_urls = [e.get('url', '') for e in fresh_entities_dicts if isinstance(e, dict) and e.get('url')]
                new_urls = [e.get('url', '') for e in new_entities_dicts if isinstance(e, dict) and e.get('url')]
                url_changes = sum(1 for old, new in zip(old_urls, new_urls) if old != new)

                if count == 0 and url_changes == 0:
                    print(f"  Warning: No replacements made in message #{msg_id} (text may have changed)")
                    skipped += 1
                    continue

                total_changes = count + url_changes

                # Convert entity dicts back to Telegram entity objects for editing
                new_entity_objs = dicts_to_entities(new_entities_dicts)

                # Store FRESH backup from Telegram BEFORE editing
                # (fresh_entities_dicts already computed above for URL check)
                backup_entry = {
                    'date': str(datetime.now()),
                    'action': 'channel_search_replace',
                    'search': search,
                    'replace': replace,
                    'telegram_raw_text': fresh_raw_text,
                    'telegram_text': entities_to_markdown(fresh_raw_text, fresh_entities_dicts),
                    'telegram_entities': fresh_entities_dicts,
                    'local_raw_text': msg.get('raw_text', ''),
                    'local_text': msg.get('text', ''),
                    'local_entities': msg.get('entities', []),
                    'can_restore': True
                }

                # Edit message on Telegram
                changes_info = f"{count} text" if count > 0 else ""
                if url_changes > 0:
                    changes_info += f"{', ' if changes_info else ''}{url_changes} URL(s)"
                print(f"  Editing message #{msg_id} on Telegram ({changes_info})...")
                await client.edit_message(
                    active['id'],
                    int(msg_id),
                    new_raw_text,
                    formatting_entities=new_entity_objs
                )

                # Store backup in edit history
                if 'edit_history' not in msg:
                    msg['edit_history'] = []
                msg['edit_history'].append(backup_entry)

                # Update local database
                msg['raw_text'] = new_raw_text
                msg['text'] = entities_to_markdown(new_raw_text, new_entities_dicts)
                msg['entities'] = new_entities_dicts
                msg['last_update'] = str(datetime.now())
                msg['edited_on_telegram'] = True

                save_database(db_path, db)

                applied += 1
                total_replacements += total_changes
                print(f"  Message #{msg_id} edited on Telegram and saved locally.")

                # Store as last edit for potential undo
                last_edit = {
                    'msg_id': msg_id,
                    'msg': msg,
                    'backup_entry': backup_entry,
                    'count': total_changes
                }

                await asyncio.sleep(0.5)

            except Exception as e:
                error_msg = str(e)
                if 'MESSAGE_NOT_MODIFIED' in error_msg:
                    print(f"  Message #{msg_id} was not modified (content unchanged).")
                    skipped += 1
                elif 'MESSAGE_AUTHOR_REQUIRED' in error_msg:
                    print(f"  Error: No permission to edit message #{msg_id} (not the author).")
                    errors += 1
                elif 'CHAT_ADMIN_REQUIRED' in error_msg:
                    print(f"  Error: Admin rights required to edit message #{msg_id}.")
                    errors += 1
                else:
                    print(f"  Error editing message #{msg_id}: {e}")
                    errors += 1

        elif action == 'skip':
            skipped += 1
            print(f"  Message #{msg_id} skipped.")
        elif action == 'quit':
            print("\nStopping review.")
            break
        elif action == 'cancel':
            print("\nCancelled.")
            break

    # After all messages, offer to undo the last edit if any
    if last_edit:
        print("\n" + "-" * 70)
        print(f"Last edited message: #{last_edit['msg_id']}")
        undo_choice = input("Undo this last edit? (y/N): ").strip().lower()
        if undo_choice == 'y':
            undo_result = await _undo_last_edit(client, active, db, db_path, last_edit)
            if undo_result:
                applied -= 1
                total_replacements -= last_edit['count']
                restored += 1
                print(f"  Message #{last_edit['msg_id']} restored to original!")

    # Summary
    _print_summary(len(matches), applied, skipped, total_replacements,
                   channel_edited=True, errors=errors, restored=restored)


async def _undo_last_edit(client, active, db, db_path, last_edit):
    """
    Undo the last channel edit by restoring original message.

    Args:
        client: Telegram client
        active: Active channel dict
        db: Database dictionary
        db_path: Path to database file
        last_edit: Dict with msg_id, msg, backup_entry, count

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        msg_id = last_edit['msg_id']
        msg = last_edit['msg']
        backup = last_edit['backup_entry']

        print(f"  Restoring message #{msg_id} to original...")
        original_entity_objs = dicts_to_entities(backup['telegram_entities'])

        await client.edit_message(
            active['id'],
            int(msg_id),
            backup['telegram_raw_text'],
            formatting_entities=original_entity_objs
        )

        # Update backup entry
        backup['restored'] = True
        backup['restore_date'] = str(datetime.now())
        backup['can_restore'] = False

        # Restore local database
        msg['raw_text'] = backup['telegram_raw_text']
        msg['text'] = backup['telegram_text']
        msg['entities'] = backup['telegram_entities']
        msg['last_update'] = str(datetime.now())
        msg['restored_from_backup'] = True

        save_database(db_path, db)
        await asyncio.sleep(0.5)
        return True

    except Exception as e:
        print(f"  Error restoring message: {e}")
        print("  The backup is saved - you can restore later via menu option 15.")
        return False


def _print_summary(total_found, applied, skipped, total_replacements,
                   channel_edited=False, errors=0, restored=0):
    """Print summary of search/replace operation."""
    print("\n" + "=" * 70)
    print("                           SUMMARY")
    print("=" * 70)
    print(f"\nTotal messages found: {total_found}")
    print(f"Messages updated: {applied}")
    if restored > 0:
        print(f"Messages restored: {restored}")
    print(f"Messages skipped: {skipped}")
    if errors > 0:
        print(f"Errors: {errors}")
    print(f"Total replacements: {total_replacements}")
    if channel_edited:
        print("\nNote: Changes were made directly on Telegram channel.")


def display_and_get_action(msg_id, msg, current, total, search, replace,
                           channel_edit_mode=False, case_sensitive=True, last_edit=None):
    """
    Display message preview and get user action.

    Args:
        msg_id: Message ID
        msg: Original message dict
        current: Current message number
        total: Total messages
        search: Search text
        replace: Replacement text
        channel_edit_mode: If True, show channel editing UI text
        case_sensitive: Whether search is case sensitive
        last_edit: Dict with info about last edited message (for undo option)

    Returns:
        tuple: (action_str, preview_data) where action is 'approve', 'skip', 'quit', 'cancel', 'undo'
               and preview_data is the replacement preview dict (or None if skipped)
    """
    # Get original text for display
    raw_text, entities = get_message_entities(msg)
    original_markdown = entities_to_markdown(raw_text, entities)

    # Generate preview with replacement using the user's case sensitivity choice
    actual_preview = apply_replacement_to_message(msg, search, replace, case_sensitive)

    if not actual_preview:
        return 'skip', None

    new_markdown = actual_preview['text']
    replacement_count = actual_preview['replacement_count']

    # Count URL replacements (not counted in replacement_count)
    url_replacements = 0
    # Get original entities (from stored or parsed from markdown)
    _, old_entities = get_message_entities(msg)
    new_entities = actual_preview['entities']

    # Compare URLs in entities
    old_urls = [e.get('url', '') for e in old_entities if isinstance(e, dict) and e.get('url')]
    new_urls = [e.get('url', '') for e in new_entities if isinstance(e, dict) and e.get('url')]

    for old_url, new_url in zip(old_urls, new_urls):
        if old_url != new_url:
            url_replacements += 1

    while True:
        print("\n" + "=" * 70)
        mode_indicator = " [CHANNEL EDIT]" if channel_edit_mode else ""
        print(f"MESSAGE {current} of {total} (ID: #{msg_id}){mode_indicator}")
        print(f"Date: {msg.get('date', 'Unknown')}")
        print("=" * 70)

        # Show last edit info if available (for undo option)
        if last_edit and channel_edit_mode:
            print(f"[Last edited: #{last_edit['msg_id']} - press U to undo]")
            print("=" * 70)

        print("\nORIGINAL:")
        print("-" * 70)
        print(original_markdown)

        print("\n" + "-" * 70)
        print(f"AFTER REPLACEMENT ('{search}' -> '{replace}'):")
        print("-" * 70)
        print(new_markdown)

        print("\n" + "-" * 70)
        changes_desc = f"Changes: {replacement_count} text replacement(s)"
        if url_replacements > 0:
            changes_desc += f", {url_replacements} URL(s)"
        print(changes_desc)
        if channel_edit_mode:
            print("(Will edit message on Telegram)")
        print("-" * 70)

        if channel_edit_mode:
            if last_edit:
                print("\n[A]pprove & Edit  [S]kip  [U]ndo last  [V]iew full  [Q]uit  [C]ancel")
            else:
                print("\n[A]pprove & Edit  [S]kip  [V]iew full  [Q]uit  [C]ancel all")
        else:
            print("\n[A]pprove  [S]kip  [V]iew full  [Q]uit (save approved)  [C]ancel all")
        choice = input("Enter choice: ").strip().lower()

        if choice == 'a':
            # Update replacement count to include URL changes
            actual_preview['replacement_count'] = replacement_count + url_replacements
            return 'approve', actual_preview
        elif choice == 's':
            return 'skip', None
        elif choice == 'v':
            print("\n" + "=" * 70)
            print("FULL ORIGINAL MESSAGE:")
            print("=" * 70)
            print(original_markdown)
            print("\n" + "=" * 70)
            print("FULL MESSAGE AFTER REPLACEMENT:")
            print("=" * 70)
            print(new_markdown)
            input("\nPress Enter to continue...")
        elif choice == 'u' and last_edit and channel_edit_mode:
            return 'undo', None
        elif choice == 'q':
            return 'quit', None
        elif choice == 'c':
            return 'cancel', None
        else:
            valid_choices = "A, S, V, Q, or C"
            if last_edit and channel_edit_mode:
                valid_choices = "A, S, U, V, Q, or C"
            print(f"Invalid choice. Please enter {valid_choices}.")


async def batch_search_replace(db, db_path, search, replace, case_sensitive=True, dry_run=False):
    """
    Batch search and replace without interactive approval.
    Useful for scripting or when you're sure about the replacement.

    Args:
        db: Database dictionary
        db_path: Path to database file
        search: Text to search for
        replace: Text to replace with
        case_sensitive: Whether search is case sensitive
        dry_run: If True, don't actually apply changes

    Returns:
        dict: Statistics about the operation
    """
    active = get_active_channel(db)
    if not active:
        return {'error': 'No active channel selected'}

    channel_id = str(active['id'])
    if channel_id not in db.get('messages', {}):
        return {'error': 'No messages for this channel'}

    messages = db['messages'][channel_id]
    matches = find_matching_messages(db, channel_id, search, case_sensitive)

    if not matches:
        return {
            'found': 0,
            'replaced': 0,
            'total_replacements': 0
        }

    replaced = 0
    total_replacements = 0

    for msg_id, msg, _ in matches:
        preview = apply_replacement_to_message(msg, search, replace, case_sensitive)
        if preview:
            if not dry_run:
                # Store original
                if 'edit_history' not in msg:
                    msg['edit_history'] = []
                msg['edit_history'].append({
                    'date': str(datetime.now()),
                    'action': 'batch_search_replace',
                    'search': search,
                    'replace': replace,
                    'original_raw_text': msg.get('raw_text', ''),
                    'original_text': msg.get('text', '')
                })

                # Apply changes
                msg['raw_text'] = preview['raw_text']
                msg['text'] = preview['text']
                msg['entities'] = preview['entities']
                msg['last_update'] = str(datetime.now())

            replaced += 1
            total_replacements += preview['replacement_count']

    if not dry_run:
        save_database(db_path, db)

    return {
        'found': len(matches),
        'replaced': replaced,
        'total_replacements': total_replacements,
        'dry_run': dry_run
    }


async def restore_edited_messages(db, db_path, client=None):
    """
    Restore messages that were edited via channel search/replace.
    Shows list of edited messages and allows restoring them to original.

    Args:
        db: Database dictionary
        db_path: Path to database file
        client: Telegram client for channel restore
    """
    active = get_active_channel(db)
    if not active:
        print("\nNo active channel selected!")
        print("Please select a channel first (option 3 in main menu).")
        return

    channel_id = str(active['id'])
    if channel_id not in db.get('messages', {}):
        print("\nNo saved messages for this channel!")
        return

    messages = db['messages'][channel_id]

    # Find messages with restorable edits
    restorable = []
    for msg_id, msg in messages.items():
        if 'edit_history' in msg and msg['edit_history']:
            # Check for channel edits that can be restored
            for i, entry in enumerate(msg['edit_history']):
                if entry.get('can_restore') and entry.get('telegram_raw_text'):
                    restorable.append({
                        'msg_id': msg_id,
                        'msg': msg,
                        'history_index': i,
                        'entry': entry
                    })

    if not restorable:
        print("\nNo restorable channel edits found!")
        print("Only messages edited via 'Edit in channel' mode can be restored.")
        return

    print("\n" + "=" * 70)
    print("                    RESTORE EDITED MESSAGES")
    print("=" * 70)
    print(f"\nActive channel: {active['title']}")
    print(f"Found {len(restorable)} message(s) with restorable edits.")

    # Ask for restore mode
    print("\n" + "-" * 70)
    print("Restore Mode:")
    print("1. Local only (restore in database, not Telegram)")
    print("2. Restore in channel (restore messages on Telegram)")
    print("3. Cancel")
    print("-" * 70)

    mode_choice = input("Select mode (1/2/3): ").strip()

    if mode_choice == '3':
        print("\nCancelled.")
        return

    restore_in_channel = mode_choice == '2'

    if restore_in_channel:
        if not client:
            print("\nError: Client not available for channel restore!")
            return
        print("\nWARNING: This will restore messages directly on Telegram!")
        confirm = input("Continue? (y/N): ").strip().lower()
        if confirm != 'y':
            print("\nCancelled.")
            return

    # Process restorable messages
    restored = 0
    skipped = 0
    errors = 0

    for i, item in enumerate(restorable, 1):
        msg_id = item['msg_id']
        msg = item['msg']
        entry = item['entry']

        # Display message info
        print("\n" + "=" * 70)
        mode_indicator = " [CHANNEL RESTORE]" if restore_in_channel else ""
        print(f"MESSAGE {i} of {len(restorable)} (ID: #{msg_id}){mode_indicator}")
        print(f"Edit date: {entry.get('date', 'Unknown')}")
        print(f"Search: '{entry.get('search', '')}' -> Replace: '{entry.get('replace', '')}'")
        print("=" * 70)

        print("\nCURRENT (after edit):")
        print("-" * 70)
        print(msg.get('text', msg.get('raw_text', '[No text]')))

        print("\n" + "-" * 70)
        print("ORIGINAL (before edit):")
        print("-" * 70)
        print(entry.get('telegram_text', entry.get('telegram_raw_text', '[No backup]')))

        print("\n" + "-" * 70)
        if restore_in_channel:
            print("[R]estore on Telegram  [S]kip  [Q]uit")
        else:
            print("[R]estore locally  [S]kip  [Q]uit")

        choice = input("Enter choice: ").strip().lower()

        if choice == 'r':
            try:
                original_raw_text = entry.get('telegram_raw_text', '')
                original_entities = entry.get('telegram_entities', [])

                if not original_raw_text:
                    print(f"  Error: No backup data for message #{msg_id}")
                    errors += 1
                    continue

                if restore_in_channel:
                    # Restore on Telegram
                    print(f"  Restoring message #{msg_id} on Telegram...")
                    original_entity_objs = dicts_to_entities(original_entities)

                    await client.edit_message(
                        active['id'],
                        int(msg_id),
                        original_raw_text,
                        formatting_entities=original_entity_objs
                    )

                    # Mark as restored in history
                    entry['restored'] = True
                    entry['restore_date'] = str(datetime.now())
                    entry['can_restore'] = False  # Already restored

                    # Update local database
                    msg['raw_text'] = original_raw_text
                    msg['text'] = entry.get('telegram_text', '')
                    msg['entities'] = original_entities
                    msg['last_update'] = str(datetime.now())
                    msg['restored_from_backup'] = True

                    save_database(db_path, db)
                    print(f"  Message #{msg_id} restored on Telegram!")

                    # Rate limiting delay
                    await asyncio.sleep(0.5)

                else:
                    # Local-only restore
                    entry['restored'] = True
                    entry['restore_date'] = str(datetime.now())
                    entry['restored_locally_only'] = True

                    msg['raw_text'] = original_raw_text
                    msg['text'] = entry.get('telegram_text', '')
                    msg['entities'] = original_entities
                    msg['last_update'] = str(datetime.now())

                    save_database(db_path, db)
                    print(f"  Message #{msg_id} restored locally (not on Telegram).")

                restored += 1

            except Exception as e:
                error_msg = str(e)
                if 'MESSAGE_NOT_MODIFIED' in error_msg:
                    print(f"  Message #{msg_id} content unchanged.")
                    skipped += 1
                elif 'MESSAGE_AUTHOR_REQUIRED' in error_msg:
                    print(f"  Error: No permission to edit message #{msg_id}.")
                    errors += 1
                else:
                    print(f"  Error restoring message #{msg_id}: {e}")
                    errors += 1

        elif choice == 's':
            skipped += 1
            print(f"  Message #{msg_id} skipped.")
        elif choice == 'q':
            print("\nStopping restore.")
            break
        else:
            print("Invalid choice, skipping.")
            skipped += 1

    # Summary
    print("\n" + "=" * 70)
    print("                        RESTORE SUMMARY")
    print("=" * 70)
    print(f"\nTotal restorable messages: {len(restorable)}")
    print(f"Messages restored: {restored}")
    print(f"Messages skipped: {skipped}")
    if errors > 0:
        print(f"Errors: {errors}")
    if restore_in_channel:
        print("\nNote: Messages were restored directly on Telegram channel.")


def list_edited_messages(db):
    """
    List all messages that have been edited via search/replace.

    Args:
        db: Database dictionary
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

    # Find messages with edit history
    edited = []
    for msg_id, msg in messages.items():
        if 'edit_history' in msg and msg['edit_history']:
            edited.append((msg_id, msg))

    if not edited:
        print("\nNo edited messages found!")
        return

    # Sort by message ID
    edited.sort(key=lambda x: int(x[0]))

    print("\n" + "=" * 70)
    print("                    EDITED MESSAGES")
    print("=" * 70)
    print(f"\nActive channel: {active['title']}")
    print(f"Total edited messages: {len(edited)}")
    print("\n" + "-" * 70)
    print(f"{'ID':<10} | {'Edits':<6} | {'Restorable':<10} | {'Last Edit':<20}")
    print("-" * 70)

    for msg_id, msg in edited:
        history = msg['edit_history']
        num_edits = len(history)

        # Count restorable edits
        restorable = sum(1 for e in history if e.get('can_restore') and e.get('telegram_raw_text'))
        restorable_str = f"Yes ({restorable})" if restorable > 0 else "No"

        # Get last edit date
        last_edit = history[-1].get('date', 'Unknown')[:19] if history else 'Unknown'

        print(f"{msg_id:<10} | {num_edits:<6} | {restorable_str:<10} | {last_edit:<20}")

    print("-" * 70)
    print(f"\nTotal: {len(edited)} messages with {sum(len(m['edit_history']) for _, m in edited)} total edits")
