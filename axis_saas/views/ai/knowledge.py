"""Feature-aware admin page knowledge for the AXIS assistant."""

import re
from difflib import SequenceMatcher
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path

from django.conf import settings
from django.urls import NoReverseMatch, get_resolver, reverse
from django.urls.resolvers import URLPattern, URLResolver


PAGE_CATALOG = (
    {
        'key': 'dashboard',
        'label': 'Dashboard',
        'route': 'dashboard',
        'features': ('dashboard',),
        'aliases': ('dashboard', 'home', 'overview', 'main page'),
        'description': 'School-wide overview and operational summaries.',
    },
    {
        'key': 'students',
        'label': 'Students',
        'route': 'student_list',
        'features': ('students',),
        'aliases': ('student', 'students', 'talib', 'taliba', 'student list'),
        'description': 'Search students and open their profiles.',
    },
    {
        'key': 'staff',
        'label': 'Staff management',
        'route': 'staff_list',
        'features': ('staff_management',),
        'aliases': ('staff', 'teacher', 'teachers', 'ustad', 'staff management'),
        'description': 'Review staff accounts, attendance and profiles.',
    },
    {
        'key': 'attendance',
        'label': 'Attendance management',
        'route': 'admin_attendance',
        'features': ('attendance_management',),
        'aliases': ('attendance', 'hazri', 'hajri', 'attendance management', 'mark attendance'),
        'description': 'Mark and review class attendance.',
    },
    {
        'key': 'attendance_report',
        'label': 'Attendance report',
        'route': 'attendance_report',
        'features': ('reports', 'attendance_management'),
        'aliases': ('attendance report', 'hazri report', 'hajri report'),
        'description': 'Compare attendance by campus, wing, class, section and student.',
    },
    {
        'key': 'reports',
        'label': 'School reports',
        'route': 'reports',
        'features': ('reports',),
        'aliases': ('report', 'reports', 'school report', 'financial report'),
        'description': 'Review finance, enrolment, staff, leave and inventory summaries.',
    },
    {
        'key': 'fees',
        'label': 'Fee collection',
        'route': 'fee_collection',
        'features': ('fee_collection',),
        'aliases': ('fee', 'fees', 'fee collection', 'collect fee', 'wasooli', 'jama'),
        'description': 'Find a student and record a fee payment.',
    },
    {
        'key': 'defaulters',
        'label': 'Fee defaulters',
        'route': 'defaulters',
        'features': ('defaulters',),
        'aliases': ('defaulter', 'defaulters', 'pending fee', 'overdue fee', 'baqaya'),
        'description': 'Review students with unpaid or partly paid fees.',
    },
    {
        'key': 'classes',
        'label': 'Classes and sections',
        'route': 'classes_management',
        'features': ('classes_management',),
        'aliases': ('class', 'classes', 'section', 'sections', 'jamaat'),
        'description': 'Browse classes, sections and class profiles.',
    },
    {
        'key': 'class_subjects',
        'label': 'Classes and subjects',
        'route': 'class_management',
        'features': ('class_management',),
        'aliases': ('class subjects', 'subjects', 'subject', 'teacher assignments', 'assign teachers'),
        'description': 'Review classes, subjects and teacher assignments.',
    },
    {
        'key': 'timetable',
        'label': 'Timetable',
        'route': 'timetable_management',
        'features': ('timetable_management',),
        'aliases': ('timetable', 'schedule', 'periods', 'period', 'waqt'),
        'description': 'Manage school calendars, periods and schedules.',
    },
    {
        'key': 'timetable_periods',
        'label': 'Timetable periods',
        'route': 'timetable_periods',
        'features': ('timetable_management',),
        'aliases': ('period setup', 'period timings', 'break timings'),
        'description': 'Configure school periods, breaks and their timings.',
    },
    {
        'key': 'timetable_assignments',
        'label': 'Timetable assignments',
        'route': 'timetable_assignments',
        'features': ('timetable_management',),
        'aliases': ('assign timetable', 'timetable assignment'),
        'description': 'Assign timetables to classes.',
    },
    {
        'key': 'timetable_teachers',
        'label': 'Teacher timetable assignments',
        'route': 'timetable_assign_teachers',
        'features': ('timetable_management',),
        'aliases': ('assign periods to teachers', 'teacher timetable', 'substitute teacher'),
        'description': 'Assign periods and teachers, and review substitute coverage.',
    },
    {
        'key': 'leave',
        'label': 'Leave management',
        'route': 'leave_management',
        'features': ('leave_management',),
        'aliases': ('leave', 'leaves', 'chutti', ' छुट्टी', 'leave management'),
        'description': 'Review and manage staff leave requests.',
    },
    {
        'key': 'stock',
        'label': 'Stock management',
        'route': 'stock_management',
        'features': ('stock_management',),
        'aliases': ('stock', 'inventory', 'product', 'products', 'maal'),
        'description': 'Review school inventory and product quantities.',
    },
    {
        'key': 'fee_structure',
        'label': 'Fee structure',
        'route': 'fee_structure',
        'features': ('fee_structure',),
        'aliases': ('fee structure', 'monthly fee', 'tuition fee'),
        'description': 'Review configured fees by grade.',
    },
    {
        'key': 'fee_settings',
        'label': 'Fee settings',
        'route': 'fee_settings',
        'features': ('fee_settings',),
        'aliases': ('fee settings', 'late fee rules', 'fee automation'),
        'description': 'Configure fee generation, due dates, and late-fee rules.',
    },
    {
        'key': 'fee_logs',
        'label': 'Fee generation logs',
        'route': 'fee_logs',
        'features': ('fee_collection',),
        'aliases': ('fee logs', 'generation logs', 'voucher generation history'),
        'description': 'Review recent fee generation activity and skipped records.',
    },
    {
        'key': 'vouchers',
        'label': 'Fee vouchers',
        'route': 'vouchers_list',
        'features': ('fee_collection',),
        'aliases': ('voucher', 'vouchers', 'fee voucher'),
        'description': 'Browse generated student fee vouchers.',
    },
    {
        'key': 'family_payment',
        'label': 'Family payment',
        'route': 'family_payment',
        'features': ('family_payment',),
        'aliases': ('family payment', 'pay for family', 'siblings payment'),
        'description': 'Collect one payment for multiple students in a family.',
    },
    {
        'key': 'settings',
        'label': 'School settings',
        'route': 'settings',
        'features': ('fee_settings',),
        'aliases': ('school settings', 'portal settings', 'school configuration'),
        'description': 'Review school branding and portal configuration.',
    },
)


