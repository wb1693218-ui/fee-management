import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone

from axis_saas.views.ai.assistant import assistant_api
from axis_saas.views.ai.intents import extract_timetable_period_question
from axis_saas.views.ai.tools import timetable_period_summary


class AssistantSession(dict):
    session_key = 'timetable-assistant-test'
    modified = False


class TimetableAssistantTests(SimpleTestCase):
    def test_period_next_monday_phrase_is_parsed(self):
        self.assertEqual(
            extract_timetable_period_question(
                '5 period next monday ko kis ka kis class me he'
            ),
            (5, 0, True),
        )

    def test_ordinal_period_and_roman_urdu_weekday_are_parsed(self):
        self.assertEqual(
            extract_timetable_period_question('5th period aglay peer kis class me hai'),
            (5, 0, True),
        )

    def test_period_without_weekday_is_not_guessed(self):
        self.assertIsNone(
            extract_timetable_period_question('5th period kis class ka hai')
        )

    def test_today_period_question_uses_school_local_weekday(self):
        self.assertEqual(
            extract_timetable_period_question('aaj 5 period kis ka he'),
            (5, timezone.localdate().weekday(), False),
        )

    @override_settings(AI_ASSISTANT_API_KEY='', AI_ASSISTANT_MODEL='')
    @patch('axis_saas.views.ai.assistant._rate_limited', return_value=False)
    @patch('axis_saas.views.ai.assistant.run_assistant_model_turn')
    @patch('axis_saas.views.ai.assistant.timetable_period_summary')
    def test_schedule_question_uses_local_tenant_query_without_provider(
        self, timetable_summary, provider, _rate_limited,
    ):
        request = RequestFactory().post(
            '/portal/demo/api/ai/assistant/',
            data=json.dumps({
                'message': '5 period next monday ko kis ka kis class me he',
                'conversation_id': '8f853ba6-68a4-4c86-9974-3bb9259076a4',
                'confirm_school_data_sharing': False,
            }),
            content_type='application/json',
        )
        request.tenant = SimpleNamespace(
            tenant_type='school',
            is_feature_enabled=lambda feature, _channel: feature in {
                'ai_assistant', 'timetable_management',
            },
        )
        request.session = AssistantSession()
        timetable_summary.return_value = {
            'reply': '2026-10-12 Monday period 5: Class 10-A: Mathematics, Sami Khan',
            'actions': [],
        }

        response = assistant_api(request, 'demo')

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertEqual(body['kind'], 'timetable_lookup')
        timetable_summary.assert_called_once_with(
            'demo', 5, 0, next_occurrence=True, roman_urdu=True,
        )
        provider.assert_not_called()

    @override_settings(AI_ASSISTANT_API_KEY='', AI_ASSISTANT_MODEL='')
    @patch('axis_saas.views.ai.assistant._rate_limited', return_value=False)
    @patch('axis_saas.views.ai.assistant.run_assistant_model_turn')
    def test_inventory_how_to_returns_local_guide_without_provider(
        self, provider, _rate_limited,
    ):
        request = RequestFactory().post(
            '/portal/demo/api/ai/assistant/',
            data=json.dumps({'message': 'stock kese add kroo'}),
            content_type='application/json',
        )
        request.path = '/portal/demo/stock/'
        request.tenant = SimpleNamespace(
            tenant_type='school',
            is_feature_enabled=lambda _feature, _channel: True,
        )
        request.session = AssistantSession()

        response = assistant_api(request, 'demo')

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertEqual(body['kind'], 'documentation')
        self.assertIn('Add Product', body['reply'])
        self.assertIn('category', body['reply'].lower())
        provider.assert_not_called()

    @patch('axis_saas.views.ai.tools.timezone.localdate')
    @patch('axis_saas.views.ai.tools.schema_context')
    @patch('axis_saas.views.ai.tools.SubstituteAssignment.objects.filter')
    @patch('axis_saas.views.ai.tools.TimetableEntry.objects.filter')
    @patch('axis_saas.views.ai.tools.PeriodTeacherAssignment.objects.filter')
    def test_next_monday_period_lookup_reads_school_timetable_and_substitute(
        self,
        period_filter,
        timetable_filter,
        substitute_filter,
        tenant_context,
        localdate,
    ):
        from datetime import date

        localdate.return_value = date(2026, 10, 10)
        tenant_context.return_value = MagicMock()
        school_class = SimpleNamespace(name='Class 10', section='A')
        period_assignment = SimpleNamespace(
            school_class_id=1,
            school_class=school_class,
            subject_id=3,
            subject=SimpleNamespace(name='Mathematics'),
            teacher_id=4,
            teacher=SimpleNamespace(full_name='Original Teacher'),
        )
        period_filter.return_value.select_related.return_value.order_by.return_value = [
            period_assignment,
        ]
        timetable_filter.return_value.select_related.return_value.order_by.return_value = []
        substitute_filter.return_value.select_related.return_value = [SimpleNamespace(
            school_class_id=1,
            substitute_teacher=SimpleNamespace(full_name='Cover Teacher'),
            subject_id=None,
        )]

        result = timetable_period_summary('school_alpha', 5, 0, next_occurrence=True)

        self.assertIn('2026-10-12', result['reply'])
        self.assertIn('Mathematics', result['reply'])
        self.assertIn('Cover Teacher', result['reply'])
        period_filter.assert_called_once_with(day_of_week=0, period_order=5)
        timetable_filter.assert_called_once_with(day_of_week=0, **{'period__order': 5})
        substitute_filter.assert_called_once_with(
            date=date(2026, 10, 12), day_of_week=0, period_order=5,
        )
        tenant_context.assert_called_once_with('school_alpha')
