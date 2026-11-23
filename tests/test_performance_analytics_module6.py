"""ANTI-MATRIX Phase 2 — Module 6 Test Suite
Performance Analytics & Reporting System.
"""

import io
import csv
import json
from datetime import datetime, timedelta
import unittest
from app import create_app, db
from app.models import (
    User, Student, Internship, Project, ProjectAssignment,
    WeeklyMilestone, WeeklyTask, WeeklySubmission, Evaluation,
    EvaluationRubric, AuditLog, Notification
)
from app.seed import seed_initial_data
from app.services.analytics_reporting_service import AnalyticsReportingService
from tests.test_config import TestConfig


class TestPerformanceAnalyticsModule6(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.app_context = self.app.app_context()
        self.app_context.push()

        db.drop_all()
        db.create_all()
        seed_initial_data()

        self.client = self.app.test_client()

        # Seeded admin user
        self.admin = User.query.filter_by(employee_id='admin').first() or User.query.filter_by(role='admin').first()

        # Seeded mentors
        self.mentor1 = User.query.filter_by(email='praveen@antimatrix.co.in').first()
        self.mentor2 = User.query.filter_by(email='satishkumar@antimatrix.co.in').first()

        # Seeded student & active internship
        self.student = Student.query.filter_by(student_uid='AM-INT-2026-001').first()
        self.student_user = self.student.user
        self.internship = self.student.active_internship
        self.internship.mentor_id = self.mentor1.id
        db.session.commit()

        # Project Assignment linking internship to project
        self.assignment = self.internship.active_assignment
        if not self.assignment:
            self.project = Project.query.first()
            self.assignment = ProjectAssignment(
                internship_id=self.internship.id,
                project_id=self.project.id,
                assigned_by=self.admin.id,
                status='ASSIGNED'
            )
            db.session.add(self.assignment)
            db.session.commit()
        else:
            self.project = self.assignment.project

        # Milestones setup
        m1 = self.assignment.weekly_milestones.filter_by(week_number=1).first()
        if not m1:
            m1 = WeeklyMilestone(
                assignment_id=self.assignment.id,
                week_number=1,
                title='Week 1: Foundations',
                objective='Setup environment',
                status='AVAILABLE',
                due_at=datetime.utcnow() + timedelta(days=7)
            )
            db.session.add(m1)
        else:
            m1.status = 'AVAILABLE'
            m1.due_at = datetime.utcnow() + timedelta(days=7)

        m2 = self.assignment.weekly_milestones.filter_by(week_number=2).first()
        if not m2:
            m2 = WeeklyMilestone(
                assignment_id=self.assignment.id,
                week_number=2,
                title='Week 2: Backend APIs',
                objective='Implement endpoints',
                status='LOCKED',
                due_at=datetime.utcnow() + timedelta(days=14)
            )
            db.session.add(m2)

        db.session.commit()
        self.milestone1 = m1
        self.milestone2 = m2

        # Create sample task on milestone 1
        t1 = self.milestone1.tasks.first()
        if not t1:
            t1 = WeeklyTask(
                milestone_id=self.milestone1.id,
                task_text='Implement API factory',
                priority='High',
                status='ASSIGNED',
                is_completed=True
            )
            db.session.add(t1)
        else:
            t1.is_completed = True
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def login_admin(self):
        self.client.get('/logout')
        return self.client.post('/admin/login', data={
            'email_or_id': 'admin',
            'password': 'Admin@12345'
        }, follow_redirects=True)

    def login_mentor(self, mentor):
        self.client.get('/logout')
        return self.client.post('/admin/login', data={
            'email_or_id': mentor.email,
            'password': 'Mentor@2026Password!'
        }, follow_redirects=True)

    def login_student(self):
        self.client.get('/logout')
        return self.client.post('/login', data={
            'employee_id': 'AM-INT-2026-001',
            'password': 'Student@2026Password!'
        }, follow_redirects=True)

    # ── Test 1: Organization KPIs Calculation & Zero-Division Safety ───────────
    def test_organization_kpis_calculation(self):
        kpis = AnalyticsReportingService.get_organization_kpis()

        # Check all required keys exist
        required_keys = [
            'total_interns', 'active_interns', 'completed_internships',
            'total_mentors', 'active_projects', 'total_projects',
            'pending_submissions', 'total_submissions', 'overdue_milestones_count',
            'avg_completion_rate', 'avg_evaluation_score', 'on_time_rate',
            'submission_approval_rate', 'revision_rate', 'duration_distribution'
        ]
        for key in required_keys:
            self.assertIn(key, kpis)

        # Numerical integrity
        self.assertGreaterEqual(kpis['total_interns'], 1)
        self.assertGreaterEqual(kpis['total_mentors'], 1)
        self.assertGreaterEqual(kpis['active_projects'], 1)
        self.assertIsInstance(kpis['avg_completion_rate'], float)
        self.assertIsInstance(kpis['avg_evaluation_score'], float)
        self.assertIsInstance(kpis['on_time_rate'], float)
        self.assertIsInstance(kpis['submission_approval_rate'], float)
        self.assertIsInstance(kpis['revision_rate'], float)

    # ── Test 2: Intern Performance Analytics & Directory Listing ──────────────
    def test_intern_performance_analytics_and_filtering(self):
        # 1. Base query
        records = AnalyticsReportingService.get_interns_analytics()
        self.assertGreaterEqual(len(records), 1)

        first = records[0]
        self.assertIn('intern_id', first)
        self.assertIn('full_name', first)
        self.assertIn('progress_percent', first)
        self.assertIn('on_time_rate', first)
        self.assertIn('approval_rate', first)

        # 2. Filter by search
        filtered = AnalyticsReportingService.get_interns_analytics(search='AM-INT-2026-001')
        self.assertGreaterEqual(len(filtered), 1)
        self.assertEqual(filtered[0]['intern_id'], 'AM-INT-2026-001')

        # 3. Filter by non-existent query
        none_found = AnalyticsReportingService.get_interns_analytics(search='NonExistentStudentQuery123')
        self.assertEqual(len(none_found), 0)

        # 4. Filter by status
        active_list = AnalyticsReportingService.get_interns_analytics(status='ACTIVE')
        self.assertGreaterEqual(len(active_list), 1)

    # ── Test 3: Intern Detail Scorecard & Rubric Breakdown ────────────────────
    def test_intern_detail_scorecard_and_rubric_averages(self):
        # Create a submission and evaluation on milestone 1
        submission = WeeklySubmission(
            milestone_id=self.milestone1.id,
            student_id=self.student.id,
            repo_url='https://github.com/antimatrix/test-repo',
            submitted_at=datetime.utcnow() - timedelta(hours=2),
            status='APPROVED'
        )
        db.session.add(submission)
        db.session.commit()

        evaluation = Evaluation(
            assignment_id=self.assignment.id,
            submission_id=submission.id,
            milestone_id=self.milestone1.id,
            employee_id=self.student.id,
            evaluator_id=self.mentor1.id,
            technical_score=24,
            code_quality_score=19,
            functionality_score=23,
            documentation_score=14,
            presentation_score=13,
            total_score=93,
            feedback='Outstanding work on architecture and code clean-up.',
            evaluated_at=datetime.utcnow()
        )
        db.session.add(evaluation)
        self.milestone1.status = 'APPROVED'
        db.session.commit()

        # Query scorecard
        scorecard = AnalyticsReportingService.get_intern_detail_analytics(self.student.id)
        self.assertIsNotNone(scorecard)
        self.assertEqual(scorecard['intern_id'], 'AM-INT-2026-001')
        self.assertEqual(scorecard['avg_score'], 93.0)
        self.assertEqual(scorecard['on_time_rate'], 100.0)
        self.assertEqual(scorecard['approval_rate'], 100.0)
        self.assertEqual(scorecard['revision_rate'], 0.0)

        # Check rubric averages
        rubric = scorecard['rubric_averages']
        self.assertEqual(rubric['technical'], 24.0)
        self.assertEqual(rubric['code_quality'], 19.0)
        self.assertEqual(rubric['functionality'], 23.0)
        self.assertEqual(rubric['documentation'], 14.0)
        self.assertEqual(rubric['presentation'], 13.0)

        # Check weekly trends
        trends = scorecard['weekly_trends']
        self.assertGreaterEqual(len(trends), 1)
        self.assertEqual(trends[0]['week_number'], 1)
        self.assertEqual(trends[0]['score'], 93)

    # ── Test 4: Mentor Analytics & Supervisory Telemetry ──────────────────────
    def test_mentor_performance_analytics_and_workload(self):
        mentors = AnalyticsReportingService.get_mentors_analytics()
        self.assertGreaterEqual(len(mentors), 1)

        praveen = next((m for m in mentors if m['mentor'].id == self.mentor1.id), None)
        self.assertIsNotNone(praveen)
        self.assertGreaterEqual(praveen['total_assigned'], 1)
        self.assertIn(praveen['workload_tier'], ['Low Load', 'Balanced', 'At Capacity', 'Overloaded'])
        self.assertIsInstance(praveen['utilization_rate'], float)
        self.assertIsInstance(praveen['avg_turnaround_hours'], float)

    # ── Test 5: Project Portfolio Analytics ───────────────────────────────────
    def test_project_performance_analytics(self):
        # 1. Project list analytics
        projects = AnalyticsReportingService.get_projects_analytics()
        self.assertGreaterEqual(len(projects), 1)

        first_proj = projects[0]
        self.assertIn('project_code', first_proj)
        self.assertIn('title', first_proj)
        self.assertIn('allocated_count', first_proj)
        self.assertIn('capacity_utilization', first_proj)

        # 2. Detailed project analytics
        detail = AnalyticsReportingService.get_project_detail_analytics(self.project.id)
        self.assertIsNotNone(detail)
        self.assertIn('capacity', detail)
        self.assertIn('allocated_count', detail)
        self.assertIn('roster', detail)
        self.assertGreaterEqual(len(detail['roster']), 1)

    # ── Test 6: RFC 4180 Compliant CSV Export Generation ──────────────────────
    def test_csv_export_generation_and_rfc4180(self):
        report_types = [
            'interns_performance',
            'mentors_performance',
            'projects_summary',
            'organization_monthly',
            'internship_completions'
        ]

        for r_type in report_types:
            csv_text = AnalyticsReportingService.generate_csv_report(r_type)
            self.assertIsInstance(csv_text, str)
            self.assertTrue(len(csv_text) > 0)

            # Validate valid CSV parsing
            reader = csv.reader(io.StringIO(csv_text))
            rows = list(reader)
            self.assertGreaterEqual(len(rows), 1, f"Report {r_type} generated no rows")
            header = rows[0]
            self.assertGreater(len(header), 1, f"Report {r_type} header has insufficient columns")

        # Test mentor mentees CSV
        mentees_csv = AnalyticsReportingService.generate_mentor_mentees_csv(self.mentor1.id)
        self.assertIsInstance(mentees_csv, str)
        reader = csv.reader(io.StringIO(mentees_csv))
        mentee_rows = list(reader)
        self.assertGreaterEqual(len(mentee_rows), 2) # header + at least 1 mentee

    # ── Test 7: Admin Analytics & Reports Controller Routes ───────────────────
    def test_admin_analytics_routes_and_access_control(self):
        # 1. Unauthenticated redirect
        res = self.client.get('/admin/analytics')
        self.assertEqual(res.status_code, 302)

        # 2. Login admin
        self.login_admin()

        # 3. Analytics main dashboard
        res = self.client.get('/admin/analytics')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Organization Performance & Analytics', res.data)
        self.assertIn(b'Executive Overview', res.data)

        # 4. JSON API mode
        res_json = self.client.get('/admin/analytics?format=json')
        self.assertEqual(res_json.status_code, 200)
        data = json.loads(res_json.data)
        self.assertIn('kpis', data)
        self.assertIn('interns_count', data)

        # 5. Intern Scorecard Detail
        res = self.client.get(f'/admin/analytics/interns/{self.student.id}')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Candidate Scorecard', res.data)

        # 6. Project Analytics Detail
        res = self.client.get(f'/admin/analytics/projects/{self.project.id}')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Project Telemetry', res.data)

        # 7. Reports Hub
        res = self.client.get('/admin/reports')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Executive Report Generation Center', res.data)

        # 8. CSV Export endpoint
        res = self.client.get('/admin/reports/export/csv?report_type=interns_performance')
        self.assertEqual(res.status_code, 200)
        self.assertIn('text/csv', res.headers.get('Content-Type', ''))
        self.assertIn('attachment;', res.headers.get('Content-Disposition', ''))
        self.assertIn('Intern ID', res.data.decode('utf-8'))

        # 9. Printable Report View
        res = self.client.get('/admin/reports/printable/organization_monthly')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Printable Audit Document', res.data)

    # ── Test 8: Mentor Analytics & Supervisory CSV Export Routes ──────────────
    def test_mentor_analytics_routes_and_csv_export(self):
        # 1. Login mentor
        self.login_mentor(self.mentor1)

        # 2. Access mentor analytics
        res = self.client.get('/mentor/analytics')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Supervisory Performance & Analytics', res.data)
        self.assertIn(b'Assigned Mentees Performance Roster', res.data)

        # 3. Export mentees CSV
        res = self.client.get('/mentor/analytics/export/csv')
        self.assertEqual(res.status_code, 200)
        self.assertIn('text/csv', res.headers.get('Content-Type', ''))
        self.assertIn('attachment;', res.headers.get('Content-Disposition', ''))
        self.assertIn('AM-INT-2026-001', res.data.decode('utf-8'))

    # ── Test 9: Student Self-Service Analytics & IDOR Boundary ─────────────────
    def test_student_analytics_and_idor_protection(self):
        # 1. Login student
        self.login_student()

        # 2. Access student analytics
        res = self.client.get('/analytics')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'My Performance & Progress Telemetry', res.data)
        self.assertIn(b'AM-INT-2026-001', res.data)

        # 3. Attempt to access admin analytics -> strictly denied / redirected
        res = self.client.get('/admin/analytics')
        self.assertIn(res.status_code, [302, 403])

        # 4. Attempt to access mentor analytics -> strictly denied (403 or redirect)
        res = self.client.get('/mentor/analytics')
        self.assertIn(res.status_code, [302, 403])

    # ── Test 10: Zero-Division Safety on Empty Datasets ────────────────────────
    def test_zero_division_safety_empty_state(self):
        # Delete all submissions, evaluations, and assignments
        Evaluation.query.delete()
        WeeklySubmission.query.delete()
        ProjectAssignment.query.delete()
        Internship.query.delete()
        Student.query.delete()
        Project.query.delete()
        db.session.commit()

        # Execute all analytics methods on empty DB — must not raise ZeroDivisionError
        kpis = AnalyticsReportingService.get_organization_kpis()
        self.assertEqual(kpis['total_interns'], 0)
        self.assertEqual(kpis['avg_completion_rate'], 0.0)
        self.assertEqual(kpis['avg_evaluation_score'], 0.0)
        self.assertEqual(kpis['on_time_rate'], 0.0)
        self.assertEqual(kpis['submission_approval_rate'], 0.0)
        self.assertEqual(kpis['revision_rate'], 0.0)

        interns = AnalyticsReportingService.get_interns_analytics()
        self.assertEqual(len(interns), 0)

        mentors = AnalyticsReportingService.get_mentors_analytics()
        self.assertIsInstance(mentors, list)

        projects = AnalyticsReportingService.get_projects_analytics()
        self.assertEqual(len(projects), 0)

        csv_monthly = AnalyticsReportingService.generate_csv_report('organization_monthly')
        self.assertTrue(len(csv_monthly) > 0)


if __name__ == '__main__':
    unittest.main()
