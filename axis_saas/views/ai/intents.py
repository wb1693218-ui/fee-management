"""Small, deterministic intent recognizers for English and Roman Urdu."""

import re
import unicodedata

from django.utils import timezone

from .knowledge import PAGE_CATALOG


def normalize_message(message):
    message = unicodedata.normalize('NFKC', message or '').lower()
    message = message.replace('\u2019', "'").replace('\u2018', "'")
    return re.sub(r'\s+', ' ', message).strip()


def is_roman_urdu(message):
    normalized = normalize_message(message)
    return bool(re.search(
        r'\b(kitne|kitni|naam|kya|kaise|dikhao|batao|kholo|khol|hai|hain|he|hen|mein|mujhe|karna|chahiye|kahan|wala|wali|ke|ki)\b',
        normalized,
    ))


def _clean_name(value):
    value = re.sub(r'[?!.,;:]+$', '', value.strip())
    value = re.sub(r'\s+(?:hain|hai|hen|he|h|please|batao|dikhao)$', '', value)
    value = re.sub(r'\s+', ' ', value).strip(" \t\n\r\"'.,?!")
    return value[:80]


def extract_student_name_lookup(message):
    """Extract a requested student name, e.g. 'Sami name ke kitne students?'"""
    text = normalize_message(message)
    name_pattern = r"(?P<name>[a-z0-9][a-z0-9 .'-]{0,78}?)"
    patterns = (
        rf'{name_pattern}\s+(?:naam|name)\s+(?:(?:ke|ki|kay|ka|k)\s+)?(?:kitne|kitni|how many)\s+(?:students?|talib(?:a|aat)?)\b',
        rf'{name_pattern}\s+(?:naam|name)\s+(?:(?:ke|ki|kay|ka|k)\s+)?(?:saare|saray|sari|all|list)\s+(?:students?|talib(?:a|aat)?)\b',
        rf'\b(?:show|find|search|list|open)\s+(?:all\s+)?(?:students?|talib(?:a|aat)?)\s+(?:named|called)\s+{name_pattern}(?:\s+(?:please|profile|record|details|dikhao)\b|[?!.,]|$)',
        rf'{name_pattern}\s+(?:ka|ki|ke)\s+(?:profile|record|details)\b',
        rf'{name_pattern}\s+(?:ke|ki|kay)\s+(?:kitne|kitni)\s+(?:students?|talib(?:a|aat)?)\b',
        r'\b(?:students?|talib(?:a|aat)?)\s+(?:named|called|with name)\s+(?P<name>[a-z0-9][a-z0-9 .\'-]{0,78}?)(?:\s+(?:list|please|dikhao|batao)\b|[?!.,]|$)',
        r'\bhow many\s+(?:students?|talib(?:a|aat)?)\s+(?:named|called)\s+(?P<name>[a-z0-9][a-z0-9 .\'-]{0,78}?)(?:\s+(?:are|is|hai|hain|he|hen)\b|[?!.,]|$)',
        r'\b(?:students?|talib(?:a|aat)?)\s+(?:named|called)\s+(?P<name>[a-z0-9][a-z0-9 .\'-]{0,78}?)(?:\s+(?:name|naam|hai|hain|he|hen)\b|[?!.,]|$)',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            name = _clean_name(match.group('name'))
            if name:
                return name
    return None


def extract_student_parent_lookup(message):
    """Extract a parent/father name from a question asking which student they belong to."""
    text = normalize_message(message)
    name_pattern = r"(?P<name>[a-z0-9][a-z0-9 .'-]{1,78}?)"
    patterns = (
        rf'{name_pattern}\s+(?:kis|which)\s+(?:bache|bachay|student|child)\b',
        rf'\b(?:which|what)\s+(?:student|child)\s+(?:has|has the|belongs to)\s+(?:father|parent|guardian)\s+(?:named\s+)?{name_pattern}$',
        rf'{name_pattern}\s+(?:ke|ka)\s+(?:bache|bachay|student)\s+(?:kaun|kon|who)\b',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            name = _clean_name(match.group('name'))
            if name:
                return name
    return None


def extract_class_pending_fee(message):
    """Extract a grade and section for a current outstanding-fee question."""
    text = normalize_message(message)
    asks_pending = bool(re.search(
        r'\b(pending|remaining|outstanding|baqaya|due)\b', text,
    ))
    mentions_fee = bool(re.search(r'\b(fee|fees)\b', text))
    if not asks_pending or not mentions_fee:
        return None

    patterns = (
        r'\b(?:class|grade)\s*(?P<grade>[a-z0-9][a-z0-9 -]{0,20}?)\s+(?:section\s*)?(?P<section>[a-z])\b',
        r'\b(?P<grade>\d{1,2})\s*-?\s*(?P<section>[a-z])\b',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            grade = re.sub(r'\s+', ' ', match.group('grade')).strip(' -')
            if grade:
                return grade, match.group('section').upper()
    return None


def extract_timetable_period_question(message):
    """Return period number, weekday and whether the user explicitly said next."""
    text = normalize_message(message)
    period_match = re.search(
        r'\b(?:period|p)\s*(?P<period_a>\d{1,2})\b|'
        r'\b(?P<period_b>\d{1,2})(?:st|nd|rd|th)?\s*(?:period|p)\b',
        text,
    )
    if not period_match:
        return None
    period_number = int(period_match.group('period_a') or period_match.group('period_b'))
    if not 1 <= period_number <= 20:
        return None

    weekdays = {
        0: ('monday', 'mon', 'somvaar', 'somwar', 'peer'),
        1: ('tuesday', 'tue', 'mangal', 'mangalwar'),
        2: ('wednesday', 'wed', 'budh', 'budhwar'),
        3: ('thursday', 'thu', 'jumeraat', 'jumerat'),
        4: ('friday', 'fri', 'jumma', 'juma'),
        5: ('saturday', 'sat', 'hafta', 'haftay'),
        6: ('sunday', 'sun', 'itwaar', 'itwar'),
    }
    weekday = None
    if re.search(r'\b(today|aaj)\b', text):
        weekday = timezone.localdate().weekday()
    for day_number, aliases in weekdays.items():
        if weekday is None and any(
            re.search(rf'(?<!\w){re.escape(alias)}(?!\w)', text)
            for alias in aliases
        ):
            weekday = day_number
            break
    if weekday is None:
        return None
    asks_for_assignment = bool(re.search(
        r'\b(who|which|teacher|class|subject|kis|kaun|kon|kiska|kiski|me|mein|schedule|timetable)\b',
        text,
    ))
    if not asks_for_assignment:
        return None
    next_occurrence = bool(re.search(
        r'\b(next|coming|upcoming|aglay|agla|agli|aanay wala)\b', text,
    ))
    return period_number, weekday, next_occurrence


def extract_staff_name_lookup(message):
    text = normalize_message(message)
    name_pattern = r"(?P<name>[a-z0-9][a-z0-9 .'-]{0,78}?)"
    patterns = (
        rf'\b(?:find|search|show|list|lookup)\s+(?:the\s+)?(?:staff|teacher)\s+(?:named\s+|called\s+)?{name_pattern}(?:\s+(?:please|profile|ka|ki)\b|[?!.,]|$)',
        rf'{name_pattern}\s+(?:naam|name)\s+(?:ka|ki|ke)\s+(?:staff|teacher)\b',
        rf'{name_pattern}\s+(?:teacher|staff)\s+(?:profile|record|details)\b',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            name = _clean_name(match.group('name'))
            if name:
                return name
    return None


def extract_fee_balance_student(message):
    text = normalize_message(message)
    name_pattern = r"(?P<name>[a-z0-9][a-z0-9 .'-]{0,78}?)"
    patterns = (
        rf'{name_pattern}\s+(?:ki|ka|ke)\s+(?:(?:pending|remaining|baqaya)\s+)?fees?\b',
        rf'\b(?:pending|remaining|balance|baqaya)\s+(?:fee\s+)?(?:for|of)\s+{name_pattern}(?:[?!.,]|$)',
        rf'\b(?:fee|fees)\s+(?:for|of)\s+{name_pattern}(?:\s+(?:kitni|how much|pending|baqaya)\b|[?!.,]|$)',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            name = _clean_name(match.group('name'))
            if name:
                return name
    return None


def is_student_count_question(message):
    text = normalize_message(message)
    is_count_question = bool(re.search(
        r'\b(?:how many|count|kitne|kitni|total|number of)\b.*\b(?:students?|talib(?:a|aat)?)\b',
        text,
    ))
    asks_for_a_breakdown = bool(re.search(
        r'\b(absent|present|late|class|section|grade|wing|campus|fee|payment|name|naam|profile|record)\b',
        text,
    ))
    return is_count_question and not asks_for_a_breakdown


def is_school_data_question(message):
    """Prevent private operational questions from reaching an external LLM."""
    text = normalize_message(message)
    mentions_school_data = bool(re.search(
        r'\b(student|students|talib|taliba|fee|fees|payment|receipt|attendance|hazri|hajri|staff|teacher|class|section|leave|chutti|stock|inventory|sale|sales|revenue|timetable|schedule|period|subject|product|guardian|parent|defaulter|salary|profile|record|detail|naam|name)\w*\b',
        text,
    ))
    requests_records = bool(re.search(
        r'\b(how many|how much|count|total|number of|kitne|kitni|list|find|search|who|which|show|dikhao|pending|overdue|absent|present|late|paid|balance|marks|profile|details|record|records|collection|collected|revenue|average|sum|trend|between|last week|this week|last month|this month|last year|this year|kaun|kis)\b',
        text,
    ))
    return mentions_school_data and requests_records


def is_navigation_request(message):
    return bool(re.search(
        r'\b(open|go to|take me|navigate|mark|manage|kholo|khol|dikhao|where is|kahan)\b',
        normalize_message(message),
    ))


def is_help_question(message):
    text = normalize_message(message)
    return bool(re.search(
        r'\b(how to|how do|how can|steps|guide|guidance|help me|explain|samjhao|samjha|'
        r'kaise|kese|kis tarah|kis trha|kroo|karun|karon|batao|btao|kya karna|'
        r'kese karein|kaise karein)\b',
        text,
    ))


def is_attendance_summary_question(message):
    text = normalize_message(message)
    mentions_attendance = bool(re.search(r'\b(attendance|hazri|hajri)\b', text))
    asks_for_summary = bool(re.search(
        r'\b(today|aaj|summary|status|rate|percentage|kitni|kitne|how many|kya|kaisi|kaisa|report)\b',
        text,
    ))
    return mentions_attendance and asks_for_summary and not is_navigation_request(text)


def find_page_intent(message, pages):
    text = normalize_message(message)
    candidates = []
    enabled_by_key = {page['key']: page for page in pages}
    for page in PAGE_CATALOG:
        for alias in page['aliases']:
            normalized_alias = normalize_message(alias)
            if normalized_alias and re.search(
                rf'(?<!\w){re.escape(normalized_alias)}(?!\w)', text
            ):
                candidates.append((len(normalized_alias), page))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return enabled_by_key.get(candidates[0][1]['key'])
