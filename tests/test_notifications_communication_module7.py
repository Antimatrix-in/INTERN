"""ANTI-MATRIX Phase 2 — Module 7 Test Suite
Automated Notifications & Communication System.
"""

import json
from datetime import datetime, timedelta
import unittest
from flask import url_for
from app import create_app, db
from app.models import (
    User, Student, Internship, Project, ProjectAssignment,
    WeeklyMilestone, WeeklyTask, WeeklySubmission, Evaluation,
    Notification, NotificationPreference, Announcement, AnnouncementRead, AuditLog
)
from app.seed import seed_initial_data
from app.services.notification_service import NotificationService
from app.services.submission_workflow_service import SubmissionWorkflowService
from app.services.project_allocation_engine import ProjectAllocationEngine
from tests.test_config import TestConfig


class TestNotificationsCommunicationModule7(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.app.config['TESTING'] = True
        self.app_context = self.app.app_context()
        self.app_context.push()

        db.drop_all()
        db.create_all()
        seed_initial_data()

        self.client = self.app.test_client()

        # Reset in-memory test email outbox
        NotificationService.email_outbox = []

        # Clean notifications table for test determinism
        Notification.query.delete()
        db.session.commit()

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

        # Project Assignment & Milestones
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

        m1 = self.assignment.weekly_milestones.filter_by(week_number=1).first()
        if not m1:
            m1 = WeeklyMilestone(
                assignment_id=self.assignment.id,
                week_number=1,
                title='Week 1: Foundations',
                objective='Environment and setup',
                status='AVAILABLE',
                due_at=datetime.utcnow() + timedelta(days=7)
            )
            db.session.add(m1)
        else:
            m1.status = 'AVAILABLE'
            m1.due_at = datetime.utcnow() + timedelta(days=7)

        db.session.commit()
        self.milestone1 = m1

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

    # ─── 1. In-Portal Notification Generation Across All 10 Event Types ───────

    def test_in_portal_notification_generation_all_events(self):
        """Verify distinct in-portal notification generation for all 10 required events."""
        u_id = self.student_user.id
        m = self.milestone1

        # 1. Task Assigned
        n1 = NotificationService.notify_task_assigned(
            student_user_id=u_id,
            task_title='Build REST Auth Endpoints',
            milestone_id=m.id,
            week_number=m.week_number,
            milestone_title=m.title,
            actor_name='Lead Mentor'
        )
        self.assertIsNotNone(n1)
        self.assertEqual(n1.category, 'TASK')
        self.assertIn('Build REST Auth Endpoints', n1.message)

        # 2. Project Allocated
        n2 = NotificationService.notify_project_allocated(
            student_user_id=u_id,
            project_title='Enterprise Cloud Portal',
            project_id=self.project.id,
            tech_stack='Python, Flask, Docker',
            mentor_name=self.mentor1.full_name
        )
        self.assertEqual(n2.category, 'ALLOCATION')
        self.assertEqual(n2.type, 'SUCCESS')

        # 3. Mentor Assigned
        n3 = NotificationService.notify_mentor_assigned(
            student_user_id=u_id,
            mentor_name=self.mentor1.full_name,
            mentor_email=self.mentor1.email,
            mentor_dept=self.mentor1.department
        )
        self.assertEqual(n3.category, 'ALLOCATION')
        self.assertIn(self.mentor1.full_name, n3.message)

        # 4. Deadline Reminder (Upcoming)
        n4 = NotificationService.notify_deadline_reminder(
            student_user_id=u_id,
            milestone_title=m.title,
            milestone_id=m.id,
            week_number=1,
            due_date_str='Tomorrow 18:00 UTC',
            days_remaining=1
        )
        self.assertEqual(n4.category, 'DEADLINE')
        self.assertEqual(n4.type, 'WARNING')

        # 5. Overdue Task Alert
        n5 = NotificationService.notify_overdue_task(
            student_user_id=u_id,
            milestone_title=m.title,
            milestone_id=m.id,
            week_number=1,
            due_date_str='Yesterday 18:00 UTC'
        )
        self.assertEqual(n5.category, 'DEADLINE')
        self.assertEqual(n5.type, 'ACTION_REQUIRED')

        # 6. Submission Received (Sent to mentor)
        n6 = NotificationService.notify_submission_received(
            mentor_user_id=self.mentor1.id,
            student_name=self.student_user.full_name,
            milestone_title=m.title,
            milestone_id=m.id,
            submission_id=101,
            week_number=1
        )
        self.assertIsNotNone(n6)
        self.assertEqual(n6.user_id, self.mentor1.id)
        self.assertEqual(n6.category, 'SUBMISSION')

        # 7. Submission Approved
        n7 = NotificationService.notify_submission_approved(
            student_user_id=u_id,
            milestone_title=m.title,
            milestone_id=m.id,
            week_number=1,
            total_score=92.5,
            mentor_name=self.mentor1.full_name
        )
        self.assertEqual(n7.category, 'SUBMISSION')
        self.assertEqual(n7.type, 'SUCCESS')
        self.assertIn('92.5', n7.title)

        # 8. Revision Requested
        n8 = NotificationService.notify_revision_requested(
            student_user_id=u_id,
            milestone_title=m.title,
            milestone_id=m.id,
            week_number=1,
            feedback_notes='Please add integration tests for API endpoints',
            mentor_name=self.mentor1.full_name
        )
        self.assertEqual(n8.category, 'SUBMISSION')
        self.assertEqual(n8.type, 'WARNING')

        # 9. Mentor Feedback Recorded
        n9 = NotificationService.notify_mentor_feedback(
            student_user_id=u_id,
            milestone_title=m.title,
            milestone_id=m.id,
            week_number=1,
            mentor_name=self.mentor1.full_name,
            feedback_summary='Strong code structure, documentation needs slight polish',
            score=88.0
        )
        self.assertEqual(n9.category, 'EVALUATION')

        # 10. Internship Completed
        n10 = NotificationService.notify_internship_completed(
            student_user_id=u_id,
            student_name=self.student_user.full_name,
            program_title='Full-Stack Python Track',
            final_score=94.0
        )
        self.assertEqual(n10.category, 'COMPLETION')
        self.assertEqual(n10.type, 'SUCCESS')

        # Verify DB counts for student user
        student_notifs = Notification.query.filter_by(user_id=u_id).all()
        self.assertEqual(len(student_notifs), 9) # 9 student alerts
        for notif in student_notifs:
            self.assertFalse(notif.is_read)

    # ─── 2. Category Filtering & Unread Counts ────────────────────────────────

    def test_category_filtering_and_unread_counts(self):
        """Verify get_user_notifications filters properly by category and calculates unread count."""
        u_id = self.student_user.id

        NotificationService.notify_task_assigned(u_id, 'Task A', 1, 1, 'Milestone 1')
        NotificationService.notify_deadline_reminder(u_id, 'Milestone 1', 1, 1, 'Tomorrow', 1)
        NotificationService.notify_submission_approved(u_id, 'Milestone 1', 1, 1, 90.0)

        # All notifications
        all_notifs = NotificationService.get_user_notifications(u_id, category='ALL')
        self.assertEqual(len(all_notifs), 3)

        # Filter by TASK
        task_notifs = NotificationService.get_user_notifications(u_id, category='TASK')
        self.assertEqual(len(task_notifs), 1)
        self.assertEqual(task_notifs[0].category, 'TASK')

        # Filter by DEADLINE
        deadline_notifs = NotificationService.get_user_notifications(u_id, category='DEADLINE')
        self.assertEqual(len(deadline_notifs), 1)

        # Filter by SUBMISSION
        submission_notifs = NotificationService.get_user_notifications(u_id, category='SUBMISSION')
        self.assertEqual(len(submission_notifs), 1)

        # Check user unread count property
        self.assertEqual(self.student_user.unread_notifications_count, 3)

    # ─── 3. IDOR Protection & Mark As Read Mutations ──────────────────────────

    def test_idor_protection_and_mark_as_read(self):
        """Ensure users can only mark their own notifications as read and IDOR is blocked."""
        student_u_id = self.student_user.id
        mentor_u_id = self.mentor1.id

        notif_student = NotificationService.notify_task_assigned(student_u_id, 'Task Secret', 1, 1, 'M1')
        notif_mentor = NotificationService.notify_submission_received(mentor_u_id, 'Intern', 'M1', 1, 1, 1)

        # Attempt IDOR: Mentor tries to mark Student's notification as read
        idor_result = NotificationService.mark_as_read(notif_student.id, user_id=mentor_u_id)
        self.assertFalse(idor_result)
        db.session.refresh(notif_student)
        self.assertFalse(notif_student.is_read)

        # Legitimate: Student marks their own notification as read
        own_result = NotificationService.mark_as_read(notif_student.id, user_id=student_u_id)
        self.assertTrue(own_result)
        db.session.refresh(notif_student)
        self.assertTrue(notif_student.is_read)

        # Mark all as read for mentor
        NotificationService.mark_all_as_read(user_id=mentor_u_id)
        db.session.refresh(notif_mentor)
        self.assertTrue(notif_mentor.is_read)

    # ─── 4. Automated Deadline Reminders Engine ───────────────────────────────

    def test_automated_deadline_reminders_engine(self):
        """Verify automated deadline runner generates reminders for T-3d, T-1d, T-0d, and overdue."""
        now = datetime.utcnow()

        # Milestone A: Overdue by 1 day
        mA = WeeklyMilestone(
            assignment_id=self.assignment.id,
            week_number=2,
            title='Overdue Milestone',
            objective='Overdue objective',
            status='AVAILABLE',
            due_at=now - timedelta(days=1)
        )

        # Milestone B: Due Today (in 8 hours)
        mB = WeeklyMilestone(
            assignment_id=self.assignment.id,
            week_number=3,
            title='Due Today Milestone',
            objective='Due today objective',
            status='AVAILABLE',
            due_at=now + timedelta(hours=8)
        )

        # Milestone C: Due in 1 day (in 30 hours)
        mC = WeeklyMilestone(
            assignment_id=self.assignment.id,
            week_number=4,
            title='Due Tomorrow Milestone',
            objective='Due tomorrow objective',
            status='AVAILABLE',
            due_at=now + timedelta(hours=30)
        )

        # Milestone D: Due in 3 days (in 65 hours)
        mD = WeeklyMilestone(
            assignment_id=self.assignment.id,
            week_number=5,
            title='Due in 3 Days Milestone',
            objective='Due in 3 days objective',
            status='AVAILABLE',
            due_at=now + timedelta(hours=65)
        )

        db.session.add_all([mA, mB, mC, mD])
        db.session.commit()

        # Run automated deadline check
        summary = NotificationService.check_and_send_deadline_reminders()

        self.assertGreaterEqual(summary['checked_milestones'], 4)
        self.assertGreaterEqual(summary['overdue_sent'], 1)
        self.assertGreaterEqual(summary['reminders_sent'], 3)

        # Verify AuditLog audit records were created for deduplication
        audit_records = AuditLog.query.filter(AuditLog.action.like('DEADLINE_REMINDER_%')).all()
        self.assertGreaterEqual(len(audit_records), 4)

    # ─── 5. Deadline Reminder Deduplication (Idempotency) ─────────────────────

    def test_deadline_reminder_idempotent_deduplication(self):
        """Verify deadline reminders are strictly deduplicated and do not spam interns."""
        now = datetime.utcnow()

        m_test = WeeklyMilestone(
            assignment_id=self.assignment.id,
            week_number=6,
            title='Test Deduplication Milestone',
            objective='Test deduplication',
            status='AVAILABLE',
            due_at=now + timedelta(hours=10) # DUE_TODAY stage
        )
        db.session.add(m_test)
        db.session.commit()

        # First run: sends reminder
        summary1 = NotificationService.check_and_send_deadline_reminders()
        count1 = Notification.query.filter_by(user_id=self.student_user.id, category='DEADLINE').count()
        self.assertGreaterEqual(count1, 1)

        # Second run: should be 100% idempotent and send 0 additional reminders
        summary2 = NotificationService.check_and_send_deadline_reminders()
        self.assertEqual(summary2['reminders_sent'], 0)
        self.assertEqual(summary2['overdue_sent'], 0)

        count2 = Notification.query.filter_by(user_id=self.student_user.id, category='DEADLINE').count()
        self.assertEqual(count1, count2)

    # ─── 6. Skip Submitted and Completed Milestones ───────────────────────────

    def test_deadline_reminder_skips_submitted_and_completed_milestones(self):
        """Milestones that are completed or already submitted must never receive deadline reminders."""
        now = datetime.utcnow()

        # Milestone 7: Completed status
        m_completed = WeeklyMilestone(
            assignment_id=self.assignment.id,
            week_number=7,
            title='Completed Milestone',
            objective='Completed',
            status='COMPLETED',
            due_at=now + timedelta(hours=6)
        )

        # Milestone 8: Available status, but intern has already submitted deliverable
        m_submitted = WeeklyMilestone(
            assignment_id=self.assignment.id,
            week_number=8,
            title='Submitted Milestone',
            objective='Submitted',
            status='AVAILABLE',
            due_at=now + timedelta(hours=6)
        )
        db.session.add_all([m_completed, m_submitted])
        db.session.commit()

        # Add submission for milestone 8
        sub = WeeklySubmission(
            milestone_id=m_submitted.id,
            student_id=self.student.id,
            github_url='https://github.com/intern/deliverables-week8',
            demo_video_url='https://drive.google.com/file/d/test12345/view',
            implementation_summary='Delivered all tasks',
            status='SUBMITTED'
        )
        db.session.add(sub)
        db.session.commit()

        # Clear notifications for student
        Notification.query.filter_by(user_id=self.student_user.id).delete()
        db.session.commit()

        summary = NotificationService.check_and_send_deadline_reminders()
        self.assertGreaterEqual(summary['skipped_submitted_or_done'], 1)

        # Verify no deadline reminders sent for milestone 7 or 8
        m7_notifs = Notification.query.filter(
            Notification.user_id == self.student_user.id,
            Notification.link == f"/week/{m_completed.id}"
        ).all()
        m8_notifs = Notification.query.filter(
            Notification.user_id == self.student_user.id,
            Notification.link == f"/week/{m_submitted.id}"
        ).all()

        self.assertEqual(len(m7_notifs), 0)
        self.assertEqual(len(m8_notifs), 0)

    # ─── 7. Announcements Broadcast & Read Tracking ───────────────────────────

    def test_announcements_broadcast_and_read_tracking(self):
        """Verify announcement broadcasting, role targeting, and acknowledgment tracking."""
        # 1. Admin broadcasts to STUDENT role only
        announcement = NotificationService.create_announcement(
            author_id=self.admin.id,
            title='Mid-Term Technical Review Webinar',
            content='Join the live Zoom sync on Friday at 3:00 PM IST.',
            target_role='STUDENT',
            priority='NORMAL'
        )
        self.assertIsNotNone(announcement.id)

        # Student should receive announcement notification
        student_notif = Notification.query.filter_by(
            user_id=self.student_user.id,
            category='ANNOUNCEMENT'
        ).first()
        self.assertIsNotNone(student_notif)

        # Mentor should NOT receive announcement notification (targeted to STUDENT only)
        mentor_notif = Notification.query.filter_by(
            user_id=self.mentor1.id,
            category='ANNOUNCEMENT'
        ).first()
        self.assertIsNone(mentor_notif)

        # 2. Read / Acknowledgment tracking
        self.assertFalse(announcement.is_read_by(self.student_user.id))
        self.assertEqual(announcement.read_count, 0)

        # Acknowledge announcement
        NotificationService.mark_announcement_read(announcement.id, self.student_user.id)
        self.assertTrue(announcement.is_read_by(self.student_user.id))
        self.assertEqual(announcement.read_count, 1)

        # 3. Mentor broadcast to assigned mentees
        mentor_broadcast = Announcement(
            author_id=self.mentor1.id,
            title='Technical Check-in for Engineering Mentees',
            content='Please ensure Docker Compose configurations are included in repo.',
            target_role='STUDENT',
            priority='URGENT'
        )
        db.session.add(mentor_broadcast)
        db.session.commit()

        NotificationService.notify_announcement(
            recipient_user_id=self.student_user.id,
            announcement_id=mentor_broadcast.id,
            announcement_title=mentor_broadcast.title,
            content=mentor_broadcast.content,
            author_name=self.mentor1.full_name,
            priority='URGENT',
            target_role='STUDENT'
        )

        mentee_alert = Notification.query.filter_by(
            user_id=self.student_user.id,
            type='WARNING'
        ).first()
        self.assertIsNotNone(mentee_alert)
        self.assertIn('URGENT', mentee_alert.title)

    # ─── 8. User Notification Preferences Enforcement ─────────────────────────

    def test_user_notification_preferences_enforcement(self):
        """Verify user preferences toggle email and in-portal notifications."""
        u_id = self.student_user.id

        # Disable in-portal notifications
        NotificationService.update_preferences(u_id, in_portal_enabled=False, email_enabled=True)
        pref = NotificationService.get_or_create_preferences(u_id)
        self.assertFalse(pref.in_portal_enabled)

        # Clear notifications
        Notification.query.filter_by(user_id=u_id).delete()
        db.session.commit()

        # Dispatch regular task alert -> should NOT create in-portal notification
        n_suppressed = NotificationService.notify_task_assigned(u_id, 'Suppressed Task', 1, 1, 'M1')
        self.assertIsNone(n_suppressed)
        self.assertEqual(Notification.query.filter_by(user_id=u_id).count(), 0)

        # Dispatch critical security notification -> MUST bypass and still create notification
        n_security = NotificationService.send_notification(
            user_id=u_id,
            title='Security Alert: Password Changed',
            message='Your password was updated from a new IP.',
            category='SECURITY',
            notif_type='WARNING'
        )
        self.assertIsNotNone(n_security)
        self.assertEqual(Notification.query.filter_by(user_id=u_id).count(), 1)

        # Re-enable in-portal and disable email deadline reminders
        NotificationService.update_preferences(u_id, in_portal_enabled=True, deadline_reminders=False)
        NotificationService.email_outbox = []

        # Dispatch deadline reminder -> email should be suppressed
        NotificationService.notify_deadline_reminder(u_id, 'M1', 1, 1, 'Tomorrow', 1)
        self.assertEqual(len(NotificationService.email_outbox), 0)

    # ─── 9. Email Branding and Mock Outbox Verification ───────────────────────

    def test_email_branding_and_mock_outbox(self):
        """Verify email templates render with Anti-Matrix styling and plain text fallback."""
        NotificationService.email_outbox = []
        u_id = self.student_user.id

        # Ensure user has email enabled
        NotificationService.update_preferences(u_id, email_enabled=True, in_portal_enabled=True)

        NotificationService.notify_project_allocated(
            student_user_id=u_id,
            project_title='AI Analytics Core',
            project_id=self.project.id,
            tech_stack='Python, PyTorch',
            mentor_name=self.mentor1.full_name
        )

        self.assertEqual(len(NotificationService.email_outbox), 1)
        outbox_entry = NotificationService.email_outbox[0]

        self.assertEqual(outbox_entry['to'], self.student_user.email)
        self.assertIn('Project Allocated', outbox_entry['subject'])
        self.assertIn('ANTI', outbox_entry['html'])
        self.assertIn('#10B981', outbox_entry['html']) # Anti-Matrix Emerald brand color
        self.assertIsNotNone(outbox_entry['text'])
        self.assertIn('AI Analytics Core', outbox_entry['text'])

    # ─── 10. Fault Tolerance & Zero-Crash Guarantee on SMTP Error ─────────────

    def test_zero_crash_fault_tolerance_on_mail_error(self):
        """Verify SMTP failure does not break task assignments or submission evaluations."""
        u_id = self.student_user.id

        # Temporarily mock send_email to raise a severe socket exception
        original_send_email = NotificationService.send_email

        def broken_send_email(*args, **kwargs):
            raise ConnectionRefusedError("SMTP server connection dropped")

        NotificationService.send_email = broken_send_email

        try:
            # 1. Task assignment must succeed without crashing
            SubmissionWorkflowService.notify_task_assigned(
                student_user_id=u_id,
                milestone=self.milestone1,
                task_title='Resilient Task Test'
            )

            # 2. Evaluation approval must succeed without crashing
            SubmissionWorkflowService.notify_submission_approved(
                student_user_id=u_id,
                mentor_name=self.mentor1.full_name,
                milestone=self.milestone1,
                total_score=95.0
            )

            # 3. Project allocation must succeed without crashing
            student_notifs = Notification.query.filter_by(user_id=u_id).all()
            self.assertGreaterEqual(len(student_notifs), 1)

        finally:
            NotificationService.send_email = original_send_email

    # ─── 11. HTTP Endpoints and Access Control ────────────────────────────────

    def test_http_endpoints_and_access_control(self):
        """Verify HTTP routes, authorization boundaries, and JSON endpoints."""
        # 1. Student Notifications Feed
        self.login_student()

        res = self.client.get('/notifications')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Notification Center', res.data)

        # 2. JSON Unread Count Endpoint
        res_count = self.client.get('/notifications/api/unread-count')
        self.assertEqual(res_count.status_code, 200)
        data = json.loads(res_count.data)
        self.assertTrue(data['success'])
        self.assertIn('unread_count', data)

        # 3. Student attempting to access Admin Announcements -> 403 Forbidden
        res_admin = self.client.get('/admin/announcements')
        self.assertIn(res_admin.status_code, [302, 403])

        # 4. Admin Session Access
        self.login_admin()

        res_admin_ok = self.client.get('/admin/announcements')
        self.assertEqual(res_admin_ok.status_code, 200)
        self.assertIn(b'Broadcast &amp; Communication Center', res_admin_ok.data)

        # 5. Admin Trigger Deadline Check
        res_trigger = self.client.post('/admin/automation/deadline-check', headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertEqual(res_trigger.status_code, 200)
        summary_data = json.loads(res_trigger.data)
        self.assertTrue(summary_data['success'])
        self.assertIn('summary', summary_data)

        # 6. Mentor Announcements Hub
        self.login_mentor(self.mentor1)

        res_mentor = self.client.get('/mentor/announcements')
        self.assertEqual(res_mentor.status_code, 200)
        self.assertIn(b'Mentorship Broadcast Center', res_mentor.data)


if __name__ == '__main__':
    unittest.main()
