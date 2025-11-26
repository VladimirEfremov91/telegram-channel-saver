"""
Formatting preservation utilities for search and replace.
Uses Telegram's native entity format (raw_text + entities).
"""
import re
import copy
from telethon.tl import types
from telethon.extensions import markdown

# All supported entity types for serialization/deserialization
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
    """
    Convert a MessageEntity to a JSON-serializable dict.

    Args:
        entity: A Telethon MessageEntity object

    Returns:
        dict: Serializable representation of the entity
    """
    if entity is None:
        return None

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
    if hasattr(entity, 'user_id') and entity.user_id:
        d['user_id'] = entity.user_id
    if hasattr(entity, 'document_id') and entity.document_id:
        d['document_id'] = entity.document_id
    if hasattr(entity, 'collapsed') and entity.collapsed:
        d['collapsed'] = entity.collapsed

    return d


def dict_to_entity(d):
    """
    Convert a dict back to a MessageEntity object.

    Args:
        d: Dictionary with entity data

    Returns:
        MessageEntity object or None if type unknown
    """
    if not d or '_type' not in d:
        return None

    entity_type = d['_type']
    offset = d.get('offset', 0)
    length = d.get('length', 0)

    cls = ENTITY_TYPES.get(entity_type)
    if not cls:
        return None

    # Handle different constructor signatures
    try:
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
    except Exception:
        return None


def entities_to_dicts(entities):
    """
    Convert a list of entities to list of dicts.

    Args:
        entities: List of MessageEntity objects or None

    Returns:
        list: List of serializable dicts
    """
    if not entities:
        return []
    return [entity_to_dict(e) for e in entities if e is not None]


def dicts_to_entities(dicts):
    """
    Convert a list of dicts back to MessageEntity objects.

    Args:
        dicts: List of entity dicts or None

    Returns:
        list: List of MessageEntity objects
    """
    if not dicts:
        return []
    entities = [dict_to_entity(d) for d in dicts]
    return [e for e in entities if e is not None]


def search_replace_with_entities(raw_text, entities, search, replace, case_sensitive=True):
    """
    Replace text while adjusting entity offsets and lengths.
    Works with Telegram's native format.

    Args:
        raw_text: Plain text without formatting markers
        entities: List of MessageEntity objects or dicts
        search: String to find
        replace: String to replace with
        case_sensitive: Whether search is case sensitive

    Returns:
        tuple: (new_raw_text, new_entities_as_dicts, replacement_count)
    """
    if not raw_text or not search:
        return raw_text, entities_to_dicts(entities) if entities else [], 0

    # Convert dicts to entity objects if needed
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
    replacement_count = 0

    # Build regex pattern
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(re.escape(search), flags)

    # Find all matches in original text
    for match in pattern.finditer(raw_text):
        start = match.start()
        end = match.end()
        len_diff = len(replace) - len(search)

        # Apply replacement at adjusted position
        adjusted_start = start + offset_delta
        new_text = new_text[:adjusted_start] + replace + new_text[adjusted_start + len(search):]
        replacement_count += 1

        # Adjust entity offsets and lengths
        for ent in entity_objs:
            ent_start = ent.offset
            ent_end = ent.offset + ent.length

            # Entity is completely AFTER the replacement
            if ent_start >= end:
                ent.offset += len_diff

            # Entity CONTAINS the replacement (replacement is inside entity)
            elif ent_start <= start and ent_end >= end:
                ent.length += len_diff

            # Replacement OVERLAPS entity start (partial overlap from left)
            elif start < ent_start < end:
                overlap = end - ent_start
                ent.offset = adjusted_start + len(replace)
                ent.length = max(0, ent.length - overlap)

            # Replacement OVERLAPS entity end (partial overlap from right)
            elif start < ent_end <= end:
                overlap = ent_end - start
                ent.length = max(0, ent.length - overlap)

        offset_delta += len_diff

    # Also replace in URL attributes of TextUrl entities
    for ent in entity_objs:
        if isinstance(ent, types.MessageEntityTextUrl) and ent.url:
            if case_sensitive:
                ent.url = ent.url.replace(search, replace)
            else:
                # Case-insensitive URL replacement
                ent.url = re.sub(re.escape(search), replace, ent.url, flags=re.IGNORECASE)

    # Remove zero-length entities and convert back to dicts
    new_entities = [entity_to_dict(e) for e in entity_objs if e.length > 0]

    return new_text, new_entities, replacement_count


