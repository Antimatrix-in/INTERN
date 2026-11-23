"""
ANTI-MATRIX Phase 2 — Module 9 Test Suite
Attendance, Leave & Internship Scheduling System.
"""

import io
import os
import json
from datetime import datetime, date, time, timezone, timedelta
import unittest
from flask import url_for
from app import create_app, db
from app.models import (
    User, Student, Internship, Project, ProjectAssignment,
    AttendanceRecord, LeaveRequest, AttendancePolicy, CompanyHoliday,
    AuditLog, Notification
)
from app.seed import seed_initial_data
from app.services.attendance_service import AttendanceService
from tests.test_config import TestConfig


class TestAttendanceLeaveModule9(unittest.TestCase):
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

        # Seeded admin user
        self.admin = User.query.filter_by(employee_id='admin').first() or User.query.filter_by(role='admin').first()

        # Seeded student 1 & active internship
        self.student1 = Student.query.filter_by(student_uid='AM-INT-2026-001').first()
        self.student1_user = self.student1.user
        self.internship1 = self.student1.active_internship

        # Mentor
        self.mentor_user = self.internship1.mentor if self.internship1 else None
        if not self.mentor_user:
            self.mentor_user = User.query.filter_by(role='mentor').first()

        # Seeded student 2
        self.student2 = Student.query.filter_by(student_uid='AM-INT-2026-002').first()
        if not self.student2:
            u2 = User(
                email='student2@test.com',
                full_name='Second Intern',
                role='student',
                employee_id='AM-INT-2026-002',
                is_active=True
            )
            u2.set_password('Student@2026Password!')
            db.session.add(u2)
            db.session.flush()

            self.student2 = Student(
                user_id=u2.id,
                student_uid='AM-INT-2026-002',
                college_id=self.student1.college_id,
                department_id=self.student1.department_id,
                roll_number='ROLL-002',
                degree='B.Tech',
                current_year='3rd Year',
                graduation_year='2027'
            )
            db.session.add(self.student2)
            db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Attendance Check-In / Check-Out Engine Tests
    # ──────────────────────────────────────────────────────────────────────────

    def test_on_time_check_in(self):
        """Validates that a check-in before the late threshold receives status PRESENT."""
        on_time_dt = datetime(2026, 3, 2, 9, 15, tzinfo=timezone.utc)
        record = AttendanceService.record_check_in(
            student_id=self.student1.id,
            internship_id=self.internship1.id,
            check_in_dt=on_time_dt,
            notes="Morning standup attendance"
        )
        self.assertIsNotNone(record)
        self.assertEqual(record.status, 'PRESENT')
        self.assertEqual(record.date, on_time_dt.date())
        self.assertIn("Morning standup", record.check_in_notes)

    def test_late_check_in(self):
        """Validates that a check-in after 09:30 UTC receives status LATE."""
        late_dt = datetime(2026, 3, 2, 9, 45, tzinfo=timezone.utc)
        record = AttendanceService.record_check_in(
            student_id=self.student1.id,
            internship_id=self.internship1.id,
            check_in_dt=late_dt,
            notes="Delayed transit"
        )
        self.assertEqual(record.status, 'LATE')

    def test_duplicate_check_in_prevention(self):
        """Ensures an intern cannot check in twice on the same calendar day."""
        dt1 = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
        AttendanceService.record_check_in(
            student_id=self.student1.id,
            internship_id=self.internship1.id,
            check_in_dt=dt1
        )
        # Attempt second check-in on same day
        dt2 = datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError) as cm:
            AttendanceService.record_check_in(
                student_id=self.student1.id,
                internship_id=self.internship1.id,
                check_in_dt=dt2
            )
        self.assertIn("already checked in", str(cm.exception).lower())

    def test_check_out_without_check_in(self):
        """Validates that checking out without a prior check-in is rejected."""
        out_dt = datetime(2026, 3, 2, 17, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError) as cm:
            AttendanceService.record_check_out(
                student_id=self.student1.id,
                check_out_dt=out_dt
            )
        self.assertIn("no check-in record found", str(cm.exception).lower())

    def test_full_day_check_out(self):
        """Validates full day shift duration calculation (8 hours worked)."""
        in_dt = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
        AttendanceService.record_check_in(
            student_id=self.student1.id,
            internship_id=self.internship1.id,
            check_in_dt=in_dt
        )
        out_dt = datetime(2026, 3, 2, 17, 0, tzinfo=timezone.utc)
        record = AttendanceService.record_check_out(
            student_id=self.student1.id,
            check_out_dt=out_dt
        )
        self.assertEqual(record.work_duration_minutes, 480)
        self.assertEqual(record.status, 'PRESENT')
        self.assertEqual(record.work_duration_formatted, "8h 0m")

    def test_half_day_rule_applied_on_early_departure(self):
        """Validates that departure before 4.0 hours threshold transitions status to HALF_DAY."""
        in_dt = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
        AttendanceService.record_check_in(
            student_id=self.student1.id,
            internship_id=self.internship1.id,
            check_in_dt=in_dt
        )
        # Leaving after 3 hours
        out_dt = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
        record = AttendanceService.record_check_out(
            student_id=self.student1.id,
            check_out_dt=out_dt
        )
        self.assertEqual(record.work_duration_minutes, 180)
        self.assertEqual(record.status, 'HALF_DAY')

    def test_duplicate_check_out_prevention(self):
        """Ensures an intern cannot check out twice on the same day."""
        in_dt = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
        AttendanceService.record_check_in(
            student_id=self.student1.id,
            internship_id=self.internship1.id,
            check_in_dt=in_dt
        )
        out_dt1 = datetime(2026, 3, 2, 17, 0, tzinfo=timezone.utc)
        AttendanceService.record_check_out(student_id=self.student1.id, check_out_dt=out_dt1)

        out_dt2 = datetime(2026, 3, 2, 18, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError) as cm:
            AttendanceService.record_check_out(student_id=self.student1.id, check_out_dt=out_dt2)
        self.assertIn("already checked out", str(cm.exception).lower())

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Administrative Regularization & Retroactive Entry Tests
    # ──────────────────────────────────────────────────────────────────────────

    def test_admin_correct_attendance_with_mandatory_reason(self):
        """Validates administrative regularization with mandatory audit reason."""
        in_dt = datetime(2026, 3, 2, 9, 45, tzinfo=timezone.utc)
        record = AttendanceService.record_check_in(
            student_id=self.student1.id,
            internship_id=self.internship1.id,
            check_in_dt=in_dt
        )
        self.assertEqual(record.status, 'LATE')

        # Admin regularizes with audit reason
        updated = AttendanceService.admin_correct_attendance(
            record_id=record.id,
            admin_user_id=self.admin.id,
            new_status='PRESENT',
            reason="Verified network outage delayed check-in submission."
        )
        self.assertEqual(updated.status, 'PRESENT')
        self.assertTrue(updated.is_corrected)
        self.assertEqual(updated.corrected_by, self.admin.id)
        self.assertIn("network outage", updated.correction_reason)

        # Verify audit log
        audit = AuditLog.query.filter_by(action='ATTENDANCE_CORRECTED').first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor_id, self.admin.id)

    def test_admin_correct_attendance_fails_without_reason(self):
        """Enforces mandatory reason requirement on administrative corrections."""
        in_dt = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
        record = AttendanceService.record_check_in(
            student_id=self.student1.id,
            internship_id=self.internship1.id,
            check_in_dt=in_dt
        )
        with self.assertRaises(ValueError) as cm:
            AttendanceService.admin_correct_attendance(
                record_id=record.id,
                admin_user_id=self.admin.id,
                new_status='HALF_DAY',
                reason=""
            )
        self.assertIn("reason is mandatory", str(cm.exception).lower())

    def test_admin_retroactive_attendance_entry(self):
        """Tests admin creating a retroactive attendance record for a missed day."""
        past_date = date(2026, 2, 20)
        check_in = datetime(2026, 2, 20, 9, 0)
        check_out = datetime(2026, 2, 20, 17, 0)
        record = AttendanceService.admin_record_retroactive_attendance(
            admin_user_id=self.admin.id,
            student_id=self.student1.id,
            date_val=past_date,
            status='PRESENT',
            check_in_time=check_in,
            check_out_time=check_out,
            reason="Approved field work offsite"
        )
        self.assertEqual(record.date, past_date)
        self.assertEqual(record.status, 'PRESENT')
        self.assertEqual(record.work_duration_minutes, 480)
        self.assertTrue(record.is_corrected)

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Leave Management Workflow Tests
    # ──────────────────────────────────────────────────────────────────────────

    def test_apply_leave_and_working_days_calculation(self):
        """Tests applying for leave and calculating effective working days (excluding weekends)."""
        # Monday to Wednesday (3 working days)
        start_d = date(2026, 3, 2)
        end_d = date(2026, 3, 4)
        leave = AttendanceService.apply_leave(
            student_id=self.student1.id,
            internship_id=self.internship1.id,
            leave_type='SICK_LEAVE',
            start_date=start_d,
            end_date=end_d,
            reason="Flu symptoms recovery"
        )
        self.assertEqual(leave.status, 'PENDING')
        self.assertEqual(leave.days_count, 3)
        self.assertEqual(leave.display_type, "Sick Leave")

    def test_apply_leave_overlapping_conflict_prevention(self):
        """Prevents submitting overlapping leave requests."""
        start_d1 = date(2026, 3, 2)
        end_d1 = date(2026, 3, 6)
        AttendanceService.apply_leave(
            student_id=self.student1.id,
            internship_id=self.internship1.id,
            leave_type='CASUAL_LEAVE',
            start_date=start_d1,
            end_date=end_d1,
            reason="Family commitment"
        )
        # Attempt conflicting overlap
        start_d2 = date(2026, 3, 5)
        end_d2 = date(2026, 3, 10)
        with self.assertRaises(ValueError) as cm:
            AttendanceService.apply_leave(
                student_id=self.student1.id,
                internship_id=self.internship1.id,
                leave_type='SICK_LEAVE',
                start_date=start_d2,
                end_date=end_d2,
                reason="Medical appointment"
            )
        self.assertIn("conflicting leave application already exists", str(cm.exception).lower())

    def test_student_cancel_pending_leave(self):
        """Allows intern to cancel their own pending leave request."""
        leave = AttendanceService.apply_leave(
            student_id=self.student1.id,
            internship_id=self.internship1.id,
            leave_type='ACADEMIC_LEAVE',
            start_date=date(2026, 3, 9),
            end_date=date(2026, 3, 10),
            reason="Exam postponed"
        )
        cancelled = AttendanceService.cancel_leave(
            leave_id=leave.id,
            student_id=self.student1.id,
            reason="Exam cancelled by university."
        )
        self.assertEqual(cancelled.status, 'CANCELLED')
        self.assertIn("cancelled by university", cancelled.cancellation_reason.lower())

    def test_student_cannot_cancel_other_student_leave(self):
        """IDOR check: Intern cannot cancel another intern's leave request."""
        leave = AttendanceService.apply_leave(
            student_id=self.student1.id,
            internship_id=self.internship1.id,
            leave_type='CASUAL_LEAVE',
            start_date=date(2026, 3, 9),
            end_date=date(2026, 3, 10),
            reason="Personal"
        )
        with self.assertRaises(PermissionError):
            AttendanceService.cancel_leave(
                leave_id=leave.id,
                student_id=self.student2.id
            )

    def test_leave_approval_automatically_syncs_attendance(self):
        """Approving a leave automatically marks working days as ON_LEAVE in attendance."""
        start_d = date(2026, 3, 2)  # Monday
        end_d = date(2026, 3, 3)    # Tuesday
        leave = AttendanceService.apply_leave(
            student_id=self.student1.id,
            internship_id=self.internship1.id,
            leave_type='SICK_LEAVE',
            start_date=start_d,
            end_date=end_d,
            reason="Doctor advised rest"
        )

        reviewed = AttendanceService.review_leave(
            leave_id=leave.id,
            reviewer_id=self.admin.id,
            action='APPROVE',
            remarks="Get well soon."
        )
        self.assertEqual(reviewed.status, 'APPROVED')
        self.assertEqual(reviewed.reviewer_id, self.admin.id)

        # Check attendance records created for Monday and Tuesday
        att_mon = AttendanceRecord.query.filter_by(student_id=self.student1.id, date=start_d).first()
        att_tue = AttendanceRecord.query.filter_by(student_id=self.student1.id, date=end_d).first()
        self.assertIsNotNone(att_mon)
        self.assertEqual(att_mon.status, 'ON_LEAVE')
        self.assertIsNotNone(att_tue)
        self.assertEqual(att_tue.status, 'ON_LEAVE')

    def test_leave_rejection(self):
        """Rejecting a leave request records rejection status and remarks."""
        leave = AttendanceService.apply_leave(
            student_id=self.student1.id,
            internship_id=self.internship1.id,
            leave_type='CASUAL_LEAVE',
            start_date=date(2026, 3, 2),
            end_date=date(2026, 3, 2),
            reason="Day off"
        )
        reviewed = AttendanceService.review_leave(
            leave_id=leave.id,
            reviewer_id=self.admin.id,
            action='REJECT',
            remarks="Critical milestone delivery required."
        )
        self.assertEqual(reviewed.status, 'REJECTED')
        self.assertIn("Critical milestone", reviewed.reviewer_remarks)

    # ──────────────────────────────────────────────────────────────────────────
    # 4. Policy, Company Holiday & Schedule Modification Tests
    # ──────────────────────────────────────────────────────────────────────────

    def test_company_holiday_registry_and_working_day_exclusion(self):
        """Validates that registered holidays are excluded from working day calculations."""
        # 2026-03-02 is Monday
        holiday_date = date(2026, 3, 2)
        h = AttendanceService.add_holiday(
            holiday_date=holiday_date,
            name="Founder's Day",
            description="Company holiday celebration"
        )
        self.assertIsNotNone(h.id)

        # Check working day status
        policy = AttendanceService.get_active_policy()
        is_working, desc = AttendanceService.is_working_day(holiday_date, policy)
        self.assertFalse(is_working)
        self.assertIn("Founder's Day", desc)

        # Working days between March 2 and March 4 should now be 2 days (excluding Monday holiday)
        count, dates = AttendanceService.calculate_working_days(date(2026, 3, 2), date(2026, 3, 4), policy)
        self.assertEqual(count, 2)
        self.assertNotIn(holiday_date, dates)

        # Delete holiday
        AttendanceService.delete_holiday(h.id)
        is_working_after, _ = AttendanceService.is_working_day(holiday_date, policy)
        self.assertTrue(is_working_after)

    def test_modify_internship_schedule_with_audit_reason(self):
        """Enforces business rule: automatic modification is prohibited; admin requires reason."""
        original_end = self.internship1.end_date
        new_end = date(2026, 4, 30)

        # Without reason -> fails
        with self.assertRaises(ValueError):
            AttendanceService.modify_internship_schedule(
                internship_id=self.internship1.id,
                admin_user_id=self.admin.id,
                new_end_date=new_end,
                reason=""
            )

        # With valid authorized reason -> succeeds
        updated_internship = AttendanceService.modify_internship_schedule(
            internship_id=self.internship1.id,
            admin_user_id=self.admin.id,
            new_end_date=new_end,
            reason="Authorized extension for academic exam clash."
        )
        self.assertEqual(str(updated_internship.end_date), str(new_end))

        # Check Audit Log
        audit = AuditLog.query.filter_by(action='INTERNSHIP_SCHEDULE_MODIFIED').first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor_id, self.admin.id)

    # ──────────────────────────────────────────────────────────────────────────
    # 5. Reporting, Telemetry & Export Generators Tests
    # ──────────────────────────────────────────────────────────────────────────

    def test_csv_and_pdf_exports(self):
        """Tests attendance CSV and PDF report generation."""
        # Seed an attendance record
        in_dt = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
        out_dt = datetime(2026, 3, 2, 17, 0, tzinfo=timezone.utc)
        record = AttendanceService.record_check_in(self.student1.id, self.internship1.id, in_dt)
        AttendanceService.record_check_out(self.student1.id, out_dt)

        # 1. CSV
        csv_str, filename, mime = AttendanceService.generate_attendance_csv(student_id=self.student1.id)
        self.assertTrue("Student ID" in csv_str or "Student UID" in csv_str)
        self.assertIn("AM-INT-2026-001", csv_str)
        self.assertIn("PRESENT", csv_str)
        self.assertTrue(filename.endswith(".csv"))
        self.assertEqual(mime, "text/csv")

        # 2. PDF
        pdf_bytes, pdf_filename, pdf_mime = AttendanceService.generate_attendance_pdf(student_id=self.student1.id)
        self.assertIsInstance(pdf_bytes, bytes)
        self.assertTrue(pdf_bytes.startswith(b'%PDF'))
        self.assertTrue(pdf_filename.endswith(".pdf"))
        self.assertEqual(pdf_mime, "application/pdf")

    # ──────────────────────────────────────────────────────────────────────────
    # 6. HTTP Web Route Tests
    # ──────────────────────────────────────────────────────────────────────────

    def test_student_attendance_http_flow(self):
        """Tests candidate attendance check-in, check-out, and dashboard viewing."""
        # Authenticate as student
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.student1_user.id)
            sess['auth_realm'] = 'employee'

        # 1. View Dashboard
        res = self.client.get('/attendance')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Attendance & Internship Schedule", res.data)

        # 2. Check In
        res_in = self.client.post('/attendance/check-in', data={'notes': 'Arrived on site'}, follow_redirects=True)
        self.assertEqual(res_in.status_code, 200)
        self.assertIn(b"Check-in registered successfully", res_in.data)

        # 3. Check Out
        res_out = self.client.post('/attendance/check-out', data={'notes': 'Completed day'}, follow_redirects=True)
        self.assertEqual(res_out.status_code, 200)
        self.assertIn(b"Check-out registered successfully", res_out.data)

        # 4. View Leave Dashboard
        res_leave = self.client.get('/leave')
        self.assertEqual(res_leave.status_code, 200)
        self.assertIn(b"Leave Applications", res_leave.data)

        # 5. Apply Leave
        res_apply = self.client.post('/leave/apply', data={
            'leave_type': 'SICK_LEAVE',
            'start_date': '2026-03-10',
            'end_date': '2026-03-11',
            'reason': 'Medical recovery'
        }, follow_redirects=True)
        self.assertEqual(res_apply.status_code, 200)
        self.assertIn(b"Leave application for", res_apply.data)

        # 6. Export CSV
        res_export_csv = self.client.get('/attendance/export?format=csv')
        self.assertEqual(res_export_csv.status_code, 200)
        self.assertEqual(res_export_csv.content_type, "text/csv; charset=utf-8")

        # 7. Export PDF
        res_export_pdf = self.client.get('/attendance/export?format=pdf')
        self.assertEqual(res_export_pdf.status_code, 200)
        self.assertEqual(res_export_pdf.content_type, "application/pdf")

    def test_mentor_attendance_and_leave_http_flow(self):
        """Tests mentor mentee attendance supervision and leave review decision."""
        # Submit a leave request first as student
        leave = AttendanceService.apply_leave(
            student_id=self.student1.id,
            internship_id=self.internship1.id,
            leave_type='CASUAL_LEAVE',
            start_date=date(2026, 3, 16),
            end_date=date(2026, 3, 17),
            reason="Family event"
        )

        # Authenticate as mentor
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.mentor_user.id)
            sess['auth_realm'] = 'mentor'

        # 1. View Mentee Attendance Roster
        res_att = self.client.get('/mentor/attendance')
        self.assertEqual(res_att.status_code, 200)
        self.assertIn(b"Mentee Attendance Supervision", res_att.data)

        # 2. View Mentee Leave Queue
        res_lq = self.client.get('/mentor/leave')
        self.assertEqual(res_lq.status_code, 200)
        self.assertIn(b"Mentee Leave Management Queue", res_lq.data)

        # 3. Approve Mentee Leave
        res_app = self.client.post(f'/mentor/leave/{leave.id}/review', data={
            'action': 'APPROVE',
            'remarks': 'Approved by mentor.'
        }, follow_redirects=True)
        self.assertEqual(res_app.status_code, 200)
        self.assertIn(b"has been approved successfully", res_app.data)

    def test_admin_attendance_and_schedule_http_flow(self):
        """Tests administrative attendance console, regularization, and schedule hub."""
        # Authenticate as admin
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.admin.id)
            sess['auth_realm'] = 'admin'

        # 1. Daily Attendance Dashboard
        res_att = self.client.get('/admin/attendance')
        self.assertEqual(res_att.status_code, 200)
        self.assertIn(b"Organization Attendance Console", res_att.data)

        # 2. Manual Retroactive Entry
        res_manual = self.client.post('/admin/attendance/manual-entry', data={
            'student_id': str(self.student1.id),
            'date': '2026-03-05',
            'status': 'PRESENT',
            'check_in_time': '09:00',
            'check_out_time': '17:00',
            'reason': 'Administrative manual retroactive entry test'
        }, follow_redirects=True)
        self.assertEqual(res_manual.status_code, 200)
        self.assertIn(b"Retroactive attendance record created", res_manual.data)

        # 3. Schedule Management Hub
        res_sch = self.client.get('/admin/schedule')
        self.assertEqual(res_sch.status_code, 200)
        self.assertIn(b"Scheduling, Policy", res_sch.data)

        # 4. Update Policy
        res_pol = self.client.post('/admin/schedule/policy', data={
            'work_start_time': '09:00',
            'work_end_time': '17:30',
            'late_threshold_time': '09:30',
            'half_day_minimum_hours': '4.0',
            'full_day_minimum_hours': '7.0',
            'working_days_csv': 'MON,TUE,WED,THU,FRI',
            'min_attendance_percentage': '80.0'
        }, follow_redirects=True)
        self.assertEqual(res_pol.status_code, 200)
        self.assertIn(b"Organizational Attendance Policy successfully updated", res_pol.data)

        # 5. Add Holiday
        res_hol = self.client.post('/admin/schedule/holidays', data={
            'holiday_date': '2026-08-15',
            'name': 'National Holiday',
            'description': 'Public Holiday',
            'is_recurring': '1'
        }, follow_redirects=True)
        self.assertEqual(res_hol.status_code, 200)
        self.assertIn(b"added successfully", res_hol.data)


if __name__ == '__main__':
    unittest.main()
