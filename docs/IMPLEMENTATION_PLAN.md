# Search and Replace Feature - Implementation Plan

## Feature Overview

Add a search-and-replace feature to the Telegram Channel Saver that:
1. Searches through saved messages for specified text/URLs
2. Shows each matching message with original and proposed replacement
3. Allows user to approve/skip/quit for each message
4. Preserves all message formatting (bold, italic, links, etc.)
5. Updates the local database only (does not modify Telegram)

## Key Decision: Native Entities Approach

Based on research (see [SEARCH_REPLACE_FORMATTING.md](./SEARCH_REPLACE_FORMATTING.md)), we will use **Telegram's native entity format** instead of markdown:

- **Native format**: `raw_text` (plain text) + `entities` (list of MessageEntity with offset/length)
- **Why**: More accurate, handles all 21 entity types, no parsing round-trip errors
- **Trade-off**: Need to update message download code to store entities

## Architecture

### New Files to Create

```
src/
├── search_replace.py    # Main search-replace logic + UI
└── formatting.py        # Entity serialization + replacement algorithm
```

### Files to Modify

```
src/
├── messages.py          # Add entities storage when downloading
└── app.py               # Add menu option 14
```

### Menu Integration

Add new option **14. Search and Replace** to the main menu in `src/app.py`.

## Implementation Steps

### Step 0: Update Message Download to Store Entities

In `src/messages.py`, add entity storage when downloading messages:

```python
# Add to message_dict creation (around line 180-210):

# Serialize entities if present
'entities': [entity_to_dict(e) for e in (message.entities or [])]
```

This requires the `entity_to_dict` function from `formatting.py` (see Step 1).

### Step 1: Create Formatting Utilities (`src/formatting.py`)

```python
"""
Formatting preservation utilities for search and replace.
Uses Telegram's native entity format (raw_text + entities).
"""
import re
import copy
from telethon.tl import types
from telethon.extensions import markdown

# All supported entity types
ENTITY_TYPES = {
    'MessageEntityBold': types.MessageEntityBold,
    'MessageEntityItalic': types.MessageEntityItalic,
    'MessageEntityStrike': types.MessageEntityStrike,
    'MessageEntityUnderline': types.MessageEntityUnderline,
    'MessageEntityCode': types.MessageEntityCode,
    'MessageEntityPre': types.MessageEntityPre,
    'MessageEntityTextUrl': types.MessageEntityTextUrl,
    'MessageEntityUrl': types.MessageEntityUrl,
    'MessageEntityMention': types.MessageEntityMention,
    'MessageEntityMentionName': types.MessageEntityMentionName,
    'MessageEntityHashtag': types.MessageEntityHashtag,
    'MessageEntityCashtag': types.MessageEntityCashtag,
    'MessageEntityBotCommand': types.MessageEntityBotCommand,
    'MessageEntityEmail': types.MessageEntityEmail,
    'MessageEntityPhone': types.MessageEntityPhone,
    'MessageEntityBlockquote': types.MessageEntityBlockquote,
    'MessageEntitySpoiler': types.MessageEntitySpoiler,
    'MessageEntityCustomEmoji': types.MessageEntityCustomEmoji,
}

def entity_to_dict(entity):
    """Convert a MessageEntity to a JSON-serializable dict."""
    d = {
        '_type': type(entity).__name__,
        'offset': entity.offset,
        'length': entity.length
    }
    if hasattr(entity, 'url') and entity.url:
        d['url'] = entity.url
    if hasattr(entity, 'language') and entity.language:
        d['language'] = entity.language
    if hasattr(entity, 'user_id'):
        d['user_id'] = entity.user_id
    if hasattr(entity, 'document_id'):
        d['document_id'] = entity.document_id
    if hasattr(entity, 'collapsed'):
        d['collapsed'] = entity.collapsed
    return d

def dict_to_entity(d):
    """Convert a dict back to a MessageEntity object."""
    entity_type = d['_type']
    offset = d['offset']
    length = d['length']

    cls = ENTITY_TYPES.get(entity_type)
    if not cls:
        return None

    if entity_type == 'MessageEntityTextUrl':
        return cls(offset, length, d.get('url', ''))
    elif entity_type == 'MessageEntityPre':
        return cls(offset, length, d.get('language', ''))
    elif entity_type == 'MessageEntityMentionName':
        return cls(offset, length, d.get('user_id', 0))
    elif entity_type == 'MessageEntityCustomEmoji':
        return cls(offset, length, d.get('document_id', 0))
    elif entity_type == 'MessageEntityBlockquote':
        return cls(offset, length, collapsed=d.get('collapsed', False))
    else:
        return cls(offset, length)

def search_replace_with_entities(raw_text, entities, search, replace):
    """
    Replace text while adjusting entity offsets and lengths.
    Works with Telegram's native format.

    Args:
        raw_text: Plain text without formatting
        entities: List of MessageEntity objects or dicts
        search: String to find
        replace: String to replace with

    Returns:
        tuple: (new_raw_text, new_entities_as_dicts)
    """
    # Convert dicts to entities if needed
    entity_objs = []
    for e in (entities or []):
        if isinstance(e, dict):
            obj = dict_to_entity(e)
            if obj:
                entity_objs.append(obj)
        else:
            entity_objs.append(copy.copy(e))

    entity_objs = sorted(entity_objs, key=lambda e: e.offset)

    new_text = raw_text
    offset_delta = 0

    for match in re.finditer(re.escape(search), raw_text):
        start = match.start()
        end = match.end()
        len_diff = len(replace) - len(search)

        adjusted_start = start + offset_delta
        new_text = new_text[:adjusted_start] + replace + new_text[adjusted_start + len(search):]

        for ent in entity_objs:
            ent_start = ent.offset
            ent_end = ent.offset + ent.length

            if ent_start >= end:
                ent.offset += len_diff
            elif ent_start <= start and ent_end >= end:
                ent.length += len_diff
            elif start < ent_start < end:
                overlap = end - ent_start
                ent.offset = adjusted_start + len(replace)
                ent.length = max(0, ent.length - overlap)
            elif start < ent_end <= end:
                overlap = ent_end - start
                ent.length = max(0, ent.length - overlap)

        offset_delta += len_diff

    # Replace in URL attributes
    for ent in entity_objs:
        if isinstance(ent, types.MessageEntityTextUrl) and ent.url:
            ent.url = ent.url.replace(search, replace)

    # Remove zero-length entities and convert back to dicts
    new_entities = [entity_to_dict(e) for e in entity_objs if e.length > 0]

    return new_text, new_entities

def entities_to_markdown(raw_text, entities):
    """Convert raw_text + entities to markdown for display."""
    entity_objs = []
    for e in (entities or []):
        if isinstance(e, dict):
            obj = dict_to_entity(e)
            if obj:
                entity_objs.append(obj)
        else:
            entity_objs.append(e)
    return markdown.unparse(raw_text, entity_objs)

def get_entities_from_markdown(text):
    """Parse markdown to get raw_text and entities (for legacy data)."""
    raw_text, entity_objs = markdown.parse(text)
    return raw_text, [entity_to_dict(e) for e in entity_objs]
```

