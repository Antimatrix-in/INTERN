"""
Unit Test Suite for Phase 2 — Module 2: Advanced Intern Management System
Anti-Matrix Enterprise Intern Portal
"""
import unittest
import json
from datetime import datetime
from app import create_app
from app.extensions import db
from app.models import (
    User, Student, Internship, Project, ProjectAssignment,
    WeeklyMilestone, WeeklyTask, WeeklySubmission, College, Department,
    InternshipPlan, AuditLog, Application, Document
)
from app.seed import seed_initial_data
from tests.test_config import TestConfig


class AdminInternManagementModule2TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.app_context = self.app.app_context()
        self.app_context.push()

        db.drop_all()
        db.create_all()
        seed_initial_data()

        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def login_admin(self):
        return self.client.post('/admin/login', data={
            'email_or_id': 'admin',
            'password': 'Admin@12345'
        }, follow_redirects=True)

    # ── Test 1: Direct Manual Intern Registration ─────────────────────────────

    def test_01_manual_intern_registration_success(self):
        """Admin can directly register a new intern with auto-generated ID and temporary password."""
        self.login_admin()

        college = College.query.first()
        dept = Department.query.filter_by(college_id=college.id).first()

        resp = self.client.post('/admin/employees/create', data={
            'reg_mode': 'manual',
            'full_name': 'Meera Sundaram',
            'email': 'meera.sundaram@example.com',
            'phone': '+91 9123456780',
            'gender': 'Female',
            'dob': '2004-05-15',
            'college_id': college.id,
            'department_id': dept.id,
            'degree': 'B.Tech Information Technology',
            'academic_year': '3rd Year',
            'roll_number': 'REG-2026-IT09',
            'duration_plan': '1_MONTH_PROJECT',
            'start_date': '2026-06-01',
            'end_date': '2026-07-01',
            'initial_status': 'ACTIVE'
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Meera Sundaram', resp.data)
        self.assertIn(b'AM-INT-', resp.data)

        # Database assertions
        user = User.query.filter_by(email='meera.sundaram@example.com').first()
        self.assertIsNotNone(user)
        self.assertEqual(user.full_name, 'Meera Sundaram')
        self.assertTrue(user.is_active)
        self.assertTrue(user.employee_id.startswith('AM-INT-'))

        student = user.student_profile
        self.assertIsNotNone(student)
        self.assertEqual(student.degree, 'B.Tech Information Technology')
        self.assertEqual(student.college_id, college.id)
        self.assertEqual(student.department_id, dept.id)

        internship = student.active_internship
        self.assertIsNotNone(internship)
        self.assertEqual(internship.status, 'ACTIVE')
        self.assertEqual(internship.plan.duration_months, 1)

        # Verify Audit Log entry
        audit = AuditLog.query.filter_by(action='REGISTER_INTERN_MANUAL', target_id=str(student.id)).first()
        self.assertIsNotNone(audit)
        self.assertIn('meera.sundaram@example.com', audit.details_json)

    # ── Test 2: Duplicate Email Prevention ────────────────────────────────────

    def test_02_manual_intern_registration_duplicate_email_rejected(self):
        """Registering an intern with an existing user's email is blocked."""
        self.login_admin()

        existing_user = User.query.filter(User.role == 'student').first()
        self.assertIsNotNone(existing_user)

        resp = self.client.post('/admin/employees/create', data={
            'reg_mode': 'manual',
            'full_name': 'Duplicate Candidate',
            'email': existing_user.email,
            'phone': '+91 9999999999',
            'duration_plan': '1_MONTH_PROJECT'
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'already exists', resp.data)

    # ── Test 3: Intern Directory Filtering & Search ───────────────────────────

    def test_03_intern_directory_search_and_filters(self):
        """Intern directory supports search, duration filtering, and status filtering."""
        self.login_admin()

        # 1. Base directory loads
        resp = self.client.get('/admin/employees')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Aarav Kumar', resp.data)

        # 2. Search query filter
        resp_search = self.client.get('/admin/employees?q=Aarav')
        self.assertEqual(resp_search.status_code, 200)
        self.assertIn(b'Aarav Kumar', resp_search.data)

        # Search for non-existent intern
        resp_none = self.client.get('/admin/employees?q=NonExistentPersonXYZ')
        self.assertEqual(resp_none.status_code, 200)
        self.assertIn(b'No intern records matching query', resp_none.data)

        # 3. Status filter
        resp_status = self.client.get('/admin/employees?status=ALL')
        self.assertEqual(resp_status.status_code, 200)
        self.assertIn(b'Aarav Kumar', resp_status.data)

        # 4. Duration filter
        resp_dur1 = self.client.get('/admin/employees?duration=1')
        self.assertEqual(resp_dur1.status_code, 200)

    # ── Test 4: Individual Intern Profile View ─────────────────────────────────

    def test_04_individual_intern_profile_view(self):
        """Admin can view the comprehensive intern profile with all detail tabs."""
        self.login_admin()

        student = Student.query.first()
        self.assertIsNotNone(student)

        resp = self.client.get(f'/admin/employees/{student.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(student.user.full_name.encode('utf-8'), resp.data)
        self.assertIn(student.student_uid.encode('utf-8'), resp.data)
        self.assertIn(b'Overview & Profile', resp.data)
        self.assertIn(b'Project & Mentor', resp.data)
        self.assertIn(b'Weekly Milestones', resp.data)

    # ── Test 5: Edit Intern Record ────────────────────────────────────────────

    def test_05_edit_intern_record(self):
        """Admin can edit an intern's personal, academic, and internship details."""
        self.login_admin()

        student = Student.query.first()
        user = student.user

        resp_get = self.client.get(f'/admin/employees/{student.id}/edit')
        self.assertEqual(resp_get.status_code, 200)
        self.assertIn(b'Edit Intern Record', resp_get.data)

        # POST update
        resp_post = self.client.post(f'/admin/employees/{student.id}/edit', data={
            'full_name': 'Aarav Kumar Updated',
            'email': user.email,
            'phone': '+91 9888877777',
            'degree': 'B.Tech AI & Data Science',
            'roll_number': 'NEW-ROLL-777',
            'current_year': '4th Year',
            'start_date': '10 Jan 2026',
            'end_date': '10 Feb 2026',
            'status': 'ACTIVE'
        }, follow_redirects=True)

        self.assertEqual(resp_post.status_code, 200)
        self.assertIn(b'updated successfully', resp_post.data)

        # Verify DB updates
        updated_user = User.query.get(user.id)
        self.assertEqual(updated_user.full_name, 'Aarav Kumar Updated')
        self.assertEqual(updated_user.phone, '+91 9888877777')

        updated_student = Student.query.get(student.id)
        self.assertEqual(updated_student.degree, 'B.Tech AI & Data Science')
        self.assertEqual(updated_student.roll_number, 'NEW-ROLL-777')

        # Verify Audit Log
        audit = AuditLog.query.filter_by(action='EDIT_INTERN_RECORD', target_id=str(student.id)).first()
        self.assertIsNotNone(audit)

    # ── Test 6: Lifecycle Status Transitions ──────────────────────────────────

    def test_06_lifecycle_status_transitions(self):
        """Admin can transition intern lifecycle status through valid states."""
        self.login_admin()

        student = Student.query.first()
        internship = student.active_internship

        # 1. Transition to COMPLETED
        resp = self.client.post(f'/admin/employees/{student.id}/status', data={
            'new_status': 'COMPLETED'
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'status successfully changed', resp.data)

        db.session.refresh(internship)
        self.assertEqual(internship.status, 'COMPLETED')

        # 2. Transition to ARCHIVED (which safely deactivates user login)
        resp2 = self.client.post(f'/admin/employees/{student.id}/status', data={
            'new_status': 'ARCHIVED'
        }, follow_redirects=True)
        self.assertEqual(resp2.status_code, 200)

        db.session.refresh(internship)
        db.session.refresh(student.user)
        self.assertEqual(internship.status, 'ARCHIVED')
        self.assertFalse(student.user.is_active)

        # 3. Transition back to ACTIVE (reactivates login)
        resp3 = self.client.post(f'/admin/employees/{student.id}/status', data={
            'new_status': 'ACTIVE'
        }, follow_redirects=True)
        self.assertEqual(resp3.status_code, 200)

        db.session.refresh(internship)
        db.session.refresh(student.user)
        self.assertEqual(internship.status, 'ACTIVE')
        self.assertTrue(student.user.is_active)

    # ── Test 7: Toggle Intern Active Access ───────────────────────────────────

    def test_07_toggle_intern_active_access(self):
        """Admin can activate or deactivate intern user login access."""
        self.login_admin()

        student = Student.query.first()
        user = student.user
        self.assertTrue(user.is_active)

        # Deactivate
        resp = self.client.post(f'/admin/employees/{student.id}/toggle-active', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(user)
        self.assertFalse(user.is_active)

        # Reactivate
        resp2 = self.client.post(f'/admin/employees/{student.id}/toggle-active', follow_redirects=True)
        self.assertEqual(resp2.status_code, 200)
        db.session.refresh(user)
        self.assertTrue(user.is_active)

    # ── Test 8: Archive Intern Action ─────────────────────────────────────────

    def test_08_archive_intern_preserves_records(self):
        """Archiving an intern marks status as ARCHIVED and disables login without deleting data."""
        self.login_admin()

        student = Student.query.first()
        student_id = student.id

        resp = self.client.post(f'/admin/employees/{student_id}/archive', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'safely archived', resp.data)

        # Verify records are preserved
        s = Student.query.get(student_id)
        self.assertIsNotNone(s)
        self.assertFalse(s.user.is_active)
        self.assertEqual(s.active_internship.status, 'ARCHIVED')

    # ── Test 9: Career Application Import Pathway Preserved ───────────────────

    def test_09_career_application_import_pathway_preserved(self):
        """Existing application-no conversion flow remains 100% functional and backward-compatible."""
        self.login_admin()

        resp = self.client.post('/admin/employees/create', data={
            'application_no': 'AM-APP-2026-001',
            'duration_plan': '1_MONTH_PROJECT'
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Rahul Kumar', resp.data)

        app = Application.query.filter_by(application_no='AM-APP-2026-001').first()
        self.assertTrue(app.is_converted_to_employee)
        self.assertIsNotNone(app.converted_employee_id)


if __name__ == '__main__':
    unittest.main()
