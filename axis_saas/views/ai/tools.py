"""Read-only, tenant-scoped tools available to the admin assistant."""

from difflib import get_close_matches
from datetime import timedelta
import re
import unicodedata

from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils.http import urlencode
from django.utils import timezone
from django_tenants.utils import schema_context

from ...models import (
    LeaveRequest,
    PaymentTransaction,
    Product,
    SchoolClass,
    Staff,
    StaffAttendance,
    SubstituteAssignment,
    Student,
    StudentAttendance,
    TimetableEntry,
    PeriodTeacherAssignment,
)
from ..helpers import aggregate_pending_totals, get_student_pending_queryset


def _normalized_name(value):
    value = unicodedata.normalize('NFKD', value or '').encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', ' ', value.lower()).strip()


def _fuzzy_student_names(schema_name, query, limit):
    """Use a bounded same-schema candidate set only when exact search is empty."""
    normalized_query = _normalized_name(query)
    if len(normalized_query) < 4:
        return []
    with schema_context(schema_name):
        names = list(
            Student.objects.filter(status='active')
            .exclude(name='')
            .values_list('name', flat=True)
            .distinct()[:3000]
        )
    normalized_to_names = {}
    for candidate in names:
        normalized_to_names.setdefault(_normalized_name(candidate), []).append(candidate)
    close = get_close_matches(normalized_query, list(normalized_to_names), n=limit, cutoff=0.72)
    return [name for match in close for name in normalized_to_names[match]]


def lookup_students(schema_name, name, roman_urdu=True, limit=8):
    """Search only this tenant's student table and return profile links."""
    name = (name or '').strip()[:80]
    limit = max(1, min(int(limit), 10))
    if len(name) < 2:
        return {
            'reply': 'Student ka naam thora aur specific likhein.' if roman_urdu else 'Please enter at least two characters of the student name.',
            'actions': [],
        }

    with schema_context(schema_name):
        matches = Student.objects.filter(
            Q(name__icontains=name) | Q(roll_number__iexact=name)
        ).order_by('name', 'roll_number')
        total = matches.count()
        students = list(matches[:limit])
        if total == 0:
            fuzzy_names = _fuzzy_student_names(schema_name, name, limit)
            if fuzzy_names:
                fuzzy_matches = Student.objects.filter(
                    status='active', name__in=fuzzy_names
                ).order_by('name', 'roll_number')
                total = fuzzy_matches.count()
                students = list(fuzzy_matches[:limit])
        actions = [{
            'label': f'{student.name} · {student.roll_number}',
            'detail': f'{student.grade} · {student.section}',
            'url': reverse('student_profile', kwargs={
                'schema_name': schema_name,
                'student_id': student.pk,
            }),
        } for student in students]

    if total > len(students):
        student_list_url = reverse('student_list', kwargs={'schema_name': schema_name})
        actions.append({
            'label': 'Baaki results dekhein' if roman_urdu else 'View all matching students',
            'detail': f'{total} total matches',
            'url': f'{student_list_url}?{urlencode({"q": name})}',
        })

    if roman_urdu:
        reply = f'{name} naam se {total} student' + (' mile.' if total == 1 else 's mile.')
    else:
        reply = f'Found {total} student' + (' matching' if total == 1 else 's matching') + f' “{name}”.'
    return {'reply': reply, 'actions': actions}


def lookup_students_by_parent_name(schema_name, parent_name, roman_urdu=True, limit=8):
    parent_name = (parent_name or '').strip()[:80]
    limit = max(1, min(int(limit), 10))
    if len(parent_name) < 2:
        return {
            'reply': 'Walid/guardian ka naam thora aur specific likhein.' if roman_urdu else 'Please enter at least two characters of the parent or guardian name.',
            'actions': [],
        }
    with schema_context(schema_name):
        matches = Student.objects.filter(
            father_name__icontains=parent_name,
        ).order_by('name', 'roll_number')
        total = matches.count()
        students = list(matches[:limit])
    actions = [{
        'label': f'{student.name} · {student.roll_number}',
        'detail': f'{student.grade} · {student.section}',
        'url': reverse('student_profile', kwargs={
            'schema_name': schema_name,
            'student_id': student.pk,
        }),
    } for student in students]
    if roman_urdu:
        reply = f'{parent_name} ke naam se {total} student record(s) mile.'
    else:
        reply = f'Found {total} student record(s) with parent/guardian name matching “{parent_name}”.'
    return {'reply': reply, 'actions': actions}


