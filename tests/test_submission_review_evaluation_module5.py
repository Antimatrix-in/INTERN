"""ANTI-MATRIX Phase 2 — Module 5 Test Suite
Advanced Task Assignment, Submission Review & Evaluation System.
"""

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
from app.services.submission_workflow_service import SubmissionWorkflowService
from tests.test_config import TestConfig


class TestSubmissionReviewEvaluationModule5(unittest.TestCase):
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

        # Ensure student has 2 weekly milestones on assignment
        m1 = self.assignment.weekly_milestones.filter_by(week_number=1).first()
        if not m1:
            m1 = WeeklyMilestone(
                assignment_id=self.assignment.id,
                week_number=1,
                title='Week 1: Setup & Foundations',
                objective='Initialize repo and environment',
                status='AVAILABLE',
                due_at=datetime.utcnow() + timedelta(days=7)
            )
            db.session.add(m1)
        else:
            m1.status = 'AVAILABLE'

        m2 = self.assignment.weekly_milestones.filter_by(week_number=2).first()
        if not m2:
            m2 = WeeklyMilestone(
                assignment_id=self.assignment.id,
                week_number=2,
                title='Week 2: Database & Core APIs',
                objective='Implement database models and endpoints',
                status='LOCKED',
                due_at=None
            )
            db.session.add(m2)
        else:
            m2.status = 'LOCKED'

        db.session.commit()
        self.milestone1 = m1
        self.milestone2 = m2

        # Create or update sample task on milestone 1 with technical specs
        t1 = self.milestone1.tasks.first()
        if not t1:
            t1 = WeeklyTask(
                milestone_id=self.milestone1.id,
                task_text='Configure Flask application factory',
                description='Create create_app() function with configs',
                technical_requirements='Support TestConfig and DevelopmentConfig',
                priority='High',
                status='ASSIGNED',
                is_completed=False
            )
            db.session.add(t1)
        else:
            t1.technical_requirements = 'Support TestConfig and DevelopmentConfig'
            t1.priority = 'High'
            t1.status = 'ASSIGNED'
            t1.is_completed = False

        db.session.commit()
        self.task1 = t1

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

    # ── Test 1: Evaluation Rubric Model & Strict 100% Validation ─────────────
    def test_rubric_model_and_strict_100_percent_validation(self):
        # 1. Valid rubric summing to 100%
        valid_rubric = EvaluationRubric(
            name='Balanced 100%',
            weight_technical=25,
            weight_code_quality=25,
            weight_functionality=25,
            weight_documentation=15,
            weight_presentation=10,
            is_active=True
        )
        self.assertTrue(valid_rubric.is_valid_100)
        self.assertEqual(valid_rubric.total_weight, 100)

        # Score calculation test
        calculated = valid_rubric.calculate_score(100, 100, 100, 100, 100)
        self.assertEqual(calculated, 100.0)

        calculated_mixed = valid_rubric.calculate_score(80, 90, 100, 70, 80)
        # 80*0.25 + 90*0.25 + 100*0.25 + 70*0.15 + 80*0.10 = 20 + 22.5 + 25 + 10.5 + 8 = 86.0
        self.assertAlmostEqual(calculated_mixed, 86.0, places=1)

        # 2. Invalid rubric not summing to 100%
        invalid_rubric = EvaluationRubric(
            name='Invalid 90%',
            weight_technical=20,
            weight_code_quality=20,
            weight_functionality=20,
            weight_documentation=15,
            weight_presentation=15
        )
        self.assertFalse(invalid_rubric.is_valid_100)
        self.assertEqual(invalid_rubric.total_weight, 90)

        # 3. EvaluationRubric.get_active() should seed or retrieve active
        active = EvaluationRubric.get_active()
        self.assertIsNotNone(active)
        self.assertTrue(active.is_valid_100)

    # ── Test 2: Admin Rubric Management HTTP Interface ────────────────────────
    def test_admin_rubrics_configuration_route(self):
        self.login_admin()

        # GET /admin/rubrics
        res = self.client.get('/admin/rubrics')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Evaluation Rubric', res.data)

        # POST with invalid weight sum (sum = 80%) should fail
        res_invalid = self.client.post('/admin/rubrics', data={
            'name': 'Invalid Rubric',
            'weight_technical': '20',
            'weight_code_quality': '20',
            'weight_functionality': '20',
            'weight_documentation': '10',
            'weight_presentation': '10'
        }, follow_redirects=True)
        self.assertIn(b'must equal exactly 100%', res_invalid.data)

        # POST with valid 100% weight sum should succeed
        res_valid = self.client.post('/admin/rubrics', data={
            'name': 'Custom Code Heavy',
            'weight_technical': '30',
            'weight_code_quality': '30',
            'weight_functionality': '20',
            'weight_documentation': '10',
            'weight_presentation': '10',
            'is_active': 'true'
        }, follow_redirects=True)
        self.assertEqual(res_valid.status_code, 200)
        self.assertIn(b'updated successfully', res_valid.data)

        active = EvaluationRubric.get_active()
        self.assertEqual(active.weight_technical, 30)
        self.assertEqual(active.weight_code_quality, 30)

    # ── Test 3: Task Management (Create, Edit, Delete & Broadcast) ───────────
    def test_task_management_and_project_broadcast(self):
        self.login_mentor(self.mentor1)

        # Mentor creates task on milestone 1
        res = self.client.post(f'/mentor/milestones/{self.milestone1.id}/tasks/create', data={
            'title': 'Write Alembic migration script',
            'description': 'Add upgrade and downgrade functions',
            'technical_requirements': 'Execute migration cleanly without lock contention',
            'priority': 'High',
            'estimated_hours': '3'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        created_task = WeeklyTask.query.filter_by(milestone_id=self.milestone1.id, task_text='Write Alembic migration script').first()
        self.assertIsNotNone(created_task)
        self.assertEqual(created_task.priority, 'High')
        self.assertEqual(created_task.technical_requirements, 'Execute migration cleanly without lock contention')

        # Mentor edits task
        res_edit = self.client.post(f'/mentor/milestones/tasks/{created_task.id}/edit', data={
            'title': 'Write Alembic migration script (Updated)',
            'description': 'Updated description',
            'priority': 'Medium',
            'status': 'IN_PROGRESS'
        }, follow_redirects=True)
        self.assertEqual(res_edit.status_code, 200)
        db.session.refresh(created_task)
        self.assertEqual(created_task.title, 'Write Alembic migration script (Updated)')
        self.assertEqual(created_task.priority, 'Medium')

        # Test Broadcast Task across project
        count, err = SubmissionWorkflowService.broadcast_task_to_project(
            project_id=self.project.id,
            task_title='Broadcasted Security Audit Task',
            description='Audit all endpoints for SQL injection',
            priority='High',
            technical_requirements='Check parameterized queries',
            actor=self.admin
        )
        self.assertIsNone(err)
        self.assertGreaterEqual(count, 1)

        broadcasted = WeeklyTask.query.filter_by(
            milestone_id=self.milestone1.id,
            task_text='Broadcasted Security Audit Task'
        ).first()
        self.assertIsNotNone(broadcasted)
        self.assertEqual(broadcasted.priority, 'High')

        # Verify notification was sent to student user
        notif = Notification.query.filter_by(user_id=self.student_user.id).order_by(Notification.id.desc()).first()
        self.assertIsNotNone(notif)
        self.assertIn('Broadcasted Security Audit Task', notif.message)

    # ── Test 4: Intern Workspace, Task Toggle & Detail Page ──────────────────
    def test_intern_workspace_task_toggle_and_detail(self):
        self.login_student()

        # 1. View week detail page
        res = self.client.get(f'/week/{self.milestone1.id}')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Evaluation Rubric', res.data)
        self.assertIn(b'Implementation Summary', res.data)

        # 2. View task detail page
        res_task = self.client.get(f'/week/{self.milestone1.id}/task/{self.task1.id}')
        self.assertEqual(res_task.status_code, 200)
        self.assertIn(b'Task Objectives & Instructions', res_task.data)
        self.assertIn(b'Technical Specifications', res_task.data)

        # 3. Toggle task completion
        self.task1.is_completed = False
        db.session.commit()
        res_toggle = self.client.post(
            f'/week/{self.milestone1.id}/task/{self.task1.id}/toggle',
            headers={'Accept': 'application/json'}
        )
        self.assertEqual(res_toggle.status_code, 200)
        data = res_toggle.get_json()
        self.assertTrue(data['is_completed'])

        db.session.refresh(self.task1)
        self.assertTrue(self.task1.is_completed)
        self.assertEqual(self.task1.status, 'COMPLETED')

        # Milestone should have transitioned to IN_PROGRESS
        db.session.refresh(self.milestone1)
        self.assertEqual(self.milestone1.status, 'IN_PROGRESS')

    # ── Test 5: Intern Milestone Submission & State Transition ───────────────
    def test_intern_milestone_submission_workflow(self):
        self.login_student()

        # Mark all tasks in milestone1 completed first
        for t in self.milestone1.tasks.all():
            t.is_completed = True
            t.status = 'COMPLETED'
        self.milestone1.status = 'IN_PROGRESS'
        if hasattr(self.milestone1, '_preloaded_completed_tasks_count'):
            delattr(self.milestone1, '_preloaded_completed_tasks_count')
        if hasattr(self.milestone1, '_preloaded_total_tasks_count'):
            delattr(self.milestone1, '_preloaded_total_tasks_count')
        db.session.commit()

        # Submit milestone with valid github, drive, and implementation summary
        res = self.client.post(f'/submissions/submit/{self.milestone1.id}', data={
            'github_url': 'https://github.com/student/anti-matrix-module5',
            'demo_video_url': 'https://drive.google.com/file/d/123456/view',
            'implementation_summary': 'Built application factory, integrated database tables and completed test assertions.',
            'submission_notes': 'All unit tests passing.'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        db.session.refresh(self.milestone1)
        self.assertEqual(self.milestone1.status, 'UNDER_REVIEW')

        sub = WeeklySubmission.query.filter_by(milestone_id=self.milestone1.id).order_by(WeeklySubmission.id.desc()).first()
        self.assertIsNotNone(sub)
        self.assertEqual(sub.github_url, 'https://github.com/student/anti-matrix-module5')
        self.assertIn('Built application factory', sub.implementation_summary)

        # Notification should have been sent to mentor1
        mentor_notif = Notification.query.filter_by(user_id=self.mentor1.id).order_by(Notification.id.desc()).first()
        self.assertIsNotNone(mentor_notif)
        self.assertIn('submitted', mentor_notif.message.lower())

    # ── Test 6: Submission State Machine Guardrails ──────────────────────────
    def test_submission_state_machine_validation(self):
        # Valid transitions
        v1, _ = SubmissionWorkflowService.validate_milestone_transition('AVAILABLE', 'IN_PROGRESS')
        self.assertTrue(v1)
        v2, _ = SubmissionWorkflowService.validate_milestone_transition('IN_PROGRESS', 'UNDER_REVIEW')
        self.assertTrue(v2)
        v3, _ = SubmissionWorkflowService.validate_milestone_transition('UNDER_REVIEW', 'APPROVED')
        self.assertTrue(v3)
        v4, _ = SubmissionWorkflowService.validate_milestone_transition('UNDER_REVIEW', 'REDO_REQUIRED')
        self.assertTrue(v4)
        v5, _ = SubmissionWorkflowService.validate_milestone_transition('APPROVED', 'COMPLETED')
        self.assertTrue(v5)
        v6, _ = SubmissionWorkflowService.validate_milestone_transition('REDO_REQUIRED', 'IN_PROGRESS')
        self.assertTrue(v6)

        # Blocked / Invalid transitions
        inv1, _ = SubmissionWorkflowService.validate_milestone_transition('LOCKED', 'COMPLETED')
        self.assertFalse(inv1)
        inv2, _ = SubmissionWorkflowService.validate_milestone_transition('COMPLETED', 'IN_PROGRESS')
        self.assertFalse(inv2)
        inv3, _ = SubmissionWorkflowService.validate_milestone_transition('UNDER_REVIEW', 'ASSIGNED')
        self.assertFalse(inv3)

    # ── Test 7: Mentor IDOR Protection ───────────────────────────────────────
    def test_mentor_idor_protection(self):
        # Milestone 1 is in UNDER_REVIEW
        self.milestone1.status = 'UNDER_REVIEW'
        sub = WeeklySubmission(
            milestone_id=self.milestone1.id,
            student_id=self.student.id,
            github_url='https://github.com/student/test',
            demo_video_url='https://drive.google.com/file/d/test/view',
            implementation_summary='Test summary',
            status='UNDER_REVIEW'
        )
        db.session.add(sub)
        db.session.commit()

        # Login as mentor2 (NOT the student's assigned mentor)
        self.login_mentor(self.mentor2)

        # mentor2 attempts to review student1's submission -> should receive 403 Forbidden
        res = self.client.get(f'/mentor/submissions/{sub.id}/review')
        self.assertEqual(res.status_code, 403)

        # mentor2 attempts to approve student1's submission -> should receive 403 Forbidden
        res_approve = self.client.post(f'/mentor/submissions/{sub.id}/approve', data={
            'technical_score': '85',
            'code_quality_score': '85',
            'functionality_score': '85',
            'documentation_score': '85',
            'presentation_score': '85',
            'feedback': 'Unauthorized approval attempt'
        })
        self.assertEqual(res_approve.status_code, 403)

    # ── Test 8: Mentor 5-Criteria Evaluation Approval & Week Unlocking ───────
    def test_mentor_rubric_evaluation_approval_and_advancement(self):
        self.milestone1.status = 'UNDER_REVIEW'
        sub = WeeklySubmission(
            milestone_id=self.milestone1.id,
            student_id=self.student.id,
            github_url='https://github.com/student/valid-test',
            demo_video_url='https://drive.google.com/file/d/valid-test/view',
            implementation_summary='Completed all architectural requirements cleanly.',
            status='UNDER_REVIEW'
        )
        db.session.add(sub)
        db.session.commit()

        # Login as assigned mentor1
        self.login_mentor(self.mentor1)

        # Approve with 5 criteria scores (90 each -> deterministic total 90)
        res = self.client.post(f'/mentor/submissions/{sub.id}/approve', data={
            'technical_score': '90',
            'code_quality_score': '90',
            'functionality_score': '90',
            'documentation_score': '90',
            'presentation_score': '90',
            'feedback': 'Outstanding implementation and clean tests.'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        # Milestone 1 should now be COMPLETED
        db.session.refresh(self.milestone1)
        self.assertEqual(self.milestone1.status, 'COMPLETED')

        # Verify Evaluation record with rubric snapshot
        eval_record = Evaluation.query.filter_by(milestone_id=self.milestone1.id).order_by(Evaluation.id.desc()).first()
        self.assertIsNotNone(eval_record)
        self.assertEqual(eval_record.total_score, 90)
        self.assertIsNotNone(eval_record.rubric_snapshot_json)
        self.assertIn('weight_technical', eval_record.rubric_snapshot_json)

        # Milestone 2 should now be UNLOCKED (AVAILABLE) with 7-day deadline
        db.session.refresh(self.milestone2)
        self.assertEqual(self.milestone2.status, 'AVAILABLE')
        self.assertIsNotNone(self.milestone2.due_at)

        # Verify AuditLog created
        audit = AuditLog.query.filter_by(
            action='SUBMISSION_EVALUATION_APPROVED',
            target_entity='weekly_submissions',
            target_id=str(sub.id)
        ).first()
        self.assertIsNotNone(audit)

    # ── Test 9: Mentor Redo / Revision Workflow ──────────────────────────────
    def test_mentor_redo_revision_workflow(self):
        self.milestone1.status = 'UNDER_REVIEW'
        sub = WeeklySubmission(
            milestone_id=self.milestone1.id,
            student_id=self.student.id,
            github_url='https://github.com/student/needs-work',
            demo_video_url='https://drive.google.com/file/d/needs-work/view',
            implementation_summary='Draft implementation.',
            status='UNDER_REVIEW'
        )
        db.session.add(sub)
        db.session.commit()

        # Login as assigned mentor1
        self.login_mentor(self.mentor1)

        # Request redo
        res = self.client.post(f'/mentor/submissions/{sub.id}/redo', data={
            'feedback': 'Please add integration test cases for exception handling.'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        # Milestone 1 should now be REDO_REQUIRED
        db.session.refresh(self.milestone1)
        self.assertEqual(self.milestone1.status, 'REDO_REQUIRED')

        # Milestone 2 should remain LOCKED
        db.session.refresh(self.milestone2)
        self.assertEqual(self.milestone2.status, 'LOCKED')

        # Intern should receive revision notification
        notif = Notification.query.filter_by(user_id=self.student_user.id).order_by(Notification.id.desc()).first()
        self.assertIsNotNone(notif)
        self.assertIn('Revisions Requested', notif.title)
        self.assertIn('integration test cases', notif.message)

    # ── Test 10: Admin Submissions Review & Override ────────────────────────
    def test_admin_submission_review_and_actions(self):
        self.milestone1.status = 'UNDER_REVIEW'
        sub = WeeklySubmission(
            milestone_id=self.milestone1.id,
            student_id=self.student.id,
            github_url='https://github.com/student/admin-eval',
            demo_video_url='https://drive.google.com/file/d/admin-eval/view',
            implementation_summary='Admin evaluation test.',
            status='UNDER_REVIEW'
        )
        db.session.add(sub)
        db.session.commit()

        # Login as Admin
        self.login_admin()

        # Admin reviews submission page
        res = self.client.get(f'/admin/submissions/{sub.id}/review')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Evaluation Rubric', res.data)
        self.assertIn(b'Implementation Summary', res.data)

        # Admin approves submission (95 each -> deterministic total 95)
        res_approve = self.client.post(f'/admin/submissions/{sub.id}/proceed-next', data={
            'technical_score': '95',
            'code_quality_score': '95',
            'functionality_score': '95',
            'documentation_score': '95',
            'presentation_score': '95',
            'feedback': 'Admin approved directly.'
        }, follow_redirects=True)
        self.assertEqual(res_approve.status_code, 200)

        db.session.refresh(self.milestone1)
        self.assertEqual(self.milestone1.status, 'COMPLETED')

        eval_record = Evaluation.query.filter_by(milestone_id=self.milestone1.id).order_by(Evaluation.id.desc()).first()
        self.assertIsNotNone(eval_record)
        self.assertEqual(eval_record.total_score, 95)


if __name__ == '__main__':
    unittest.main()
