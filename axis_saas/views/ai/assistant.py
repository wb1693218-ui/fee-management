"""Authenticated assistant endpoint for a single school tenant."""

import json
import logging
import re
import uuid

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from ..helpers import get_tenant, require_tenant_type
from .intents import (
    extract_student_name_lookup,
    extract_student_parent_lookup,
    extract_class_pending_fee,
    extract_timetable_period_question,
    extract_staff_name_lookup,
    extract_fee_balance_student,
    find_page_intent,
    is_attendance_summary_question,
    is_help_question,
    is_roman_urdu,
    is_navigation_request,
    is_school_data_question,
    is_student_count_question,
)
from .knowledge import available_pages, fuzzy_page_intent, retrieve_documentation
from .providers import run_assistant_model_turn
from .registry import enabled_tool_definitions
from .tools import (
    attendance_today_summary,
    class_pending_fee_summary,
    count_students,
    lookup_staff,
    lookup_students,
    lookup_students_by_parent_name,
    student_fee_balance,
    timetable_period_summary,
)

logger = logging.getLogger(__name__)


def _rate_limited(request, schema_name):
    session_key = request.session.session_key or 'no-session'
    key = f'ai_assistant_rate:{schema_name}:{session_key}'
    try:
        if cache.add(key, 1, timeout=60):
            return False
        return cache.incr(key) > 30
    except Exception as exc:
        # Do not make an external-model endpoint unmetered during cache outages.
        logger.warning('AI assistant rate-limit cache failed: %s', type(exc).__name__)
        return True


def _current_page(path, pages):
    for page in pages:
        if path == page['url'] or path.startswith(page['url'].rstrip('/') + '/'):
            return page['label']
    return 'AXIS school admin'


def _remember_turn(request, history_key, message, reply, sharing_allowed):
    if not sharing_allowed:
        return
    history = request.session.get(history_key, [])
    history.extend((
        {'role': 'user', 'content': message[:500]},
        {'role': 'assistant', 'content': str(reply or '')[:1000]},
    ))
    request.session[history_key] = history[-16:]
    request.session.modified = True


def _conversation_id(value):
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError):
        return None
    return parsed if parsed == value.lower() else None