def count_students(schema_name, roman_urdu=True, grade='', section='', status=''):
    if status and status not in {'active', 'suspended', 'graduated'}:
        raise ValueError('Unknown student status.')
    filters = {}
    if grade:
        filters['grade__iexact'] = grade
    if section:
        filters['section__iexact'] = section
    if status:
        filters['status'] = status
    with schema_context(schema_name):
        students = Student.objects.filter(**filters)
        counts = {
            row['status']: row['count']
            for row in students.values('status').annotate(count=Count('pk'))
        }
    total = sum(counts.values())
    active = counts.get('active', 0)
    scope = ' '.join(part for part in (
        f'grade {grade}' if grade else '',
        f'section {section}' if section else '',
        f'{status} status' if status else '',
    ) if part)
    scope_text = f' ({scope})' if scope else ''
    if roman_urdu:
        reply = f'School mein{scope_text} kul {total} students hain; in mein se {active} active hain.'
    else:
        reply = f'The school has {total} students{scope_text}, including {active} active students.'
    return {
        'reply': reply,
        'actions': [{
            'label': 'Students list',
            'detail': 'School ke student records dekhein.' if roman_urdu else 'Browse student records.',
            'url': reverse('student_list', kwargs={'schema_name': schema_name}),
        }],
    }


def class_pending_fee_summary(schema_name, grade, section, roman_urdu=True):
    grade = (grade or '').strip()[:50]
    section = (section or '').strip()[:50]
    grade_names = (grade, f'Class {grade}', f'Grade {grade}')
    with schema_context(schema_name):
        students = Student.objects.filter(section__iexact=section).filter(
            Q(grade__iexact=grade)
            | Q(school_class__name__iexact=grade_names[0])
            | Q(school_class__name__iexact=grade_names[1])
            | Q(school_class__name__iexact=grade_names[2])
        )
        balances = get_student_pending_queryset(students).filter(pending_amount__gt=0)
        totals = balances.aggregate(
            students=Count('pk'),
            pending=Sum('pending_amount'),
        )
    pending = totals['pending'] or 0
    if roman_urdu:
        reply = (
            f'{grade} section {section} ka current total pending fee {pending:,.2f} hai; '
            f'{totals["students"]} students ka balance baqi hai.'
        )
    else:
        reply = (
            f'Current total pending fees for grade {grade}, section {section}: {pending:,.2f} '
            f'across {totals["students"]} students with a remaining balance.'
        )
    return {'reply': reply, 'actions': []}


