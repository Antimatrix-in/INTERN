import unittest
import json
from datetime import datetime, timedelta
from app import create_app
from app.extensions import db
from app.models import (
    User, Student, Internship, Project, ProjectAssignment,
    WeeklyMilestone, WeeklyTask, WeeklySubmission, Evaluation,
    AuditLog, College, Department, InternshipPlan
)
from app.seed import seed_initial_data
from app.services.mentor_service import (
    get_approved_mentors, get_all_active_mentors,
    is_approved_mentor, generate_next_mentor_id
)
from tests.test_config import TestConfig


class TestMentorManagementModule3(unittest.TestCase):
    """
    Automated Test Suite for Phase 2, Module 3:
    Advanced Mentor Management System & Mentor Supervision Portal.
    """

    def setUp(self):
        self.app = create_app(TestConfig)
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.app_context = self.app.app_context()
        self.app_context.push()

        db.drop_all()
        db.create_all()
        seed_initial_data()

        self.client = self.app.test_client()

        # Retrieve seeded entities
        self.admin = User.query.filter_by(employee_id='admin').first() or User.query.filter_by(role='admin').first()
        self.mentor1 = User.query.filter_by(email='praveen@antimatrix.co.in').first()
        self.mentor2 = User.query.filter_by(email='satishkumar@antimatrix.co.in').first()

        # Ensure mentors have Module 3 attributes populated
        self.mentor1.department = 'Cloud Architecture & DevOps'
        self.mentor1.designation = 'Lead Cloud Architect'
        self.mentor1.skills = 'Python, AWS, Terraform, Docker'
        self.mentor1.max_capacity = 5
        self.mentor1.availability_status = 'AVAILABLE'

        self.mentor2.department = 'Computer Science & Engineering'
        self.mentor2.designation = 'Senior Staff Engineer'
        self.mentor2.skills = 'Python, Machine Learning, Scikit-learn'
        self.mentor2.max_capacity = 5
        self.mentor2.availability_status = 'AVAILABLE'

        # Seeded students
        self.student1 = Student.query.filter_by(student_uid='AM-INT-2026-001').first()
        self.student2 = Student.query.filter_by(student_uid='AM-INT-2026-002').first()

        # Ensure student1 is assigned to mentor1, student2 is assigned to mentor2
        self.internship1 = self.student1.active_internship
        self.internship1.mentor_id = self.mentor1.id

        self.internship2 = self.student2.active_internship
        self.internship2.mentor_id = self.mentor2.id

        # Retrieve or create a submission for student1 under mentor1
        self.assignment1 = self.internship1.active_assignment
        self.milestone1 = self.assignment1.weekly_milestones.filter_by(week_number=1).first()
        self.milestone1.status = 'SUBMITTED'

        self.submission1 = self.milestone1.latest_submission
        if not self.submission1:
            self.submission1 = WeeklySubmission(
                milestone_id=self.milestone1.id,
                student_id=self.student1.id,
                github_url='https://github.com/antimatrix/cloud-iac-week1',
                demo_video_url='https://drive.google.com/file/d/demo-video-1',
                submission_notes='Completed Docker compose setup and unit tests.',
                status='SUBMITTED',
                submitted_at=datetime.utcnow() - timedelta(hours=3)
            )
            db.session.add(self.submission1)

        # Retrieve or create a submission for student2 under mentor2
        self.assignment2 = self.internship2.active_assignment
        self.milestone2 = self.assignment2.weekly_milestones.filter_by(week_number=1).first()
        self.milestone2.status = 'SUBMITTED'

        self.submission2 = self.milestone2.latest_submission
        if not self.submission2:
            self.submission2 = WeeklySubmission(
                milestone_id=self.milestone2.id,
                student_id=self.student2.id,
                github_url='https://github.com/antimatrix/cloud-iac-week2',
                demo_video_url='https://drive.google.com/file/d/demo-video-2',
                submission_notes='Containerized service ready for verification.',
                status='SUBMITTED',
                submitted_at=datetime.utcnow() - timedelta(hours=2)
            )
            db.session.add(self.submission2)

        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def login_admin(self):
        return self.client.post('/admin/login', data={
            'email_or_id': 'admin',
            'password': 'Admin@12345'
        }, follow_redirects=True)

    def login_mentor(self, mentor):
        return self.client.post('/admin/login', data={
            'email_or_id': mentor.email,
            'password': 'Mentor@2026Password!'
        }, follow_redirects=True)

    # ─── TEST 1: Unique Mentor ID Generation & Creation ────────────────────────

    def test_01_mentor_creation_and_unique_id_generation(self):
        """Test admin creation of a new mentor with auto-generated sequential ID."""
        self.login_admin()

        next_id = generate_next_mentor_id()
        self.assertTrue(next_id.startswith('AM-MTR-'))

        resp = self.client.post('/admin/mentors/create', data={
            'full_name': 'Sarah Chen',
            'email': 'sarah.chen@antimatrix.co.in',
            'phone': '+91 99999 88888',
            'department': 'Artificial Intelligence & ML',
            'designation': 'Principal ML Engineer',
            'skills': 'PyTorch, Transformers, LLMs, FastAPI',
            'max_capacity': '8',
            'availability_status': 'AVAILABLE',
            'password': 'Mentor@2026Password!'
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)

        created_mentor = User.query.filter_by(email='sarah.chen@antimatrix.co.in').first()
        self.assertIsNotNone(created_mentor)
        self.assertEqual(created_mentor.role, 'mentor')
        self.assertEqual(created_mentor.department, 'Artificial Intelligence & ML')
        self.assertEqual(created_mentor.max_capacity, 8)
        self.assertTrue(created_mentor.check_password('Mentor@2026Password!'))
        self.assertTrue(is_approved_mentor(created_mentor))

        # Check Audit Log was recorded
        audit = AuditLog.query.filter_by(action='CREATE_MENTOR').first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor_id, self.admin.id)

    # ─── TEST 2: Duplicate Email Prevention on Creation ────────────────────────

    def test_02_duplicate_email_prevention(self):
        """Ensure duplicate emails cannot be registered as mentors."""
        self.login_admin()

        resp = self.client.post('/admin/mentors/create', data={
            'full_name': 'Duplicate Praveen',
            'email': 'praveen@antimatrix.co.in',  # Already exists
            'department': 'Engineering',
            'max_capacity': '5'
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'already exists', resp.data)

    # ─── TEST 3: Edit Mentor Profile, Capacity & Status ────────────────────────

    def test_03_edit_mentor_profile(self):
        """Test admin updating mentor technical skills, department, and capacity."""
        self.login_admin()

        resp = self.client.post(f'/admin/mentors/{self.mentor1.id}/edit', data={
            'full_name': 'Praveen Senior Architect',
            'email': 'praveen@antimatrix.co.in',
            'phone': '+91 98765 00000',
            'department': 'Enterprise Cloud & AI',
            'designation': 'Chief Cloud Strategist',
            'skills': 'AWS, GCP, Kubernetes, Go',
            'max_capacity': '12',
            'availability_status': 'BUSY',
            'is_active': '1'
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        updated = User.query.get(self.mentor1.id)
        self.assertEqual(updated.full_name, 'Praveen Senior Architect')
        self.assertEqual(updated.department, 'Enterprise Cloud & AI')
        self.assertEqual(updated.max_capacity, 12)
        self.assertEqual(updated.availability_status, 'BUSY')

    # ─── TEST 4: Toggle Mentor Active Status ──────────────────────────────────

    def test_04_toggle_mentor_active_status(self):
        """Test activating and deactivating a mentor account."""
        self.login_admin()

        # Deactivate
        resp = self.client.post(f'/admin/mentors/{self.mentor1.id}/toggle-active', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        m = User.query.get(self.mentor1.id)
        self.assertFalse(m.is_active)

        # Deactivated mentor cannot sign in
        self.client.get('/logout', follow_redirects=True)
        resp_login = self.client.post('/admin/login', data={
            'email_or_id': 'praveen@antimatrix.co.in',
            'password': 'Mentor@2026Password!'
        }, follow_redirects=True)
        self.assertIn(b'inactive', resp_login.data)

        # Re-activate
        self.login_admin()
        self.client.post(f'/admin/mentors/{self.mentor1.id}/toggle-active', follow_redirects=True)
        m = User.query.get(self.mentor1.id)
        self.assertTrue(m.is_active)

    # ─── TEST 5: Intern Allocation & Multi-Intern Assignment ───────────────────

    def test_05_intern_allocation_and_reassignment(self):
        """Test allocating and reassigning interns to mentors with capacity check."""
        self.login_admin()

        # Reassign Student 2 from Mentor 2 to Mentor 1
        resp = self.client.post(f'/admin/mentors/{self.mentor1.id}/allocate', data={
            'student_ids': [self.student2.id]
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)

        internship2 = Internship.query.get(self.internship2.id)
        self.assertEqual(internship2.mentor_id, self.mentor1.id)

        # Mentor 1 now has 2 active mentees
        self.assertEqual(self.mentor1.active_interns_count, 2)

        # Check Audit Log recorded reassignment
        audit = AuditLog.query.filter_by(action='MENTOR_ALLOCATION').first()
        self.assertIsNotNone(audit)
        details = json.loads(audit.details_json)
        self.assertEqual(details['new_mentor'], 'Praveen')

    # ─── TEST 6: Mentor Role-Based Scoped Access (Security) ────────────────────

    def test_06_mentor_role_based_scoped_authorization(self):
        """
        CRITICAL SECURITY REQUIREMENT:
        Mentors can access ONLY their own assigned interns and submissions.
        Attempts to access unassigned interns or submissions MUST return 403 Forbidden.
        """
        # Sign in as Mentor 1 (Supervises Student 1; NOT Student 2)
        self.login_mentor(self.mentor1)

        # 1. Mentor 1 accessing Mentor Dashboard -> 200 OK
        resp_dash = self.client.get('/mentor/dashboard')
        self.assertEqual(resp_dash.status_code, 200)

        # 2. Mentor 1 accessing assigned intern (Student 1) -> 200 OK
        resp_assigned = self.client.get(f'/mentor/interns/{self.student1.id}')
        self.assertEqual(resp_assigned.status_code, 200)

        # 3. Mentor 1 attempting to access UNASSIGNED intern (Student 2) -> 403 Forbidden!
        resp_unauthorized_student = self.client.get(f'/mentor/interns/{self.student2.id}')
        self.assertEqual(resp_unauthorized_student.status_code, 403)

        # 4. Mentor 1 accessing assigned submission (Submission 1) -> 200 OK
        resp_sub_assigned = self.client.get(f'/mentor/submissions/{self.submission1.id}/review')
        self.assertEqual(resp_sub_assigned.status_code, 200)

        # 5. Mentor 1 attempting to review UNASSIGNED submission (Submission 2) -> 403 Forbidden!
        resp_unauthorized_sub = self.client.get(f'/mentor/submissions/{self.submission2.id}/review')
        self.assertEqual(resp_unauthorized_sub.status_code, 403)

        # 6. Mentor 1 attempting to POST approve on UNASSIGNED submission -> 403 Forbidden!
        resp_unauthorized_post = self.client.post(f'/mentor/submissions/{self.submission2.id}/approve', data={
            'technical_score': '20'
        })
        self.assertEqual(resp_unauthorized_post.status_code, 403)

    # ─── TEST 7: Submission Review, Scoring & Approval ─────────────────────────

    def test_07_submission_review_approval_and_milestone_progression(self):
        """Test structured evaluation scoring, milestone completion, and next milestone unlocking."""
        # Ensure week 2 locked milestone exists for assignment 1
        milestone_w2 = WeeklyMilestone.query.filter_by(assignment_id=self.assignment1.id, week_number=2).first()
        if not milestone_w2:
            milestone_w2 = WeeklyMilestone(
                assignment_id=self.assignment1.id,
                week_number=2,
                title='Database Migration & Pipelines',
                objective='Build data pipelines',
                status='LOCKED'
            )
            db.session.add(milestone_w2)
            db.session.commit()
        else:
            milestone_w2.status = 'LOCKED'
            db.session.commit()

        self.login_mentor(self.mentor1)

        resp = self.client.post(f'/mentor/submissions/{self.submission1.id}/approve', data={
            'technical_score': '19',
            'functionality_score': '19',
            'ui_ux_score': '14',
            'documentation_score': '15',
            'demo_score': '14',
            'originality_score': '9',
            'presentation_score': '5',
            'feedback': 'Exceptional architectural rigor and containerized configuration.'
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)

        # Check submission marked COMPLETED
        sub = WeeklySubmission.query.get(self.submission1.id)
        self.assertEqual(sub.status, 'COMPLETED')
        self.assertEqual(sub.reviewed_by, self.mentor1.id)

        # Check Week 1 milestone marked COMPLETED
        m1 = WeeklyMilestone.query.get(self.milestone1.id)
        self.assertEqual(m1.status, 'COMPLETED')
        self.assertEqual(m1.approved_by, self.mentor1.id)

        # Check Week 2 milestone UNLOCKED and set to AVAILABLE
        m2 = WeeklyMilestone.query.get(milestone_w2.id)
        self.assertEqual(m2.status, 'AVAILABLE')
        self.assertIsNotNone(m2.unlocked_at)

        # Check Evaluation model record created with correct scores
        ev = Evaluation.query.filter_by(submission_id=self.submission1.id).first()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.total_score, 95)
        self.assertEqual(ev.evaluator_id, self.mentor1.id)
        self.assertEqual(ev.result, 'PASSED')

    # ─── TEST 8: Submission Redo / Revision Request ───────────────────────────

    def test_08_submission_redo_request(self):
        """Test mentor requesting revisions on an unsatisfactory submission."""
        self.login_mentor(self.mentor1)

        resp = self.client.post(f'/mentor/submissions/{self.submission1.id}/redo', data={
            'feedback': 'Docker compose fails on port 5000 collision. Please update container ports and re-test.'
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)

        sub = WeeklySubmission.query.get(self.submission1.id)
        self.assertEqual(sub.status, 'REDO_REQUIRED')

        m1 = WeeklyMilestone.query.get(self.milestone1.id)
        self.assertEqual(m1.status, 'REDO_REQUIRED')
        self.assertIn('port 5000', m1.admin_notes)

        # Evaluation record created
        ev = Evaluation.query.filter_by(submission_id=self.submission1.id).first()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.action, 'REDO')

    # ─── TEST 9: Mentor Performance Analytics & Profile Update ────────────────

    def test_09_mentor_analytics_and_profile_management(self):
        """Test analytics calculations and profile availability toggle."""
        self.login_mentor(self.mentor1)

        # Check analytics page loads
        resp_analytics = self.client.get('/mentor/analytics')
        self.assertEqual(resp_analytics.status_code, 200)
        self.assertIn(b'Supervisory Performance & Analytics', resp_analytics.data)

        # Update profile and toggle availability to ON_LEAVE
        resp_prof = self.client.post('/mentor/profile', data={
            'phone': '+91 99999 11111',
            'skills': 'Kubernetes, Terraform, AWS, Golang',
            'availability_status': 'ON_LEAVE'
        }, follow_redirects=True)

        self.assertEqual(resp_prof.status_code, 200)
        m = User.query.get(self.mentor1.id)
        self.assertEqual(m.phone, '+91 99999 11111')
        self.assertEqual(m.availability_status, 'ON_LEAVE')


if __name__ == '__main__':
    unittest.main()