def entities_to_markdown(raw_text, entities):
    """
    Convert raw_text + entities to markdown for display.

    Args:
        raw_text: Plain text
        entities: List of entity dicts or MessageEntity objects

    Returns:
        str: Markdown-formatted text
    """
    if not raw_text:
        return ''

    entity_objs = dicts_to_entities(entities) if entities else []

    try:
        return markdown.unparse(raw_text, entity_objs)
    except Exception:
        # Fallback to raw text if unparse fails
        return raw_text


def get_entities_from_markdown(text):
    """
    Parse markdown to get raw_text and entities.
    Used for legacy data that doesn't have entities stored.

    Args:
        text: Markdown-formatted text

    Returns:
        tuple: (raw_text, entities_as_dicts)
    """
    if not text:
        return '', []

    try:
        raw_text, entity_objs = markdown.parse(text)
        return raw_text, entities_to_dicts(entity_objs)
    except Exception:
        # If parsing fails, return text as-is with no entities
        return text, []


def get_message_entities(message_dict):
    """
    Get entities from a message dict, with fallback to parsing markdown.

    Args:
        message_dict: Message dictionary from database

    Returns:
        tuple: (raw_text, entities_as_dicts)
    """
    # If entities are stored, use them with raw_text
    if 'entities' in message_dict and message_dict['entities']:
        raw_text = message_dict.get('raw_text') or ''
        entities = message_dict['entities']
        return raw_text, entities

    # Fallback: parse from markdown text field
    text = message_dict.get('text') or ''
    if text:
        return get_entities_from_markdown(text)

    # Last resort: use raw_text with no entities
    return message_dict.get('raw_text') or '', []


def apply_replacement_to_message(message_dict, search, replace, case_sensitive=True):
    """
    Apply search-replace to a message and return updated fields.

    Args:
        message_dict: Original message dictionary
        search: Text to find
        replace: Text to replace with
        case_sensitive: Whether search is case sensitive

    Returns:
        dict: Updated fields (raw_text, entities, text) or None if no changes
    """
    raw_text, entities = get_message_entities(message_dict)

    # Handle None or empty raw_text
    if not raw_text:
        return None

    # Check if search term exists
    if case_sensitive:
        if search not in raw_text:
            # Also check URLs in entities
            has_url_match = False
            for ent in entities:
                if isinstance(ent, dict) and ent.get('url') and search in ent['url']:
                    has_url_match = True
                    break
            if not has_url_match:
                return None
    else:
        if search.lower() not in raw_text.lower():
            has_url_match = False
            for ent in entities:
                if isinstance(ent, dict) and ent.get('url') and search.lower() in ent['url'].lower():
                    has_url_match = True
                    break
            if not has_url_match:
                return None

    # Apply replacement
    new_raw, new_entities, count = search_replace_with_entities(
        raw_text, entities, search, replace, case_sensitive
    )

    if count == 0:
        # Check if only URL was changed
        old_urls = [e.get('url', '') for e in entities if isinstance(e, dict)]
        new_urls = [e.get('url', '') for e in new_entities if isinstance(e, dict)]
        if old_urls == new_urls:
            return None

    # Generate new markdown
    new_markdown = entities_to_markdown(new_raw, new_entities)

    return {
        'raw_text': new_raw,
        'entities': new_entities,
        'text': new_markdown,
        'replacement_count': count
    }