def fee_collection_summary(schema_name, start_date, end_date, roman_urdu=True):
    with schema_context(schema_name):
        payments = PaymentTransaction.objects.filter(
            payment_date__range=(start_date, end_date),
        )
        collected = payments.aggregate(
            total=Sum('amount'),
            transactions=Count('pk'),
            cash=Sum('amount', filter=Q(payment_mode='cash')),
            bank_transfer=Sum('amount', filter=Q(payment_mode='bank_transfer')),
            cheque=Sum('amount', filter=Q(payment_mode='cheque')),
            online=Sum('amount', filter=Q(payment_mode='online')),
        )
        current_pending = aggregate_pending_totals()['total_pending']
        active_defaulters = get_student_pending_queryset(
            Student.objects.filter(status='active')
        ).filter(pending_amount__gt=0).count()
    total = collected['total'] or 0
    mode_totals = [
        f'{mode.replace("_", " ")}: {amount:,.2f}'
        for mode in ('cash', 'bank_transfer', 'cheque', 'online')
        if (amount := collected[mode])
    ]
    modes = '; '.join(mode_totals) or ('koi payment record nahin' if roman_urdu else 'no recorded payments')
    if roman_urdu:
        reply = (
            f'{start_date.isoformat()} se {end_date.isoformat()} tak {collected["transactions"]} payments mein '
            f'{total:,.2f} collect hue ({modes}). Aaj ka current outstanding balance '
            f'{current_pending:,.2f} hai; {active_defaulters} active students ka balance baqi hai.'
        )
    else:
        reply = (
            f'{collected["transactions"]} payments collected {total:,.2f} from '
            f'{start_date.isoformat()} through {end_date.isoformat()} ({modes}). '
            f'The current school-wide outstanding balance is {current_pending:,.2f}; '
            f'{active_defaulters} active students have a remaining balance.'
        )
    return {'reply': reply, 'actions': []}


def staff_attendance_summary(schema_name, start_date, end_date, roman_urdu=True):
    with schema_context(schema_name):
        totals = StaffAttendance.objects.filter(
            date__range=(start_date, end_date),
        ).aggregate(
            marked=Count('pk'),
            present=Count('pk', filter=Q(status='present')),
            late=Count('pk', filter=Q(status='late')),
            half_day=Count('pk', filter=Q(status='half_day')),
            absent=Count('pk', filter=Q(status='absent')),
            on_leave=Count('pk', filter=Q(status='on_leave')),
            holiday=Count('pk', filter=Q(status='holiday')),
            weekend=Count('pk', filter=Q(status='weekend')),
        )
    eligible = totals['present'] + totals['late'] + totals['half_day'] + totals['absent']
    rate = round((totals['present'] + totals['late'] + totals['half_day']) * 100 / eligible, 1) if eligible else 0
    if roman_urdu:
        reply = (
            f'{start_date.isoformat()} se {end_date.isoformat()} tak staff attendance ke '
            f'{totals["marked"]} records: {totals["present"]} present, {totals["late"]} late, '
            f'{totals["half_day"]} half-day, {totals["absent"]} absent, {totals["on_leave"]} leave par. '
            f'Recorded working-day attendance rate {rate}%; holidays aur weekends denominator mein shamil nahin.'
        )
    else:
        reply = (
            f'{totals["marked"]} staff attendance records from {start_date.isoformat()} through '
            f'{end_date.isoformat()}: {totals["present"]} present, {totals["late"]} late, '
            f'{totals["half_day"]} half-day, {totals["absent"]} absent, {totals["on_leave"]} on leave. '
            f'Recorded working-day attendance rate: {rate}%; holidays and weekends are excluded from the denominator.'
        )
    return {'reply': reply, 'actions': []}


def leave_request_summary(schema_name, start_date, end_date, roman_urdu=True):
    with schema_context(schema_name):
        status_counts = {
            row['status']: row['count']
            for row in LeaveRequest.objects.filter(
                created_at__date__range=(start_date, end_date),
            ).values('status').annotate(count=Count('pk'))
        }
    total = sum(status_counts.values())
    counts = ', '.join(
        f'{status}: {status_counts.get(status, 0)}'
        for status in ('pending', 'approved', 'rejected', 'cancelled')
    )
    if roman_urdu:
        reply = (
            f'{start_date.isoformat()} se {end_date.isoformat()} ke darmiyan '
            f'{total} staff leave requests submit hui: {counts}. Yeh submission date ke mutabiq hai.'
        )
    else:
        reply = (
            f'{total} staff leave requests were submitted from {start_date.isoformat()} through '
            f'{end_date.isoformat()}: {counts}. This is based on request creation date.'
        )
    return {'reply': reply, 'actions': []}