class _VisibleTemplateText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden_depth += 1
            return
        if self.hidden_depth:
            return
        for name, value in attrs:
            if name in {'aria-label', 'alt', 'placeholder', 'title'} and value:
                self.parts.append(value)

    def handle_endtag(self, tag):
        if tag in {'script', 'style'} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)


def available_pages(tenant, schema_name):
    """Return only links that this tenant is allowed to access."""
    pages = []
    known_routes = {page['route'] for page in PAGE_CATALOG}
    for page in PAGE_CATALOG:
        if not all(tenant.is_feature_enabled(feature, 'desktop') for feature in page['features']):
            continue
        try:
            url = reverse(page['route'], kwargs={'schema_name': schema_name})
        except NoReverseMatch:
            continue
        pages.append({**page, 'url': url})

    def walk(patterns, prefix=''):
        for pattern in patterns:
            route = prefix + str(getattr(pattern.pattern, '_route', ''))
            if isinstance(pattern, URLResolver):
                yield from walk(pattern.url_patterns, route)
            elif isinstance(pattern, URLPattern):
                yield pattern, route

    for pattern, route in walk(get_resolver().url_patterns):
        route_name = pattern.name
        if not route_name or route_name in known_routes:
            continue
        if not route.startswith('portal/<slug:schema_name>/'):
            continue
        remainder = route.removeprefix('portal/<slug:schema_name>/')
        if '<' in remainder or any(
            part in route_name.lower()
            for part in ('api', 'mobile', 'login', 'logout', 'delete', 'toggle', 'reset', 'submit')
        ):
            continue

        required_features = set()
        callback = pattern.callback
        seen_callbacks = set()
        while callback and id(callback) not in seen_callbacks:
            seen_callbacks.add(id(callback))
            required_features.update(getattr(callback, 'required_school_features', ()))
            callback = getattr(callback, '__wrapped__', None)
        if not required_features or not all(
            tenant.is_feature_enabled(feature, 'desktop')
            for feature in required_features
        ):
            continue
        try:
            url = reverse(route_name, kwargs={'schema_name': schema_name})
        except NoReverseMatch:
            continue
        label = re.sub(r'[_-]+', ' ', route_name.removesuffix('_view')).strip().title()
        path_alias = re.sub(r'[-/]+', ' ', remainder).strip()
        pages.append({
            'key': f'route:{route_name}',
            'label': label,
            'route': route_name,
            'features': tuple(sorted(required_features)),
            'aliases': (label.lower(), path_alias),
            'description': f'{label} page in the school admin panel.',
            'url': url,
        })
        known_routes.add(route_name)
    return pages


def _normalize(text):
    return re.sub(r'[^a-z0-9]+', ' ', (text or '').lower()).strip()