### Step 2: Create Search-Replace Module (`src/search_replace.py`)

```python
"""
Search and replace functionality with message-by-message approval.
"""
import os
from datetime import datetime
from src.channels import get_active_channel
from src.database import save_database
from src.formatting import (
    parse_message_text,
    unparse_message_text,
    replace_preserving_entities,
    highlight_changes
)

async def search_replace_messages(db, db_path):
    """
    Main entry point for search and replace feature.
    """
    # 1. Get active channel
    # 2. Get search/replace terms from user
    # 3. Find all matching messages
    # 4. Show each match with preview
    # 5. Process approvals
    # 6. Save changes
    pass

def find_matching_messages(db, channel_id, search_term, case_sensitive=False):
    """
    Find all messages containing the search term.
    Returns list of (message_id, message_dict, match_count)
    """
    pass

def preview_replacement(message, search, replace):
    """
    Generate preview of replacement without modifying message.
    Returns dict with original, new, and diff info.
    """
    pass

def apply_replacement(message, search, replace):
    """
    Apply replacement to message, preserving formatting.
    Returns updated message dict.
    """
    pass

def display_message_preview(preview, message_num, total_messages):
    """
    Display formatted preview in terminal.
    Shows original, replacement, and action options.
    """
    pass
```

### Step 3: User Interface Flow

```
╔══════════════════════════════════════════════════════════════════════════╗
║                         SEARCH AND REPLACE                                ║
╚══════════════════════════════════════════════════════════════════════════╝

Active channel: Sergio Bulaev AI (ID: 2234839119)
Total messages: 834

Search Options:
1. Search and replace text
2. Search and replace URL/domain
3. Back to main menu

Enter choice: 1

Enter text to search: example.com
Enter replacement text: newsite.org
Case sensitive? (y/N): n

Searching... Found 15 messages with matches.

═══════════════════════════════════════════════════════════════════════════
MESSAGE 1 of 15 (ID: #508, Date: 2024-01-15 14:30:00)
═══════════════════════════════════════════════════════════════════════════

ORIGINAL:
─────────
Visit [example.com](https://example.com) for more info.
Check out **example.com** for details.

AFTER REPLACEMENT:
──────────────────
Visit [newsite.org](https://newsite.org) for more info.
Check out **newsite.org** for details.

Changes: 4 occurrences will be replaced

───────────────────────────────────────────────────────────────────────────
[A]pprove  [S]kip  [V]iew full message  [Q]uit (save approved)  [C]ancel all
───────────────────────────────────────────────────────────────────────────

Enter choice: a

✓ Message #508 approved for replacement.

═══════════════════════════════════════════════════════════════════════════
MESSAGE 2 of 15 (ID: #512, Date: 2024-01-16 09:15:00)
...
```