def lookup_staff(schema_name, name, roman_urdu=True, limit=8):
    name = (name or '').strip()[:80]
    limit = max(1, min(int(limit), 10))
    with schema_context(schema_name):
        matches = Staff.objects.filter(status='active').filter(
            Q(full_name__icontains=name) | Q(staff_id__iexact=name)
        ).order_by('full_name')
        total = matches.count()
        staff_members = list(matches[:limit])
    actions = [{
        'label': member.full_name,
        'detail': f'{member.job_title} · {member.staff_id}',
        'url': reverse('staff_profile', kwargs={
            'schema_name': schema_name,
            'staff_id': member.pk,
        }),
    } for member in staff_members]
    if roman_urdu:
        reply = f'{name} se match karte hue {total} active staff mile.'
    else:
        reply = f'Found {total} active staff member(s) matching “{name}”.'
    return {'reply': reply, 'actions': actions}


def student_fee_balance(schema_name, name, roman_urdu=True, limit=8):
    name = (name or '').strip()[:80]
    limit = max(1, min(int(limit), 10))
    with schema_context(schema_name):
        students = get_student_pending_queryset(
            Student.objects.filter(
                Q(name__icontains=name) | Q(roll_number__iexact=name)
            ).order_by('name')
        )
        matches = list(students[:limit])
        total = students.count()
    actions = [{
        'label': f'{student.name} · {student.roll_number}',
        'detail': f'{student.pending_amount:,.2f} pending · {student.grade} · {student.section}',
        'url': reverse('student_profile', kwargs={
            'schema_name': schema_name,
            'student_id': student.pk,
        }),
    } for student in matches]
    if roman_urdu:
        reply = f'{name} ke {total} matching student record(s) mile. Profile links par current pending balance diya hai.'
    else:
        reply = f'Found {total} matching student record(s). Current pending balances are shown on their profile links.'
    return {'reply': reply, 'actions': actions}


def attendance_today_summary(schema_name, roman_urdu=True, report_url=None):
    today = timezone.localdate()
    with schema_context(schema_name):
        records = StudentAttendance.objects.filter(
            date=today,
            period_order__isnull=True,
        )
        totals = {
            'marked': records.count(),
            'present': records.filter(status='present').count(),
            'absent': records.filter(status='absent').count(),
            'late': records.filter(status='late').count(),
            'half_day': records.filter(status='half_day').count(),
            'excused': records.filter(status='excused').count(),
        }
        totals['active_students'] = Student.objects.filter(status='active').count()
    eligible = totals['present'] + totals['absent'] + totals['late'] + totals['half_day']
    rate = round((totals['present'] + totals['late'] + totals['half_day']) * 100 / eligible, 1) if eligible else 0
    if roman_urdu:
        reply = (
            f'Aaj full-day attendance: {totals["marked"]} marks, {totals["present"]} present, '
            f'{totals["absent"]} absent, {totals["late"]} late; recorded attendance rate {rate}%. '
            f'{totals["active_students"]} active students hain.'
        )
    else:
        reply = (
            f"Today's full-day attendance: {totals['marked']} marks, {totals['present']} present, "
            f"{totals['absent']} absent, {totals['late']} late; recorded attendance rate {rate}%. "
            f"There are {totals['active_students']} active students."
        )
    actions = []
    if report_url:
        actions.append({
            'label': 'Attendance report',
            'detail': today.isoformat(),
            'url': report_url,
        })
    return {'reply': reply, 'actions': actions}