@require_POST
@require_tenant_type(['school'])
def assistant_api(request, schema_name):
    if _rate_limited(request, schema_name):
        return JsonResponse({'error': 'Please wait a moment before sending another question.'}, status=429)

    try:
        payload = json.loads(request.body or b'{}')
    except (TypeError, ValueError):
        return JsonResponse({'error': 'Invalid request body.'}, status=400)
    if isinstance(payload, dict) and payload.get('action') == 'end_chat':
        conversation_id = _conversation_id(payload.get('conversation_id'))
        consent_key = f'ai_assistant_consent:{schema_name}'
        if conversation_id and request.session.get(consent_key) == conversation_id:
            request.session.pop(consent_key, None)
            request.session.pop(
                f'ai_assistant_history:{schema_name}:{conversation_id}', None,
            )
        return JsonResponse({'ended': True})

    message = payload.get('message', '') if isinstance(payload, dict) else ''
    if not isinstance(message, str):
        return JsonResponse({'error': 'Message must be text.'}, status=400)
    message = re.sub(r'\s+', ' ', message).strip()
    if not message or len(message) > 500:
        return JsonResponse({'error': 'Write a question up to 500 characters.'}, status=400)

    roman_urdu = is_roman_urdu(message)
    tenant = getattr(request, 'tenant', None) or get_tenant(request, schema_name)
    if not tenant.is_feature_enabled('ai_assistant', 'desktop'):
        return JsonResponse({'error': 'AI assistant is not enabled for this school.'}, status=404)

    conversation_id = _conversation_id(payload.get('conversation_id')) if isinstance(payload, dict) else None
    consent_key = f'ai_assistant_consent:{schema_name}'
    provider_ready = bool(
        getattr(settings, 'AI_ASSISTANT_API_KEY', '')
        and getattr(settings, 'AI_ASSISTANT_MODEL', '')
    )
    platform_data_sharing_enabled = bool(
        getattr(settings, 'AI_ASSISTANT_ALLOW_SCHOOL_DATA_TO_PROVIDER', False)
        and provider_ready
    )
    history_key = (
        f'ai_assistant_history:{schema_name}:{conversation_id}'
        if conversation_id else f'ai_assistant_history:{schema_name}'
    )
    if (
        conversation_id
        and payload.get('confirm_school_data_sharing') is True
        and platform_data_sharing_enabled
        and request.session.get(consent_key) != conversation_id
    ):
        request.session[consent_key] = conversation_id
        for session_key in list(request.session.keys()):
            if session_key.startswith(f'ai_assistant_history:{schema_name}:'):
                request.session.pop(session_key, None)
        request.session[history_key] = []
        request.session.modified = True
    elif conversation_id and payload.get('confirm_school_data_sharing') is False:
        request.session.pop(consent_key, None)
        for session_key in list(request.session.keys()):
            if session_key.startswith(f'ai_assistant_history:{schema_name}:'):
                request.session.pop(session_key, None)
    if not platform_data_sharing_enabled:
        request.session.pop(consent_key, None)
        for session_key in list(request.session.keys()):
            if session_key.startswith(f'ai_assistant_history:{schema_name}:'):
                request.session.pop(session_key, None)
    allow_provider_data = bool(
        platform_data_sharing_enabled
        and conversation_id
        and request.session.get(consent_key) == conversation_id
    )
    if not allow_provider_data:
        request.session.pop(history_key, None)

    pages = available_pages(tenant, schema_name)
    requested_path = payload.get('current_path', '') if isinstance(payload, dict) else ''
    if not isinstance(requested_path, str) or len(requested_path) > 300:
        requested_path = ''
    current_page = _current_page(requested_path, pages) if requested_path else 'AXIS school admin'
    if current_page == 'AXIS school admin':
        current_page = _current_page(request.path, pages)
    page = find_page_intent(message, pages)
    if page is None:
        page = fuzzy_page_intent(message, pages)

    if is_help_question(message) and (not provider_ready or not allow_provider_data):
        documentation = retrieve_documentation(
            f'{message} {current_page}',
            limit=2,
        )
        if documentation:
            sections = []
            for section in documentation:
                excerpt = re.sub(r'(?m)^#{1,6}\s*', '', section['text']).strip()
                excerpt = re.sub(r'\n{3,}', '\n\n', excerpt)
                sections.append(f"{section['title']}\n{excerpt[:900]}")
            actions = []
            if page:
                actions.append({
                    'label': page['label'],
                    'detail': page['description'],
                    'url': page['url'],
                })
            return JsonResponse({
                'kind': 'documentation',
                'reply': '\n\n'.join(sections)[:2000],
                'actions': actions,
                'provider_configured': provider_ready,
                'sources': [section['source'] for section in documentation],
            })

    student_name = extract_student_name_lookup(message)
    if student_name:
        if not tenant.is_feature_enabled('students', 'desktop'):
            return JsonResponse({'kind': 'local_only', 'reply': 'Students module is not enabled for this school.', 'actions': []})
        result = lookup_students(schema_name, student_name, roman_urdu=roman_urdu)
        result['kind'] = 'student_lookup'
        _remember_turn(request, history_key, message, result['reply'], allow_provider_data)
        return JsonResponse(result)

    parent_name = extract_student_parent_lookup(message)
    if parent_name:
        if not tenant.is_feature_enabled('students', 'desktop'):
            return JsonResponse({'kind': 'local_only', 'reply': 'Students module is not enabled for this school.', 'actions': []})
        result = lookup_students_by_parent_name(
            schema_name, parent_name, roman_urdu=roman_urdu,
        )
        result['kind'] = 'student_lookup'
        _remember_turn(request, history_key, message, result['reply'], allow_provider_data)
        return JsonResponse(result)

    class_fee_question = extract_class_pending_fee(message)
    if class_fee_question:
        has_fee_access = any(
            tenant.is_feature_enabled(feature, 'desktop')
            for feature in ('fee_collection', 'defaulters', 'reports')
        )
        if not has_fee_access or not tenant.is_feature_enabled('students', 'desktop'):
            return JsonResponse({'kind': 'local_only', 'reply': 'Fee/student records are not enabled for this school.', 'actions': []})
        result = class_pending_fee_summary(
            schema_name,
            class_fee_question[0],
            class_fee_question[1],
            roman_urdu=roman_urdu,
        )
        result['kind'] = 'fee_summary'
        _remember_turn(request, history_key, message, result['reply'], allow_provider_data)
        return JsonResponse(result)

    if is_student_count_question(message):
        if not tenant.is_feature_enabled('students', 'desktop'):
            return JsonResponse({'kind': 'local_only', 'reply': 'Students module is not enabled for this school.', 'actions': []})
        result = count_students(schema_name, roman_urdu=roman_urdu)
        result['kind'] = 'student_count'
        _remember_turn(request, history_key, message, result['reply'], allow_provider_data)
        return JsonResponse(result)

    staff_name = extract_staff_name_lookup(message)
    if staff_name:
        if not tenant.is_feature_enabled('staff_management', 'desktop'):
            return JsonResponse({'kind': 'local_only', 'reply': 'Staff management is not enabled for this school.', 'actions': []})
        result = lookup_staff(schema_name, staff_name, roman_urdu=roman_urdu)
        result['kind'] = 'staff_lookup'
        _remember_turn(request, history_key, message, result['reply'], allow_provider_data)
        return JsonResponse(result)

    fee_student_name = extract_fee_balance_student(message)
    if fee_student_name:
        has_fee_access = any(tenant.is_feature_enabled(feature, 'desktop') for feature in ('fee_collection', 'defaulters', 'reports'))
        if not has_fee_access or not tenant.is_feature_enabled('students', 'desktop'):
            return JsonResponse({'kind': 'local_only', 'reply': 'Fee/student records are not enabled for this school.', 'actions': []})
        result = student_fee_balance(schema_name, fee_student_name, roman_urdu=roman_urdu)
        result['kind'] = 'fee_balance'
        _remember_turn(request, history_key, message, result['reply'], allow_provider_data)
        return JsonResponse(result)

    if is_attendance_summary_question(message):
        if not tenant.is_feature_enabled('attendance_management', 'desktop'):
            return JsonResponse({'kind': 'local_only', 'reply': 'Attendance management is not enabled for this school.', 'actions': []})
        attendance_page = next((page for page in pages if page['key'] == 'attendance_report'), None)
        attendance_url = attendance_page['url'] if attendance_page else None
        result = attendance_today_summary(
            schema_name,
            roman_urdu=roman_urdu,
            report_url=attendance_url,
        )
        result['kind'] = 'attendance_summary'
        _remember_turn(request, history_key, message, result['reply'], allow_provider_data)
        return JsonResponse(result)

    timetable_question = extract_timetable_period_question(message)
    if timetable_question:
        if not tenant.is_feature_enabled('timetable_management', 'desktop'):
            return JsonResponse({
                'kind': 'local_only',
                'reply': 'Time-table management is not enabled for this school.',
                'actions': [],
            })
        result = timetable_period_summary(
            schema_name,
            timetable_question[0],
            timetable_question[1],
            next_occurrence=timetable_question[2],
            roman_urdu=roman_urdu,
        )
        result['kind'] = 'timetable_lookup'
        _remember_turn(request, history_key, message, result['reply'], allow_provider_data)
        return JsonResponse(result)

    if page and is_navigation_request(message):
        if roman_urdu:
            reply = f"{page['label']} ka page yahan khol sakte hain."
        else:
            reply = f"{page['description']} Open this page here."
        return JsonResponse({
            'kind': 'navigation',
            'reply': reply,
            'actions': [{'label': page['label'], 'detail': page['description'], 'url': page['url']}],
        })

    if is_school_data_question(message) and not allow_provider_data:
        if roman_urdu:
            reply = (
                'Is sawal ka verified jawab abhi local lookup se available nahin. '
                'Broader school-data sawalon ke liye platform provider setup aur platform data-sharing policy enabled honi chahiye; har chat ke start par admin ki confirmation bhi zaroori hai. '
                'Student/guardian lookup, class-section pending fee, aaj ki attendance aur school how-to guides local available hain.'
            )
        else:
            reply = (
                'A verified local lookup is not available for this question yet. '
                'Broader school-data questions require a configured provider and the platform data-sharing policy; the admin must also confirm at the start of each chat. '
                'Student/guardian lookup, class-section pending fees, today’s attendance, and school how-to guides are available locally.'
            )
        return JsonResponse({'kind': 'local_only', 'reply': reply, 'actions': []})

    if allow_provider_data:
        history = request.session.get(history_key, [])
    else:
        history = []
        request.session.pop(history_key, None)
    turn = run_assistant_model_turn(
        message,
        current_page,
        pages,
        tenant=tenant,
        schema_name=schema_name,
        roman_urdu=roman_urdu,
        history=history,
        provider_data_consent=allow_provider_data,
    )
    if turn.get('provider_status') == 'unconfigured':
        documentation = retrieve_documentation(
            f'{message} {current_page}', limit=2,
        ) if is_help_question(message) else []
        if documentation:
            section = documentation[0]
            excerpt = re.sub(r'(?m)^#{1,6}\s*', '', section['text']).strip()
            excerpt = re.sub(r'\n{3,}', '\n\n', excerpt)
            reply = f"{section['title']}\n{excerpt[:1200]}"
            actions = []
            if page:
                actions.append({
                    'label': page['label'],
                    'detail': page['description'],
                    'url': page['url'],
                })
            return JsonResponse({
                'kind': 'documentation',
                'reply': reply,
                'actions': actions,
                'provider_configured': False,
                'source': section['source'],
            })
    if turn.get('reply') or turn.get('actions'):
        if turn.get('reply'):
            _remember_turn(request, history_key, message, turn['reply'], allow_provider_data)
        return JsonResponse({
            'kind': 'help',
            'reply': turn.get('reply') or '',
            'actions': turn.get('actions', []),
            'tools_used': turn.get('tools_used', []),
            'provider_status': turn.get('provider_status', 'ready'),
        })

    if turn.get('provider_status') == 'unavailable':
        reply = (
            'AI provider is temporarily unavailable. Please try again shortly.'
            if not roman_urdu else
            'AI provider filhal available nahin. Thori dair baad dobara koshish karein.'
        )
        return JsonResponse({
            'kind': 'help',
            'reply': reply,
            'actions': [],
            'provider_configured': True,
            'provider_status': 'unavailable',
            'available_tools': len(enabled_tool_definitions(
                tenant, consent_confirmed=allow_provider_data,
            )),
        }, status=503)

    if roman_urdu:
        reply = 'Is sawal ke liye general AI provider configure nahin hai. Main students dhoond sakta hoon ya enabled admin pages kholne ke links de sakta hoon.'
    else:
        reply = 'General AI is not configured yet. I can search students locally or link to enabled admin pages.'
    return JsonResponse({
        'kind': 'help',
        'reply': reply,
        'actions': [],
        'provider_configured': False,
        'provider_status': 'unconfigured',
        'available_tools': len(enabled_tool_definitions(
            tenant, consent_confirmed=allow_provider_data,
        )),
    })