### Step 4: Summary and Confirmation

```
═══════════════════════════════════════════════════════════════════════════
                              SUMMARY
═══════════════════════════════════════════════════════════════════════════

Total messages found: 15
Approved for replacement: 12
Skipped: 3

Approved messages:
  #508 - 4 replacements
  #512 - 2 replacements
  #523 - 1 replacement
  ... (showing first 10)

Total replacements: 28

Apply all approved changes? (y/N): y

Applying changes...
✓ Message #508 updated
✓ Message #512 updated
✓ Message #523 updated
...

Database saved successfully.

12 messages updated with 28 total replacements.
```

### Step 5: Integration with Main App

In `src/app.py`:

```python
from src.search_replace import search_replace_messages

# In menu display (around line 100):
print("14. Search and replace in messages")

# In menu handler (around line 300):
elif choice == '14':
    await search_replace_messages(self.db, self.db_path)
```

## Data Model

### Message Before (Current Format - Legacy)
```json
{
  "id": 508,
  "text": "Visit [example.com](https://example.com) for **bold info**.",
  "raw_text": "Visit example.com for bold info.",
  "text_html": "...",
  "last_update": "2024-01-15 14:30:00"
}
```

### Message Before (New Format - With Native Entities)
```json
{
  "id": 508,
  "text": "Visit [example.com](https://example.com) for **bold info**.",
  "raw_text": "Visit example.com for bold info.",
  "entities": [
    {"_type": "MessageEntityTextUrl", "offset": 6, "length": 11, "url": "https://example.com"},
    {"_type": "MessageEntityBold", "offset": 22, "length": 9}
  ],
  "last_update": "2024-01-15 14:30:00"
}
```

### Message After Replacement
```json
{
  "id": 508,
  "text": "Visit [newsite.org](https://newsite.org) for **bold info**.",
  "raw_text": "Visit newsite.org for bold info.",
  "entities": [
    {"_type": "MessageEntityTextUrl", "offset": 6, "length": 11, "url": "https://newsite.org"},
    {"_type": "MessageEntityBold", "offset": 22, "length": 9}
  ],
  "last_update": "2024-11-26 10:00:00",
  "edit_history": [
    {
      "date": "2024-11-26 10:00:00",
      "action": "search_replace",
      "search": "example.com",
      "replace": "newsite.org",
      "original_raw_text": "Visit example.com for bold info.",
      "original_entities": [...]
    }
  ]
}
```

### Backward Compatibility

For messages without `entities` field (legacy data), the system will:
1. Parse `text` field using `markdown.parse()` to extract entities
2. Apply replacement
3. Store both `text` (markdown) and new `entities` field

## Error Handling

1. **No active channel** - Prompt user to select channel first
2. **No messages saved** - Inform user to download messages first
3. **No matches found** - Display "No messages found matching 'search term'"
4. **Formatting error** - Log warning, show original, ask user to skip or force
5. **Database save error** - Rollback changes, show error, keep backup

## Testing Plan

### Unit Tests
- `test_replace_preserving_entities()` - Various entity types
- `test_url_replacement()` - URLs in text and attributes
- `test_nested_formatting()` - Bold inside italic, etc.
- `test_unicode_handling()` - Cyrillic, emoji, special chars
- `test_edge_cases()` - Empty strings, no matches, all matches

### Integration Tests
- Full workflow with real database
- Menu navigation
- Approval flow
- Database persistence

### Manual Testing
- Visual verification of formatting
- Different terminal widths
- Large messages
- Many matches

## Future Enhancements

1. **Regex support** - Allow regex patterns for search
2. **Batch operations** - Replace in all messages without approval
3. **Undo feature** - Restore from edit_history
4. **Export changes** - Generate report of all changes
5. **Dry run mode** - Preview all changes without prompts
6. **Filter by date** - Only replace in messages from date range
7. **Filter by author** - Only replace in messages from specific user

## Dependencies

No new dependencies required. Uses existing:
- `telethon.extensions.markdown` - For formatting
- `telethon.tl.types` - For entity types

## Timeline Estimate

| Task | Complexity |
|------|------------|
| Step 1: Formatting utilities | Low |
| Step 2: Core search-replace | Medium |
| Step 3: UI flow | Medium |
| Step 4: Summary/confirmation | Low |
| Step 5: App integration | Low |
| Testing & edge cases | Medium |

## Risk Assessment

| Risk | Mitigation |
|------|------------|
| Formatting corruption | Extensive testing, user preview before apply |
| Data loss | Keep edit_history, backup before changes |
| Unicode issues | Use proper encoding, test with various scripts |
| Performance with many messages | Batch processing, progress indicator |