def attendance_range_summary(schema_name, start_date, end_date, roman_urdu=True, report_url=None):
    with schema_context(schema_name):
        totals = StudentAttendance.objects.filter(
            date__range=(start_date, end_date),
            period_order__isnull=True,
        ).aggregate(
            marked=Count('pk'),
            present=Count('pk', filter=Q(status='present')),
            absent=Count('pk', filter=Q(status='absent')),
            late=Count('pk', filter=Q(status='late')),
            half_day=Count('pk', filter=Q(status='half_day')),
            excused=Count('pk', filter=Q(status='excused')),
        )
    eligible = totals['present'] + totals['absent'] + totals['late'] + totals['half_day']
    rate = round((totals['present'] + totals['late'] + totals['half_day']) * 100 / eligible, 1) if eligible else 0
    if roman_urdu:
        reply = (
            f'{start_date.isoformat()} se {end_date.isoformat()} tak full-day attendance: '
            f'{totals["marked"]} recorded marks, {totals["present"]} present, '
            f'{totals["absent"]} absent, {totals["late"]} late; recorded attendance rate {rate}%. '
            'Counts recorded marks, not unique students.'
        )
    else:
        reply = (
            f'Full-day attendance from {start_date.isoformat()} through {end_date.isoformat()}: '
            f'{totals["marked"]} recorded marks, {totals["present"]} present, '
            f'{totals["absent"]} absent, {totals["late"]} late; recorded attendance rate {rate}%. '
            'Counts are recorded marks, not unique students.'
        )
    actions = []
    if report_url:
        actions.append({
            'label': 'Attendance report',
            'detail': f'{start_date.isoformat()} – {end_date.isoformat()}',
            'url': report_url,
        })
    return {'reply': reply, 'actions': actions}


def timetable_period_summary(schema_name, period_number, weekday, next_occurrence=True, roman_urdu=True):
    today = timezone.localdate()
    days_ahead = (weekday - today.weekday()) % 7
    if next_occurrence and days_ahead == 0:
        days_ahead = 7
    target_date = today + timedelta(days=days_ahead)
    rows_by_class = {}

    with schema_context(schema_name):
        period_assignments = PeriodTeacherAssignment.objects.filter(
            day_of_week=weekday,
            period_order=period_number,
        ).select_related(
            'school_class', 'school_class__wing_category', 'subject', 'teacher',
        ).order_by('school_class__name', 'school_class__section')
        for item in period_assignments:
            rows_by_class[item.school_class_id] = {
                'school_class': str(item.school_class),
                'class_name': item.school_class.name,
                'section': item.school_class.section,
                'subject': item.subject.name if item.subject_id else '',
                'teacher': item.teacher.full_name if item.teacher_id else '',
                'start_time': '',
                'end_time': '',
            }

        timetable_entries = TimetableEntry.objects.filter(
            day_of_week=weekday,
            period__order=period_number,
        ).select_related(
            'school_class', 'school_class__wing_category', 'period', 'subject', 'teacher',
        ).order_by('school_class__name', 'school_class__section')
        for item in timetable_entries:
            rows_by_class.setdefault(item.school_class_id, {
                'school_class': str(item.school_class),
                'class_name': item.school_class.name,
                'section': item.school_class.section,
                'subject': item.subject.name,
                'teacher': item.teacher.full_name,
                'start_time': item.period.start_time.strftime('%H:%M'),
                'end_time': item.period.end_time.strftime('%H:%M'),
            })

        substitutes = SubstituteAssignment.objects.filter(
            date=target_date,
            day_of_week=weekday,
            period_order=period_number,
        ).select_related('school_class', 'substitute_teacher', 'subject')
        for item in substitutes:
            row = rows_by_class.get(item.school_class_id)
            if row:
                row['teacher'] = item.substitute_teacher.full_name
                row['substitute'] = True
                if item.subject_id:
                    row['subject'] = item.subject.name

    rows = list(rows_by_class.values())
    weekday_name = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')[weekday]
    if rows:
        if roman_urdu:
            lines = [
                f"{row['school_class']}: {row['subject'] or 'subject assign nahin'}, "
                f"{row['teacher'] or 'teacher assign nahin'}"
                + (f" ({row['start_time']}-{row['end_time']})" if row['start_time'] else '')
                + (' [substitute]' if row.get('substitute') else '')
                for row in rows
            ]
            reply = f"{target_date.isoformat()} ({weekday_name}) period {period_number}:\n" + '\n'.join(lines)
        else:
            lines = [
                f"{row['school_class']}: {row['subject'] or 'no subject assigned'}, "
                f"{row['teacher'] or 'no teacher assigned'}"
                + (f" ({row['start_time']}-{row['end_time']})" if row['start_time'] else '')
                + (' [substitute]' if row.get('substitute') else '')
                for row in rows
            ]
            reply = f"{target_date.isoformat()} ({weekday_name}), period {period_number}:\n" + '\n'.join(lines)
    elif roman_urdu:
        reply = f'{target_date.isoformat()} ({weekday_name}) ko period {period_number} ka timetable record nahin mila.'
    else:
        reply = f'No timetable entry found for period {period_number} on {target_date.isoformat()} ({weekday_name}).'

    return {'reply': reply, 'actions': []}