def fuzzy_page_intent(message, pages):
    """Resolve misspelled page names without ever returning a disabled page."""
    text = _normalize(message)
    if not text:
        return None
    words = text.split()
    enabled_by_key = {page['key']: page for page in pages}
    ranked = []
    for page in PAGE_CATALOG:
        for alias in (page['label'], *page['aliases']):
            normalized_alias = _normalize(alias)
            if not normalized_alias:
                continue
            if re.search(rf'(?<!\w){re.escape(normalized_alias)}(?!\w)', text):
                return enabled_by_key.get(page['key'])
            alias_words = normalized_alias.split()
            if len(alias_words) == 1:
                matches = [
                    SequenceMatcher(None, word, alias_words[0]).ratio()
                    for word in words
                    if len(word) >= 5 and len(alias_words[0]) >= 5
                ]
                score = max(matches, default=0)
            else:
                score = SequenceMatcher(None, normalized_alias, text).ratio()
            if score >= 0.82:
                ranked.append((score, page))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return enabled_by_key.get(ranked[0][1]['key'])


@lru_cache(maxsize=1)
def _documentation_sections():
    """Load docs once per process; new Markdown files are picked up on restart."""
    root = Path(settings.BASE_DIR)
    documents = []
    for folder in ('docs', 'documentation'):
        for path in (root / folder).rglob('*.md'):
            try:
                content = path.read_text(encoding='utf-8')
            except OSError:
                continue
            heading = path.stem.replace('-', ' ').title()
            current_heading = heading
            lines = []
            for line in content.splitlines():
                if line.startswith('#'):
                    if lines:
                        text = '\n'.join(lines).strip()
                        if text:
                            documents.append({
                                'title': current_heading,
                                'source': f'{folder}/{path.relative_to(root / folder)}',
                                'text': text[:2200],
                            })
                    current_heading = line.lstrip('#').strip() or heading
                    lines = [line]
                elif line.strip():
                    lines.append(line)
            if lines:
                text = '\n'.join(lines).strip()
                if text:
                    documents.append({
                        'title': current_heading,
                        'source': f'{folder}/{path.relative_to(root / folder)}',
                        'text': text[:2200],
                    })
    template_root = root / 'templates' / 'tenant'
    for path in template_root.rglob('*.html'):
        try:
            content = path.read_text(encoding='utf-8')
        except OSError:
            continue
        content = re.sub(
            r'{%\s*comment\s*%}.*?{%\s*endcomment\s*%}',
            ' ', content, flags=re.DOTALL,
        )
        content = re.sub(r'{#.*?#}|{%.*?%}|{{.*?}}', ' ', content, flags=re.DOTALL)
        parser = _VisibleTemplateText()
        try:
            parser.feed(content)
        except Exception:
            continue
        text = re.sub(r'\s+', ' ', ' '.join(parser.parts)).strip()
        if not text:
            continue
        source = f'templates/tenant/{path.relative_to(template_root)}'
        title = f"Admin page: {path.relative_to(template_root).with_suffix('').as_posix().replace('/', ' ').replace('_', ' ').title()}"
        chunk_size = 1800
        for chunk_index, start in enumerate(range(0, len(text), chunk_size), start=1):
            documents.append({
                'title': f'{title} · part {chunk_index}',
                'source': source,
                'text': text[start:start + chunk_size],
            })
    return tuple(documents)


def retrieve_documentation(message, limit=4):
    """Small lexical retrieval over current docs, independent of a model SDK."""
    terms = set(_normalize(message).split())
    terms = {term for term in terms if len(term) > 2}
    if not terms:
        return []
    if terms & {'stock', 'inventory', 'maal', 'product', 'products'}:
        terms.update({'inventory', 'product', 'category', 'quantity', 'add', 'update'})
    if terms & {'kaise', 'kese', 'how', 'karun', 'karo', 'kroo', 'batao', 'btao', 'guide', 'guidance', 'samjhao'}:
        terms.update({'add', 'create', 'update', 'steps', 'manage', 'use', 'guide'})
    if terms & {'attendance', 'hazri', 'hajri'}:
        terms.update({'attendance', 'report', 'mark', 'student', 'absent', 'present'})
    if terms & {'fee', 'fees', 'payment', 'payments', 'wasooli', 'jama'}:
        terms.update({'fee', 'collection', 'voucher', 'receipt', 'payment', 'balance'})
    if terms & {'student', 'students', 'bacha', 'bache', 'talib'}:
        terms.update({'student', 'admission', 'profile', 'records', 'class'})
    if terms & {'teacher', 'teachers', 'staff', 'ustad'}:
        terms.update({'staff', 'teacher', 'profile', 'attendance', 'leave'})
    if terms & {'timetable', 'schedule', 'period', 'periods', 'waqt'}:
        terms.update({'timetable', 'schedule', 'period', 'assign', 'class', 'teacher'})
    scored = []
    for section in _documentation_sections():
        content = _normalize(f"{section['title']} {section['text']}")
        content_terms = set(content.split())
        overlap = len(terms & content_terms)
        if not overlap:
            continue
        score = overlap / (len(terms) ** 0.5)
        scored.append((score, section))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [section for _score, section in scored[:limit]]
