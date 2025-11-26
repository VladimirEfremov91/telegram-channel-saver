# Search and Replace with Formatting Preservation

## Overview

This document describes how Telegram message formatting works and how to implement a search-and-replace feature that preserves all formatting (bold, italic, links, etc.) while modifying text content.

## Two Approaches: Markdown vs Native Entities

### Approach A: Markdown-Based (Current)
- Use `message.text` which contains markdown syntax
- Parse with `markdown.parse()` → get (plain_text, entities)
- Modify and reconstruct with `markdown.unparse()`

### Approach B: Native Entities (Recommended)
- Use `message.raw_text` (plain text) + `message.entities` (list of MessageEntity)
- Work directly with Telegram's native format
- More accurate, handles all 21 entity types including those markdown doesn't support
- **This is how Telegram actually stores and transmits messages**

## Why Native Entities is Better

According to [Telethon documentation](https://docs.telethon.dev/en/stable/modules/custom.html) and [DeepWiki](https://deepwiki.com/LonamiWebs/Telethon/7.1-markdown-and-html-parsing):

> "Telegram does not natively support markdown or HTML. Clients such as Telethon parse the text into a list of formatting MessageEntity at different offsets."

> "Message.text returns the text formatted using the current parse mode of the client. By default, this is Telegram's markdown."

The native format is:
- **`raw_text`**: Plain text without any formatting markers
- **`entities`**: Array of MessageEntity objects with offset/length

This is more reliable because:
1. No parsing/unparsing round-trip errors
2. Handles entity types that markdown doesn't support (CustomEmoji, Spoiler, etc.)
3. Preserves exact offsets as Telegram uses them
4. Works correctly with UTF-16 surrogate pairs (emojis)

## Current Database Structure

Messages are stored with the following text-related fields:

| Field | Description | Example |
|-------|-------------|---------|
| `text` | Markdown-formatted text from Telethon | `[Link](https://example.com) and **bold**` |
| `raw_text` | Plain text without formatting | `Link and bold` |
| `text_html` | Currently same as `text` (should be HTML) | `[Link](https://example.com) and **bold**` |
| `entities` | **NOT STORED YET** - Need to add this | `[{_type: "MessageEntityBold", offset: 0, length: 4}, ...]` |

**Note:** The `text` field in our database contains markdown formatting because Telethon's `message.text` property automatically converts entities to markdown format.

## Telegram Message Formatting Architecture

### Message Entities

Telegram stores formatted text as:
1. **Plain text** - The actual characters without formatting markers
2. **Entities array** - List of formatting instructions with offset/length

Example:
```
Text: "Hello bold and click here"
Entities:
  - MessageEntityBold(offset=6, length=4)      → "bold"
  - MessageEntityTextUrl(offset=15, length=10, url="https://example.com") → "click here"
```

### Available Entity Types (21 total)

| Entity Type | Description | Markdown Syntax |
|-------------|-------------|-----------------|
| `MessageEntityBold` | Bold text | `**text**` |
| `MessageEntityItalic` | Italic text | `*text*` |
| `MessageEntityStrike` | Strikethrough | `~~text~~` |
| `MessageEntityUnderline` | Underlined text | `__text__` |
| `MessageEntityCode` | Inline code | `` `code` `` |
| `MessageEntityPre` | Code block with language | ` ```python\ncode``` ` |
| `MessageEntityTextUrl` | Hyperlink | `[text](url)` |
| `MessageEntityMention` | @username mention | `@username` |
| `MessageEntityMentionName` | User mention by ID | N/A |
| `MessageEntityUrl` | Plain URL | `https://...` |
| `MessageEntityEmail` | Email address | `user@example.com` |
| `MessageEntityPhone` | Phone number | `+1234567890` |
| `MessageEntityHashtag` | Hashtag | `#hashtag` |
| `MessageEntityCashtag` | Cashtag | `$TICKER` |
| `MessageEntityBotCommand` | Bot command | `/command` |
| `MessageEntityBlockquote` | Block quote | `> quote` |
| `MessageEntitySpoiler` | Spoiler text | `\|\|spoiler\|\|` |
| `MessageEntityCustomEmoji` | Custom emoji | N/A |
| `MessageEntityBankCard` | Bank card number | N/A |
| `MessageEntityUnknown` | Unknown entity | N/A |

### Telethon Extensions

Telethon provides markdown/HTML parsing utilities:

```python
from telethon.extensions import markdown

# Parse markdown to (text, entities)
text, entities = markdown.parse("**bold** and [link](https://example.com)")
# Result: text="bold and link", entities=[MessageEntityBold(...), MessageEntityTextUrl(...)]

# Convert back to markdown
markdown_text = markdown.unparse(text, entities)
# Result: "**bold** and [link](https://example.com)"
```

## The Challenge: Search & Replace

When replacing text, we must handle:

1. **Offset Adjustment** - Entities after the replacement need their `offset` shifted
2. **Length Adjustment** - Entities containing the replacement need their `length` updated
3. **URL Replacement** - `MessageEntityTextUrl.url` may also need updating
4. **Nested Entities** - Multiple entities can overlap

### Algorithm for Safe Replacement

```python
import re
import copy
from telethon.extensions import markdown
from telethon.tl.types import MessageEntityTextUrl

def search_replace_preserving_formatting(text, entities, search, replace):
    """
    Replace text while preserving entity formatting.

    Args:
        text: Plain text (without markdown markers)
        entities: List of MessageEntity objects
        search: String to find
        replace: String to replace with

    Returns:
        tuple: (new_text, new_entities)
    """
    # Deep copy to avoid modifying originals
    entities = [copy.copy(e) for e in entities]
    entities = sorted(entities, key=lambda e: e.offset)

    new_text = text
    offset_delta = 0

    # Process each match
    for match in re.finditer(re.escape(search), text):
        start = match.start()
        end = match.end()
        len_diff = len(replace) - len(search)

        # Apply replacement
        adjusted_start = start + offset_delta
        new_text = new_text[:adjusted_start] + replace + new_text[adjusted_start + len(search):]

        # Adjust entity offsets and lengths
        for ent in entities:
            ent_start = ent.offset
            ent_end = ent.offset + ent.length

            if ent_start >= end:
                # Entity is after replacement - shift offset
                ent.offset += len_diff
            elif ent_start <= start and ent_end >= end:
                # Entity contains replacement - adjust length
                ent.length += len_diff
            # Entities before replacement: no change needed

        offset_delta += len_diff

    # Also replace in URL attributes
    for ent in entities:
        if isinstance(ent, MessageEntityTextUrl) and ent.url:
            ent.url = ent.url.replace(search, replace)

    return new_text, entities
```

## Implementation Approach

### Option A: Work with Markdown (Current Data) - Fallback

Since our database currently stores markdown-formatted text, we can:

1. Use `markdown.parse()` to get (plain_text, entities)
2. Apply replacements with entity adjustment
3. Use `markdown.unparse()` to get markdown back
4. Store updated markdown in database

**Pros:** Works with existing data without migration
**Cons:** Some edge cases with nested markdown, doesn't support all entity types

### Option B: Store Raw Entities (Recommended) - Native Format

Modify message saving to store:
- `raw_text`: Plain text (already stored)
- `entities`: Serialized entity list (JSON)

Then use entities directly for search/replace.

**Pros:** More accurate, handles ALL entity types, matches Telegram's native format
**Cons:** Requires updating message download code

## Entity Serialization

### Converting Entities to JSON

```python
def entity_to_dict(entity):
    """Convert a MessageEntity to a serializable dict."""
    d = {
        '_type': type(entity).__name__,
        'offset': entity.offset,
        'length': entity.length
    }
    # Type-specific attributes
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
```

### Reconstructing Entities from JSON

```python
from telethon.tl import types

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

def dict_to_entity(d):
    """Convert a dict back to a MessageEntity."""
    entity_type = d['_type']
    offset = d['offset']
    length = d['length']

    cls = ENTITY_TYPES.get(entity_type)
    if not cls:
        return None

    # Handle different constructor signatures
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
```

## Recommended Approach: Native Entities

### Algorithm for Search-Replace with Raw Entities

```python
import re
import copy
from telethon.tl.types import MessageEntityTextUrl

def search_replace_with_raw_entities(raw_text, entities, search, replace):
    """
    Replace text in raw_text while adjusting entity offsets/lengths.
    This works with Telegram's native format (raw_text + entities list).

    Args:
        raw_text: Plain text without formatting markers
        entities: List of MessageEntity objects
        search: String to find
        replace: String to replace with

    Returns:
        tuple: (new_raw_text, new_entities)
    """
    # Deep copy entities
    new_entities = [copy.copy(e) for e in entities]
    new_entities = sorted(new_entities, key=lambda e: e.offset)

    new_text = raw_text
    offset_delta = 0

    # Find all matches and process from left to right
    for match in re.finditer(re.escape(search), raw_text):
        start = match.start()
        end = match.end()
        len_diff = len(replace) - len(search)

        # Apply replacement at adjusted position
        adjusted_start = start + offset_delta
        new_text = new_text[:adjusted_start] + replace + new_text[adjusted_start + len(search):]

        # Adjust entities
        for ent in new_entities:
            ent_start = ent.offset
            ent_end = ent.offset + ent.length

            # Entity is completely AFTER the replacement
            if ent_start >= end:
                ent.offset += len_diff

            # Entity CONTAINS the replacement
            elif ent_start <= start and ent_end >= end:
                ent.length += len_diff

            # Replacement OVERLAPS entity start
            elif start < ent_start < end:
                overlap = end - ent_start
                ent.offset = adjusted_start + len(replace)
                ent.length = max(0, ent.length - overlap)

            # Replacement OVERLAPS entity end
            elif start < ent_end <= end:
                overlap = ent_end - start
                ent.length = max(0, ent.length - overlap)

        offset_delta += len_diff

    # Also replace in URL attributes of TextUrl entities
    for ent in new_entities:
        if isinstance(ent, MessageEntityTextUrl) and ent.url:
            ent.url = ent.url.replace(search, replace)

    # Remove zero-length entities
    new_entities = [e for e in new_entities if e.length > 0]

    return new_text, new_entities
```

### Regenerating Markdown After Replacement

```python
from telethon.extensions import markdown

def apply_replacement_and_get_markdown(raw_text, entities, search, replace):
    """
    Apply replacement and return both raw format and markdown.
    """
    new_raw, new_entities = search_replace_with_raw_entities(
        raw_text, entities, search, replace
    )

    # Generate markdown for display
    new_markdown = markdown.unparse(new_raw, new_entities)

    return {
        'raw_text': new_raw,
        'entities': new_entities,
        'text': new_markdown  # For backward compatibility
    }
```

### Phase 2: Message-by-Message Approval UI

```
╔══════════════════════════════════════════════════════════════════╗
║                    MESSAGE #508 - PREVIEW                         ║
╠══════════════════════════════════════════════════════════════════╣
║ ORIGINAL:                                                         ║
║ ─────────                                                         ║
║ Visit [example.com](https://example.com) for **bold info**.       ║
║                                                                   ║
║ AFTER REPLACEMENT (example.com → newsite.org):                   ║
║ ──────────────────────────────────────────────────                ║
║ Visit [newsite.org](https://newsite.org) for **bold info**.      ║
║                                                                   ║
║ Changes: 2 replacements                                           ║
╠══════════════════════════════════════════════════════════════════╣
║ [A]pprove  [S]kip  [Q]uit                                        ║
╚══════════════════════════════════════════════════════════════════╝
```

## Edge Cases to Handle

### 1. Replacement Inside Entity
```
Original: **bold text here**
Search: "text"
Replace: "content"
Result: **bold content here**  ✓
```

### 2. Replacement Spanning Entity Boundary
```
Original: **bold** text
Search: "bold text"
Replace: "new"
Result: Tricky - may break formatting ⚠️
Solution: Warn user, require confirmation
```

### 3. URL in Link Text vs URL Attribute
```
Original: [example.com](https://example.com/page)
Search: "example.com"
Replace: "newsite.org"
Result: [newsite.org](https://newsite.org/page)  ✓
```

### 4. Partial URL Match
```
Original: https://example.com/path
Search: "example"
Replace: "test"
Result: https://test.com/path  ✓
```

### 5. Case Sensitivity
- Implement case-insensitive search option
- Preserve original case when possible

## Testing Checklist

- [ ] Simple text replacement
- [ ] Replacement in bold text
- [ ] Replacement in italic text
- [ ] Replacement in link text
- [ ] Replacement in URL attribute
- [ ] Multiple replacements in one message
- [ ] Nested formatting (bold + italic)
- [ ] Unicode/emoji preservation
- [ ] Empty replacement (deletion)
- [ ] Cyrillic and other non-ASCII text

## References

- Telethon Documentation: https://docs.telethon.dev/en/stable/
- Message Entities: https://docs.telethon.dev/en/stable/modules/custom.html
- Markdown Extension: `telethon.extensions.markdown`
- HTML Extension: `telethon.extensions.html` (alternative parser)