def search_defaulters(schema_name, query='', roman_urdu=True, limit=8):
    query = (query or '').strip()[:80]
    with schema_context(schema_name):
        students = Student.objects.filter(status='active')
        if query:
            students = students.filter(
                Q(name__icontains=query) | Q(roll_number__iexact=query)
            )
        students = get_student_pending_queryset(students).filter(
            pending_amount__gt=0
        ).order_by('-pending_amount', 'name')
        total = students.count()
        matches = list(students[:max(1, min(limit, 10))])
    actions = [{
        'label': f'{student.name} · {student.roll_number}',
        'detail': f'{student.pending_amount:,.2f} pending · {student.grade} · {student.section}',
        'url': reverse('student_profile', kwargs={
            'schema_name': schema_name,
            'student_id': student.pk,
        }),
    } for student in matches]
    if roman_urdu:
        reply = f'{total} active student(s) ki fee pending hai.'
    else:
        reply = f'{total} active student(s) have a pending fee balance.'
    return {'reply': reply, 'actions': actions}


def search_classes(schema_name, query, roman_urdu=True, limit=8):
    query = (query or '').strip()[:80]
    with schema_context(schema_name):
        classes = SchoolClass.objects.filter(is_active=True).select_related(
            'wing_category', 'wing_category__parent', 'class_teacher'
        ).annotate(active_students=Count('students', filter=Q(students__status='active')))
        if query:
            classes = classes.filter(
                Q(name__icontains=query)
                | Q(section__icontains=query)
                | Q(wing_category__name__icontains=query)
                | Q(wing_category__parent__name__icontains=query)
            )
        total = classes.count()
        matches = list(classes.order_by('name', 'section')[:max(1, min(limit, 10))])
    actions = [{
        'label': str(school_class),
        'detail': f'{school_class.active_students} active students · {school_class.class_teacher.full_name if school_class.class_teacher else "No class teacher"}',
        'url': reverse('class_detailed', kwargs={
            'schema_name': schema_name,
            'class_id': school_class.pk,
        }),
    } for school_class in matches]
    reply = (
        f'{total} matching class(es) milin.' if roman_urdu
        else f'Found {total} matching class(es).'
    )
    return {'reply': reply, 'actions': actions}


def stock_summary(schema_name, roman_urdu=True):
    with schema_context(schema_name):
        total_products = Product.objects.count()
        total_units = Product.objects.aggregate(total=Sum('quantity'))['total'] or 0
        low_stock = list(
            Product.objects.filter(quantity__lte=5).order_by('quantity', 'name')[:8]
        )
    actions = [{
        'label': product.name,
        'detail': f'{product.quantity} units left · SKU {product.sku}',
        'url': reverse('product_detail', kwargs={
            'schema_name': schema_name,
            'product_id': product.pk,
        }),
    } for product in low_stock]
    reply = (
        f'{total_products} products aur {total_units} units current stock mein hain. '
        f'{len(low_stock)} products mein 5 ya us se kam units hain.'
        if roman_urdu else
        f'{total_products} products and {total_units} units are in stock. '
        f'{len(low_stock)} products have five or fewer units.'
    )
    return {'reply': reply, 'actions': actions}
