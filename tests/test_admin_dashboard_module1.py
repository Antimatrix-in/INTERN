import unittest
from datetime import datetime, timedelta
from app import create_app
from app.extensions import db
from app.models import (
    User, Student, Internship, Project, ProjectAssignment,
    WeeklyMilestone, WeeklyTask, WeeklySubmission, InternshipPlan
)
from app.seed import seed_initial_data
from tests.test_config import TestConfig


class AdminDashboardModule1TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        with self.app.app_context():
            db.drop_all()
            db.create_all()
            seed_initial_data()
        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    # ── Helpers ────────────────────────────────────────────────────────────────
    def login_admin(self):
        self.client.get('/logout')
        return self.client.post('/admin/login', data={
            'email_or_id': 'admin',
            'password': 'Admin@12345'
        }, follow_redirects=True)

    def login_student(self):
        self.client.get('/logout')
        return self.client.post('/login', data={
            'employee_id': 'AM-INT-2026-001',
            'password': 'Student@2026Password!'
        }, follow_redirects=True)

    # ── 1. Security & Role Boundaries ──────────────────────────────────────────
    def test_unauthenticated_blocked(self):
        """Unauthenticated requests to /admin/dashboard must redirect to login."""
        res = self.client.get('/admin/dashboard')
        self.assertIn(res.status_code, [302, 401])

    def test_student_role_strictly_forbidden(self):
        """Student role must receive 403 Forbidden when attempting to access /admin/dashboard."""
        self.login_student()
        res = self.client.get('/admin/dashboard')
        self.assertEqual(res.status_code, 403)

    # ── 2. Admin Dashboard Access ──────────────────────────────────────────────
    def test_admin_dashboard_accessible(self):
        """Administrator logs in and receives HTTP 200 on /admin/dashboard."""
        login_res = self.login_admin()
        self.assertEqual(login_res.status_code, 200)

        res = self.client.get('/admin/dashboard')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("Operations & Workforce Command Center", html)
        self.assertIn("LIVE TELEMETRY ACTIVE", html)

    # ── 3. Exact 6 KPI Cards ───────────────────────────────────────────────────
    def test_exact_six_kpi_cards_rendered(self):
        """Dashboard must render all 6 required KPI cards: Total Interns, Active Interns,
        Completed Internships, Total Mentors, Active Projects, Pending Submissions."""
        self.login_admin()
        res = self.client.get('/admin/dashboard')
        html = res.get_data(as_text=True)

        self.assertIn("TOTAL INTERNS", html)
        self.assertIn("ACTIVE INTERNS", html)
        self.assertIn("COMPLETED INTERNSHIPS", html)
        self.assertIn("TOTAL MENTORS", html)
        self.assertIn("ACTIVE PROJECTS", html)
        self.assertIn("PENDING SUBMISSIONS", html)

    # ── 4. Internship Analytics ────────────────────────────────────────────────
    def test_internship_analytics_rendered(self):
        """Dashboard must render Weekly task completion, progress distribution,
        project allocation stats, and submission review stats."""
        self.login_admin()
        res = self.client.get('/admin/dashboard')
        html = res.get_data(as_text=True)

        self.assertIn("Internship Analytics", html)
        self.assertIn("Weekly Task Completion", html)
        self.assertIn("Progress Distribution", html)
        self.assertIn("Project Allocation Stats", html)
        self.assertIn("Review Pipeline Stats", html)

    # ── 5. Upcoming Deadlines Section ──────────────────────────────────────────
    def test_upcoming_deadlines_rendered(self):
        """Dashboard must display upcoming deadlines with intern name, ID, project name,
        milestone, and deadline."""
        self.login_admin()
        res = self.client.get('/admin/dashboard')
        html = res.get_data(as_text=True)

        self.assertIn("Upcoming Deadlines", html)
        self.assertIn("Aarav Kumar", html)
        self.assertIn("AM-INT-2026-001", html)

    # ── 6. Recent Activity Feed ────────────────────────────────────────────────
    def test_recent_activity_feed_rendered(self):
        """Dashboard must render the Recent Activity feed."""
        self.login_admin()
        res = self.client.get('/admin/dashboard')
        html = res.get_data(as_text=True)

        self.assertIn("Recent Activity", html)

    # ── 7. Project Overview Table ──────────────────────────────────────────────
    def test_project_overview_table_rendered(self):
        """Dashboard must display the Project Overview table with allocated interns,
        assigned mentors, completion percentage, and project status."""
        self.login_admin()
        res = self.client.get('/admin/dashboard')
        html = res.get_data(as_text=True)

        self.assertIn("Project Overview", html)
        self.assertIn("AM-PRJ-001", html)
        self.assertIn("AI-Based Student Performance Analysis System", html)
        self.assertIn("Allocated Interns", html)
        self.assertIn("Assigned Mentors", html)
        self.assertIn("Completion Rate", html)

    # ── 8. Mentor Workload Distribution ────────────────────────────────────────
    def test_mentor_workload_distribution(self):
        """Dashboard must display the mentor workload tracker with approved mentors."""
        self.login_admin()
        res = self.client.get('/admin/dashboard')
        html = res.get_data(as_text=True)

        self.assertIn("Mentor Workload Tracker", html)
        self.assertIn("Praveen", html)
        self.assertIn("praveen@antimatrix.co.in", html)


if __name__ == '__main__':
    unittest.main()
