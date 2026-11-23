"""
Anti-Matrix Intern Portal — Attendance, Leave & Internship Scheduling Service (Phase 2, Module 9).
Handles check-in, check-out, status evaluation (Present, Late, Half-day, Absent), leave requests,
organizational attendance policies, holiday calendar, schedule adjustments, and CSV/PDF reporting.
"""

import os
import io
import csv
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
from flask import current_app, url_for


def _safe_url(endpoint, **kwargs):
    try:
        return url_for(endpoint, **kwargs)
    except Exception:
        mapping = {
            'student.attendance_dashboard': '/attendance',
            'mentor.mentor_leave_dashboard': '/mentor/leave',
            'student.leave_dashboard': '/leave',
            'admin.attendance_dashboard': '/admin/attendance',
            'admin.schedule_management': '/admin/schedule'
        }
        return mapping.get(endpoint, f"/{endpoint.replace('.', '/')}")
from werkzeug.utils import secure_filename
from sqlalchemy import or_, and_

from app import db
from app.models import (
    User, Student, Internship, Project, ProjectAssignment,
    AttendanceRecord, LeaveRequest, AttendancePolicy, CompanyHoliday,
    AuditLog
)
from app.services.notification_service import NotificationService

# ReportLab imports for attendance reports
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle


class AttendanceService:
    """Centralized service for attendance tracking, leave lifecycle, and internship scheduling."""

    ALLOWED_DOCUMENT_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'docx'}
    MAX_DOCUMENT_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

    WEEKDAY_MAP = {
        0: 'MON',
        1: 'TUE',
        2: 'WED',
        3: 'THU',
        4: 'FRI',
        5: 'SAT',
        6: 'SUN'
    }

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Policy & Configuration Engine
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def get_active_policy(cls) -> AttendancePolicy:
        """
        Retrieves the active organizational attendance policy.
        If no policy exists, creates and seeds the default enterprise policy.
        """
        policy = AttendancePolicy.query.filter_by(is_active=True).first()
        if not policy:
            policy = AttendancePolicy(
                name='Anti-Matrix Standard Engineering Attendance Policy',
                work_start_time='09:00',
                work_end_time='17:00',
                late_threshold_time='09:30',
                half_day_min_hours=4.0,
                full_day_min_hours=7.0,
                weekly_working_days='MON,TUE,WED,THU,FRI',
                min_attendance_percentage=80.0,
                is_active=True,
                updated_at=datetime.now(timezone.utc)
            )
            db.session.add(policy)
            db.session.commit()
        return policy

    @classmethod
    def update_policy(cls, admin_user_id: int, **kwargs) -> AttendancePolicy:
        """Updates organizational attendance rules with audit logging."""
        policy = cls.get_active_policy()
        allowed_keys = {
            'name', 'work_start_time', 'work_end_time', 'late_threshold_time',
            'half_day_min_hours', 'full_day_min_hours', 'weekly_working_days',
            'min_attendance_percentage'
        }

        changes = {}
        for k, v in kwargs.items():
            if k in allowed_keys and v is not None:
                old_val = getattr(policy, k)
                if old_val != v:
                    changes[k] = {'old': old_val, 'new': v}
                    setattr(policy, k, v)

        policy.updated_by = admin_user_id
        policy.updated_at = datetime.now(timezone.utc)

        audit = AuditLog(
            actor_id=admin_user_id,
            actor_role='ADMIN',
            action='ATTENDANCE_POLICY_UPDATED',
            target_entity='AttendancePolicy',
            target_id=str(policy.id),
            details_json=str(changes),
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(audit)
        db.session.commit()
        return policy

    @classmethod
    def is_working_day(cls, check_date: date, policy: AttendancePolicy = None) -> tuple:
        """
        Determines if a date is a scheduled working day.
        Returns: (is_working: bool, reason: str)
        """
        if not policy:
            policy = cls.get_active_policy()

        # Check holiday
        holiday = CompanyHoliday.query.filter_by(date=check_date).first()
        if holiday:
            return False, f"Holiday: {holiday.name}"

        # Check configured working days
        weekday_code = cls.WEEKDAY_MAP.get(check_date.weekday(), 'MON')
        working_days = policy.get_working_days_list()
        if weekday_code not in working_days:
            return False, f"Weekly Off ({weekday_code})"

        return True, "Working Day"

    @classmethod
    def get_holidays(cls, start_date: date = None, end_date: date = None) -> list:
        """Fetches company holidays, optionally filtered by date window."""
        q = CompanyHoliday.query
        if start_date:
            q = q.filter(CompanyHoliday.date >= start_date)
        if end_date:
            q = q.filter(CompanyHoliday.date <= end_date)
        return q.order_by(CompanyHoliday.date.asc()).all()

    @classmethod
    def add_holiday(cls, holiday_date: date, name: str, description: str = None, is_optional: bool = False, is_recurring: bool = False, **kwargs) -> CompanyHoliday:
        """Registers a company holiday."""
        existing = CompanyHoliday.query.filter_by(date=holiday_date).first()
        if existing:
            raise ValueError(f"Holiday already registered for {holiday_date}: '{existing.name}'")

        holiday = CompanyHoliday(
            date=holiday_date,
            name=name.strip(),
            description=description.strip() if description else None,
            is_optional=is_optional or is_recurring,
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(holiday)
        db.session.commit()
        return holiday

    @classmethod
    def delete_holiday(cls, holiday_id: int) -> bool:
        """Deletes a registered holiday."""
        h = db.session.get(CompanyHoliday, holiday_id)
        if h:
            db.session.delete(h)
            db.session.commit()
            return True
        return False

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Daily Check-In & Check-Out Workflow
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def get_today_attendance(cls, student_id: int, target_date: date = None) -> AttendanceRecord:
        """Retrieves today's attendance record for an intern."""
        if not target_date:
            target_date = datetime.now(timezone.utc).date()
        return AttendanceRecord.query.filter_by(student_id=student_id, date=target_date).first()

    @classmethod
    def record_check_in(
        cls,
        student_id: int,
        internship_id: int = None,
        check_in_dt: datetime = None,
        notes: str = None,
        ip_address: str = None
    ) -> AttendanceRecord:
        """
        Records intern check-in for the day:
        - Prevents duplicate check-ins on the same calendar day.
        - Compares arrival time against policy late threshold (e.g. 09:30).
        - Sets initial status (PRESENT or LATE).
        - Logs audit trail.
        """
        if isinstance(notes, datetime) and check_in_dt is None:
            check_in_dt = notes
            notes = None
        if isinstance(internship_id, datetime) and check_in_dt is None:
            check_in_dt = internship_id
            internship_id = None
        if not check_in_dt:
            check_in_dt = datetime.now(timezone.utc)
        target_date = check_in_dt.date()

        student = db.session.get(Student, student_id)
        if not student:
            raise ValueError("Student record not found.")

        # Check duplicate
        existing = AttendanceRecord.query.filter_by(student_id=student_id, date=target_date).first()
        if existing and existing.check_in_time:
            raise ValueError(f"You have already checked in for today at {existing.check_in_time.strftime('%H:%M UTC')}.")

        policy = cls.get_active_policy()

        # Parse threshold time
        try:
            late_parts = [int(p) for p in policy.late_threshold_time.split(':')]
            threshold_t = time(late_parts[0], late_parts[1])
        except Exception:
            threshold_t = time(9, 30)

        check_in_time_only = check_in_dt.time()
        initial_status = 'LATE' if check_in_time_only > threshold_t else 'PRESENT'

        if existing:
            # Overwrite placeholder record (e.g., if day was pre-seeded as pending)
            existing.check_in_time = check_in_dt
            existing.status = initial_status
            existing.check_in_notes = notes
            existing.ip_address = ip_address
            record = existing
        else:
            record = AttendanceRecord(
                student_id=student_id,
                internship_id=internship_id,
                date=target_date,
                check_in_time=check_in_dt,
                status=initial_status,
                check_in_notes=notes,
                ip_address=ip_address,
                created_at=datetime.now(timezone.utc)
            )
            db.session.add(record)

        audit = AuditLog(
            actor_id=student.user_id,
            actor_role='STUDENT',
            action='ATTENDANCE_CHECK_IN',
            target_entity='AttendanceRecord',
            target_id=str(record.id) if record.id else student.student_uid,
            details_json=str({
                'date': str(target_date),
                'check_in_time': check_in_dt.isoformat(),
                'status': initial_status
            }),
            ip_address=ip_address,
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(audit)
        db.session.commit()
        return record

    @classmethod
    def record_check_out(
        cls,
        student_id: int,
        check_out_dt: datetime = None,
        internship_id: int = None,
        ip_address: str = None,
        notes: str = None
    ) -> AttendanceRecord:
        """
        Records intern check-out:
        - Validates check-in exists.
        - Prevents multiple check-outs.
        - Calculates total working duration.
        - Applies half-day rule if worked hours < policy threshold.
        """
        if not check_out_dt:
            check_out_dt = datetime.now(timezone.utc)
        target_date = check_out_dt.date()

        student = db.session.get(Student, student_id)
        if not student:
            raise ValueError("Student record not found.")

        record = AttendanceRecord.query.filter_by(student_id=student_id, date=target_date).first()
        if not record or not record.check_in_time:
            raise ValueError("No check-in record found for today. You must check in first.")

        if record.check_out_time:
            raise ValueError(f"You have already checked out for today at {record.check_out_time.strftime('%H:%M UTC')}.")

        # Helper to normalize offset-aware vs offset-naive datetimes
        def _to_naive_utc(dt):
            if dt is None:
                return None
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt

        norm_out = _to_naive_utc(check_out_dt)
        norm_in = _to_naive_utc(record.check_in_time)

        if norm_out < norm_in:
            raise ValueError("Check-out time cannot be earlier than check-in time.")

        record.check_out_time = check_out_dt
        if notes:
            record.check_out_notes = notes
        duration_minutes = int((norm_out - norm_in).total_seconds() // 60)
        record.work_duration_minutes = max(0, duration_minutes)

        policy = cls.get_active_policy()
        half_day_min_mins = int((policy.half_day_min_hours or 4.0) * 60)

        # Apply half-day rule
        if record.work_duration_minutes < half_day_min_mins:
            record.status = 'HALF_DAY'

        audit = AuditLog(
            actor_id=student.user_id,
            actor_role='STUDENT',
            action='ATTENDANCE_CHECK_OUT',
            target_entity='AttendanceRecord',
            target_id=str(record.id),
            details_json=str({
                'date': str(target_date),
                'check_out_time': check_out_dt.isoformat(),
                'duration_minutes': record.work_duration_minutes,
                'final_status': record.status
            }),
            ip_address=ip_address,
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(audit)
        db.session.commit()
        return record

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Attendance Regularization & Admin Corrections
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def admin_correct_attendance(
        cls,
        record_id: int,
        admin_user_id: int,
        new_status: str,
        new_check_in: datetime = None,
        new_check_out: datetime = None,
        reason: str = None
    ) -> AttendanceRecord:
        """
        Authorizes administrative modification of attendance with immutable audit justification.
        """
        valid_statuses = {'PRESENT', 'LATE', 'HALF_DAY', 'ABSENT', 'ON_LEAVE', 'HOLIDAY'}
        if new_status not in valid_statuses:
            raise ValueError(f"Invalid status '{new_status}'. Allowed: {valid_statuses}")

        if not reason or not reason.strip():
            raise ValueError("A clear, documented reason is mandatory for administrative attendance corrections.")

        record = db.session.get(AttendanceRecord, record_id)
        if not record:
            raise ValueError("Attendance record not found.")

        old_state = {
            'status': record.status,
            'check_in': record.check_in_time.isoformat() if record.check_in_time else None,
            'check_out': record.check_out_time.isoformat() if record.check_out_time else None,
            'duration': record.work_duration_minutes
        }

        record.status = new_status
        if new_check_in:
            record.check_in_time = new_check_in
        if new_check_out:
            record.check_out_time = new_check_out

        if record.check_in_time and record.check_out_time:
            record.work_duration_minutes = max(0, int((record.check_out_time - record.check_in_time).total_seconds() // 60))

        record.is_corrected = True
        record.correction_reason = reason.strip()
        record.corrected_by = admin_user_id
        record.corrected_at = datetime.now(timezone.utc)

        audit = AuditLog(
            actor_id=admin_user_id,
            actor_role='ADMIN',
            action='ATTENDANCE_CORRECTED',
            target_entity='AttendanceRecord',
            target_id=str(record.id),
            details_json=str({
                'student_id': record.student_id,
                'date': str(record.date),
                'old': old_state,
                'new_status': new_status,
                'reason': reason.strip()
            }),
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(audit)
        db.session.commit()

        # Send notification to student
        try:
            student = record.student
            NotificationService.create_in_portal_notification(
                user_id=student.user_id,
                title="Attendance Record Regularized",
                message=f"Your attendance record for {record.date.strftime('%B %d, %Y')} was adjusted to '{record.display_status}'. Reason: {reason.strip()}",
                notif_type='INFO',
                category='GENERAL',
                link=_safe_url('student.attendance_dashboard')
            )
        except Exception as e:
            current_app.logger.warning(f"Notification error on attendance correction: {e}")

        return record

    @classmethod
    def admin_record_retroactive_attendance(
        cls,
        admin_user_id: int = None,
        student_id: int = None,
        date_val: date = None,
        status: str = 'PRESENT',
        check_in_time: datetime = None,
        check_out_time: datetime = None,
        reason: str = None,
        internship_id: int = None,
        target_date: date = None,
        check_in_t: time = None,
        check_out_t: time = None,
        **kwargs
    ) -> AttendanceRecord:
        """Creates or updates retroactive attendance record for past dates."""
        # Unify date argument
        effective_date = date_val or target_date
        if not effective_date:
            raise ValueError("Attendance date is required.")

        student = db.session.get(Student, student_id)
        if not student:
            raise ValueError("Student not found.")

        if not internship_id:
            active_internship = student.active_internship or student.internships.first()
            internship_id = active_internship.id if active_internship else None

        existing = AttendanceRecord.query.filter_by(student_id=student_id, date=effective_date).first()

        check_in_dt = check_in_time or (datetime.combine(effective_date, check_in_t) if check_in_t else None)
        check_out_dt = check_out_time or (datetime.combine(effective_date, check_out_t) if check_out_t else None)
        duration = 0
        if check_in_dt and check_out_dt:
            duration = max(0, int((check_out_dt - check_in_dt).total_seconds() // 60))

        if existing:
            return cls.admin_correct_attendance(
                record_id=existing.id,
                admin_user_id=admin_user_id,
                new_status=status,
                new_check_in=check_in_dt,
                new_check_out=check_out_dt,
                reason=reason
            )

        record = AttendanceRecord(
            student_id=student_id,
            internship_id=internship_id,
            date=effective_date,
            check_in_time=check_in_dt,
            check_out_time=check_out_dt,
            status=status,
            work_duration_minutes=duration,
            is_corrected=True,
            correction_reason=reason,
            corrected_by=admin_user_id,
            corrected_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(record)
        db.session.commit()
        return record

    # ──────────────────────────────────────────────────────────────────────────
    # 4. Leave Management Workflow
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def apply_leave(
        cls,
        student_id: int,
        internship_id: int,
        leave_type: str,
        start_date: date,
        end_date: date,
        reason: str,
        file_obj = None
    ) -> LeaveRequest:
        """
        Submits an intern leave application:
        - Validates date chronology.
        - Prevents duplicate or conflicting overlapping leave applications.
        - Calculates effective working days requested.
        - Persists optional supporting documentation safely.
        - Dispatches notifications to assigned mentor and academic staff.
        """
        valid_types = {'SICK_LEAVE', 'CASUAL_LEAVE', 'ACADEMIC_LEAVE', 'EMERGENCY_LEAVE'}
        leave_type = leave_type.upper().strip()
        if leave_type not in valid_types:
            raise ValueError(f"Invalid leave type '{leave_type}'. Allowed: {valid_types}")

        if start_date > end_date:
            raise ValueError("Leave start date cannot be later than end date.")

        if not reason or not reason.strip():
            raise ValueError("A clear reason for the leave application is required.")

        # Check for overlapping requests
        conflict = LeaveRequest.query.filter(
            LeaveRequest.student_id == student_id,
            LeaveRequest.status.in_(['PENDING', 'APPROVED']),
            LeaveRequest.start_date <= end_date,
            LeaveRequest.end_date >= start_date
        ).first()

        if conflict:
            raise ValueError(
                f"Conflicting leave application already exists from {conflict.start_date} to {conflict.end_date} "
                f"(Status: {conflict.status})."
            )

        # Calculate actual working days
        working_days_count, _ = cls.calculate_working_days(start_date, end_date)
        if working_days_count <= 0:
            working_days_count = 1  # Minimum 1 calendar day if on weekend

        # Handle optional file upload
        doc_filename = None
        if file_obj and file_obj.filename:
            fn = secure_filename(file_obj.filename)
            ext = fn.rsplit('.', 1)[-1].lower() if '.' in fn else ''
            if ext not in cls.ALLOWED_DOCUMENT_EXTENSIONS:
                raise ValueError(f"File type '.{ext}' not permitted for supporting documents.")

            upload_dir = Path(current_app.config.get('UPLOAD_FOLDER', 'app/static/uploads')) / 'leave_docs'
            upload_dir.mkdir(parents=True, exist_ok=True)
            doc_filename = f"leave_{student_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{fn}"
            file_obj.save(upload_dir / doc_filename)

        leave_req = LeaveRequest(
            student_id=student_id,
            internship_id=internship_id,
            leave_type=leave_type,
            start_date=start_date,
            end_date=end_date,
            days_count=working_days_count,
            reason=reason.strip(),
            supporting_document_path=doc_filename,
            status='PENDING',
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(leave_req)

        student = db.session.get(Student, student_id)
        audit = AuditLog(
            actor_id=student.user_id,
            actor_role='STUDENT',
            action='LEAVE_APPLIED',
            target_entity='LeaveRequest',
            target_id=str(leave_req.id) if leave_req.id else 'NEW',
            details_json=str({
                'type': leave_type,
                'start_date': str(start_date),
                'end_date': str(end_date),
                'days': working_days_count
            }),
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(audit)
        db.session.commit()

        # Notify mentor and admin
        try:
            internship = db.session.get(Internship, internship_id)
            mentor = internship.mentor if internship else None
            if mentor:
                NotificationService.create_in_portal_notification(
                    user_id=mentor.id,
                    title="New Leave Application Submitted",
                    message=f"Intern {student.full_name} submitted a {leave_req.display_type} request for {working_days_count} day(s) from {start_date} to {end_date}.",
                    notif_type='ACTION_REQUIRED',
                    category='TASK',
                    link=_safe_url('mentor.mentor_leave_dashboard')
                )
        except Exception as e:
            current_app.logger.warning(f"Notification error on leave application: {e}")

        return leave_req

    @classmethod
    def cancel_leave(cls, leave_id: int, student_id: int, reason: str = None) -> LeaveRequest:
        """Allows an intern to cancel their own pending leave request."""
        leave = db.session.get(LeaveRequest, leave_id)
        if not leave:
            raise ValueError("Leave request not found.")

        if leave.student_id != student_id:
            raise PermissionError("Access denied: You cannot cancel another intern's leave request.")

        if leave.status != 'PENDING':
            raise ValueError(f"Cannot cancel a request that is already '{leave.status}'. Only PENDING requests can be cancelled.")

        leave.status = 'CANCELLED'
        leave.cancellation_reason = reason.strip() if reason else "Cancelled by student."
        leave.cancelled_at = datetime.now(timezone.utc)

        audit = AuditLog(
            actor_id=leave.student.user_id,
            actor_role='STUDENT',
            action='LEAVE_CANCELLED',
            target_entity='LeaveRequest',
            target_id=str(leave.id),
            details_json=str({
                'leave_id': leave.id,
                'reason': leave.cancellation_reason
            }),
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(audit)
        db.session.commit()
        return leave

    @classmethod
    def review_leave(
        cls,
        leave_id: int,
        reviewer_id: int,
        action: str,
        remarks: str = None
    ) -> LeaveRequest:
        """
        Reviews and approves/rejects a leave application:
        - If APPROVED: Automatically creates 'ON_LEAVE' attendance records for working days.
        - Dispatches student notifications.
        - Records immutable audit trail.
        """
        action = action.upper().strip()
        if action not in {'APPROVE', 'REJECT'}:
            raise ValueError("Action must be either 'APPROVE' or 'REJECT'.")

        leave = db.session.get(LeaveRequest, leave_id)
        if not leave:
            raise ValueError("Leave request not found.")

        if leave.status != 'PENDING':
            raise ValueError(f"Leave request is already finalized with status '{leave.status}'.")

        reviewer = db.session.get(User, reviewer_id)
        new_status = 'APPROVED' if action == 'APPROVE' else 'REJECTED'

        leave.status = new_status
        leave.reviewer_id = reviewer_id
        leave.reviewed_at = datetime.now(timezone.utc)
        leave.reviewer_remarks = remarks.strip() if remarks else None

        # If approved, register ON_LEAVE attendance records for each working day
        if new_status == 'APPROVED':
            cur = leave.start_date
            policy = cls.get_active_policy()
            while cur <= leave.end_date:
                is_working, _ = cls.is_working_day(cur, policy)
                if is_working:
                    att = AttendanceRecord.query.filter_by(student_id=leave.student_id, date=cur).first()
                    if att:
                        att.status = 'ON_LEAVE'
                    else:
                        att = AttendanceRecord(
                            student_id=leave.student_id,
                            internship_id=leave.internship_id,
                            date=cur,
                            status='ON_LEAVE',
                            check_in_notes=f"Approved Leave: {leave.display_type}",
                            created_at=datetime.now(timezone.utc)
                        )
                        db.session.add(att)
                cur += timedelta(days=1)

        audit = AuditLog(
            actor_id=reviewer_id,
            actor_role='ADMIN' if (reviewer and reviewer.is_admin_or_staff) else 'MENTOR',
            action=f"LEAVE_{new_status}",
            target_entity='LeaveRequest',
            target_id=str(leave.id),
            details_json=str({
                'leave_id': leave.id,
                'decision': new_status,
                'remarks': leave.reviewer_remarks
            }),
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(audit)
        db.session.commit()

        # Send notification to student
        try:
            student = leave.student
            NotificationService.create_in_portal_notification(
                user_id=student.user_id,
                title=f"Leave Application {new_status.title()}",
                message=f"Your {leave.display_type} application for {leave.start_date} to {leave.end_date} has been {new_status.lower()}.",
                notif_type='SUCCESS' if new_status == 'APPROVED' else 'WARNING',
                category='GENERAL',
                link=_safe_url('student.leave_dashboard')
            )
        except Exception as e:
            current_app.logger.warning(f"Notification error on leave review: {e}")

        return leave

    # ──────────────────────────────────────────────────────────────────────────
    # 5. Internship Scheduling & Working Days Calculations
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def parse_date(cls, val) -> date:
        """Safely parses date, string, or datetime into date object."""
        if not val:
            return None
        if isinstance(val, date) and not isinstance(val, datetime):
            return val
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, str):
            val_clean = val.strip()
            for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d-%m-%Y', '%d/%m/%Y', '%B %d, %Y', '%d %b %Y'):
                try:
                    return datetime.strptime(val_clean, fmt).date()
                except ValueError:
                    pass
        raise ValueError(f"Could not parse '{val}' into a valid date.")

    @classmethod
    def calculate_working_days(cls, start_d: date, end_d: date, policy: AttendancePolicy = None) -> tuple:
        """
        Calculates total working days between start_d and end_d, excluding weekly offs and holidays.
        Returns: (count: int, working_dates_list: list)
        """
        if not policy:
            policy = cls.get_active_policy()

        cur = start_d
        count = 0
        working_dates = []

        # Cache holidays in range
        holidays = {h.date for h in cls.get_holidays(start_d, end_d)}
        working_days_set = set(policy.get_working_days_list())

        while cur <= end_d:
            weekday_code = cls.WEEKDAY_MAP.get(cur.weekday(), 'MON')
            if weekday_code in working_days_set and cur not in holidays:
                count += 1
                working_dates.append(cur)
            cur += timedelta(days=1)

        return count, working_dates

    @classmethod
    def get_internship_schedule_summary(cls, internship_id: int) -> dict:
        """
        Computes comprehensive scheduling analytics for an internship:
        - Total working days scheduled.
        - Working days elapsed up to today.
        - Total approved leave days.
        - Attended days vs required.
        - Completion pace.
        """
        internship = db.session.get(Internship, internship_id)
        if not internship:
            raise ValueError("Internship not found.")

        start_d = cls.parse_date(internship.start_date)
        end_d = cls.parse_date(internship.end_date)
        today = datetime.now(timezone.utc).date()

        policy = cls.get_active_policy()
        total_working_days, _ = cls.calculate_working_days(start_d, end_d, policy)

        # Elapsed working days up to today
        effective_elapsed_end = min(end_d, today)
        elapsed_working_days, _ = cls.calculate_working_days(start_d, effective_elapsed_end, policy) if start_d <= effective_elapsed_end else (0, [])

        # Attendance counts
        records = AttendanceRecord.query.filter_by(internship_id=internship.id).all()
        present_count = sum(1 for r in records if r.status in ['PRESENT', 'LATE'])
        half_day_count = sum(1 for r in records if r.status == 'HALF_DAY')
        on_leave_count = sum(1 for r in records if r.status == 'ON_LEAVE')
        absent_count = sum(1 for r in records if r.status == 'ABSENT')

        effective_attended = present_count + (0.5 * half_day_count)
        attendance_pct = round((effective_attended / max(elapsed_working_days, 1)) * 100, 1) if elapsed_working_days > 0 else 100.0
        schedule_pace = round((elapsed_working_days / max(total_working_days, 1)) * 100, 1) if total_working_days > 0 else 0.0

        return {
            'internship_id': internship.id,
            'start_date': start_d,
            'end_date': end_d,
            'total_calendar_days': (end_d - start_d).days + 1,
            'total_working_days': total_working_days,
            'elapsed_working_days': elapsed_working_days,
            'working_days_elapsed': elapsed_working_days,
            'remaining_working_days': max(0, total_working_days - elapsed_working_days),
            'present_days': present_count,
            'half_days': half_day_count,
            'on_leave_days': on_leave_count,
            'approved_leave_days': on_leave_count,
            'absent_days': absent_count,
            'attendance_percentage': min(100.0, attendance_pct),
            'schedule_pace_percentage': min(100.0, schedule_pace),
            'is_attendance_satisfactory': attendance_pct >= policy.min_attendance_percentage
        }

    @classmethod
    def modify_internship_schedule(
        cls,
        internship_id: int,
        admin_user_id: int,
        new_end_date: date,
        reason: str
    ) -> Internship:
        """
        Modifies internship schedule with authorized justification:
        Rule: Do not automatically change internship end dates without an authorized action.
        """
        internship = db.session.get(Internship, internship_id)
        if not internship:
            raise ValueError("Internship not found.")

        if not reason or not reason.strip():
            raise ValueError("An authorized reason is required for modifying an internship schedule.")

        old_end_date = str(internship.end_date)
        internship.end_date = str(new_end_date)

        audit = AuditLog(
            actor_id=admin_user_id,
            actor_role='ADMIN',
            action='INTERNSHIP_SCHEDULE_MODIFIED',
            target_entity='Internship',
            target_id=str(internship.id),
            details_json=str({
                'old_end_date': old_end_date,
                'new_end_date': str(new_end_date),
                'reason': reason.strip()
            }),
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(audit)
        db.session.commit()

        # Notify student and mentor
        try:
            student = internship.student
            NotificationService.create_in_portal_notification(
                user_id=student.user_id,
                title="Internship Schedule Adjusted",
                message=f"Your internship completion date has been updated to {new_end_date.strftime('%B %d, %Y')}. Reason: {reason.strip()}",
                notif_type='INFO',
                category='ALLOCATION',
                link=_safe_url('student.attendance_dashboard')
            )
        except Exception as e:
            current_app.logger.warning(f"Notification error on schedule modification: {e}")

        return internship

    # ──────────────────────────────────────────────────────────────────────────
    # 6. Telemetry & Attendance Dashboard Reporting
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def get_student_attendance_summary(cls, student_id: int, month: int = None, year: int = None) -> dict:
        """Calculates granular attendance metrics for a student."""
        q = AttendanceRecord.query.filter_by(student_id=student_id)
        if year and month:
            # First day of month
            start_m = date(year, month, 1)
            # Last day of month
            if month == 12:
                end_m = date(year, 12, 31)
            else:
                end_m = date(year, month + 1, 1) - timedelta(days=1)
            q = q.filter(AttendanceRecord.date >= start_m, AttendanceRecord.date <= end_m)

        records = q.order_by(AttendanceRecord.date.desc()).all()

        present_days = sum(1 for r in records if r.status == 'PRESENT')
        late_days = sum(1 for r in records if r.status == 'LATE')
        half_days = sum(1 for r in records if r.status == 'HALF_DAY')
        absent_days = sum(1 for r in records if r.status == 'ABSENT')
        on_leave_days = sum(1 for r in records if r.status == 'ON_LEAVE')
        total_records = len(records)

        effective_attended = present_days + late_days + (0.5 * half_days)
        total_accountable = max(1, present_days + late_days + half_days + absent_days)
        attendance_pct = round((effective_attended / total_accountable) * 100, 1)

        pending_leaves = LeaveRequest.query.filter_by(student_id=student_id, status='PENDING').count()
        exceptions = [r for r in records if r.status in ['LATE', 'HALF_DAY', 'ABSENT'] or r.is_corrected]

        return {
            'records': records,
            'total_records': total_records,
            'present_days': present_days,
            'late_days': late_days,
            'half_days': half_days,
            'absent_days': absent_days,
            'on_leave_days': on_leave_days,
            'effective_attended': effective_attended,
            'attendance_percentage': min(100.0, attendance_pct),
            'pending_leaves_count': pending_leaves,
            'exceptions': exceptions
        }

    @classmethod
    def get_organization_daily_overview(cls, target_date: date = None) -> dict:
        """Admin daily check-in summary across all active interns."""
        if not target_date:
            target_date = datetime.now(timezone.utc).date()

        active_internships = Internship.query.filter(
            Internship.status.in_(['ONBOARDING', 'ALLOCATED', 'ACTIVE'])
        ).all()
        total_active_interns = len(active_internships)

        records = AttendanceRecord.query.filter_by(date=target_date).all()
        record_map = {r.student_id: r for r in records}

        checked_in = sum(1 for r in records if r.check_in_time is not None)
        checked_out = sum(1 for r in records if r.check_out_time is not None)
        present = sum(1 for r in records if r.status == 'PRESENT')
        late = sum(1 for r in records if r.status == 'LATE')
        half_day = sum(1 for r in records if r.status == 'HALF_DAY')
        on_leave = sum(1 for r in records if r.status == 'ON_LEAVE')
        absent = max(0, total_active_interns - checked_in - on_leave)

        return {
            'date': target_date,
            'total_active_interns': total_active_interns,
            'checked_in': checked_in,
            'checked_out': checked_out,
            'currently_working': max(0, checked_in - checked_out),
            'present': present,
            'late': late,
            'half_day': half_day,
            'on_leave': on_leave,
            'absent': absent,
            'records': records
        }

    @classmethod
    def get_monthly_matrix(cls, student_id: int, year: int, month: int) -> list:
        """Returns structured day-by-day matrix for frontend calendar rendering."""
        # Month start and end
        start_m = date(year, month, 1)
        if month == 12:
            end_m = date(year, 12, 31)
        else:
            end_m = date(year, month + 1, 1) - timedelta(days=1)

        records = AttendanceRecord.query.filter(
            AttendanceRecord.student_id == student_id,
            AttendanceRecord.date >= start_m,
            AttendanceRecord.date <= end_m
        ).all()
        rec_map = {r.date: r for r in records}

        policy = cls.get_active_policy()
        matrix = []
        cur = start_m
        today = datetime.now(timezone.utc).date()

        while cur <= end_m:
            is_working, day_desc = cls.is_working_day(cur, policy)
            rec = rec_map.get(cur)

            if rec:
                status = rec.status
                duration = rec.work_duration_hours
            elif not is_working:
                status = 'HOLIDAY' if 'Holiday' in day_desc else 'WEEKLY_OFF'
                duration = 0.0
            elif cur > today:
                status = 'FUTURE'
                duration = 0.0
            else:
                status = 'ABSENT'
                duration = 0.0

            matrix.append({
                'date': cur,
                'day': cur.day,
                'weekday': cur.strftime('%a'),
                'status': status,
                'is_working': is_working,
                'duration_hours': duration,
                'record': rec
            })
            cur += timedelta(days=1)

        return matrix

    # ──────────────────────────────────────────────────────────────────────────
    # 7. CSV & PDF Report Generators
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def generate_attendance_csv(
        cls,
        report_type: str = 'student_attendance',
        student_id: int = None,
        start_date: date = None,
        end_date: date = None,
        **filters
    ) -> tuple:
        """Generates dynamic CSV report string for attendance and leave records."""
        # Flexibility if first positional argument is student_id (int)
        if isinstance(report_type, int) and student_id is None:
            student_id = report_type
            report_type = 'student_attendance'

        if student_id is None and 'student_id' in filters:
            student_id = filters['student_id']

        output = io.StringIO()
        writer = csv.writer(output)

        if report_type == 'student_attendance' or student_id is not None:
            q = AttendanceRecord.query
            if student_id:
                q = q.filter_by(student_id=student_id)
            if start_date:
                q = q.filter(AttendanceRecord.date >= start_date)
            if end_date:
                q = q.filter(AttendanceRecord.date <= end_date)

            records = q.order_by(AttendanceRecord.date.desc()).all()
            writer.writerow(['Date', 'Student UID', 'Student Name', 'Status', 'Check-In', 'Check-Out', 'Duration (Hours)', 'Corrected', 'Notes'])
            for r in records:
                writer.writerow([
                    r.date.isoformat(),
                    r.student.student_uid if r.student else '',
                    r.student.full_name if r.student else '',
                    r.status,
                    r.check_in_time.strftime('%H:%M:%S UTC') if r.check_in_time else '',
                    r.check_out_time.strftime('%H:%M:%S UTC') if r.check_out_time else '',
                    r.work_duration_hours,
                    'YES' if r.is_corrected else 'NO',
                    r.correction_reason or r.check_in_notes or ''
                ])

        elif report_type == 'daily_overview':
            target_date = filters.get('date', datetime.now(timezone.utc).date())
            records = AttendanceRecord.query.filter_by(date=target_date).order_by(AttendanceRecord.id.asc()).all()
            writer.writerow(['Date', 'Student UID', 'Student Name', 'Project', 'Status', 'Check-In', 'Check-Out', 'Duration (Mins)'])
            for r in records:
                internship = r.internship
                project = internship.active_assignment.project if (internship and internship.active_assignment) else None
                writer.writerow([
                    r.date.isoformat(),
                    r.student.student_uid if r.student else '',
                    r.student.full_name if r.student else '',
                    project.title if project else 'N/A',
                    r.status,
                    r.check_in_time.strftime('%H:%M:%S') if r.check_in_time else '',
                    r.check_out_time.strftime('%H:%M:%S') if r.check_out_time else '',
                    r.work_duration_minutes
                ])

        elif report_type == 'leave_history':
            leaves = LeaveRequest.query.order_by(LeaveRequest.id.desc()).all()
            writer.writerow(['Request ID', 'Student UID', 'Student Name', 'Leave Type', 'Start Date', 'End Date', 'Days Count', 'Status', 'Reviewer', 'Reason'])
            for l in leaves:
                writer.writerow([
                    l.id,
                    l.student.student_uid if l.student else '',
                    l.student.full_name if l.student else '',
                    l.display_type,
                    str(l.start_date),
                    str(l.end_date),
                    l.days_count,
                    l.status,
                    l.reviewer.full_name if l.reviewer else '',
                    l.reason
                ])

        csv_content = output.getvalue()
        filename = f"attendance_report_{datetime.now().strftime('%Y%m%d%H%M%S')}.csv"
        mime = "text/csv"
        return csv_content, filename, mime

    @classmethod
    def generate_attendance_pdf(
        cls,
        report_type: str = 'student_attendance',
        student_id: int = None,
        start_date: date = None,
        end_date: date = None,
        **filters
    ) -> tuple:
        """Generates an executive attendance PDF report with ReportLab."""
        if isinstance(report_type, int) and student_id is None:
            student_id = report_type
            report_type = 'student_attendance'

        if student_id is None and 'student_id' in filters:
            student_id = filters['student_id']

        storage_dir = Path(current_app.config.get('UPLOAD_FOLDER', 'app/static/uploads')) / 'reports'
        storage_dir.mkdir(parents=True, exist_ok=True)
        filename = f"AttendanceReport_{report_type}_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
        file_path = storage_dir / filename

        doc = SimpleDocTemplate(str(file_path), pagesize=landscape(A4), leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle('ReportTitle', fontName='Helvetica-Bold', fontSize=16, leading=19, textColor=colors.HexColor('#030A07'))
        meta_style = ParagraphStyle('ReportMeta', fontName='Helvetica', fontSize=8, leading=11, textColor=colors.HexColor('#64748B'))
        th_style = ParagraphStyle('ReportTH', fontName='Helvetica-Bold', fontSize=8, leading=10, textColor=colors.HexColor('#FFFFFF'))
        td_style = ParagraphStyle('ReportTD', fontName='Helvetica', fontSize=8, leading=10, textColor=colors.HexColor('#0F172A'))

        story = []
        story.append(Paragraph("<b>ANTI&bull;MATRIX TECHNOLOGIES</b> &bull; Executive Attendance & Scheduling Intelligence", title_style))
        story.append(Paragraph(f"Generated at: {datetime.now(timezone.utc).strftime('%B %d, %Y %H:%M UTC')} &bull; Report Type: {report_type.replace('_', ' ').title()}", meta_style))
        story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#10B981'), spaceAfter=14))

        if report_type == 'student_attendance' or student_id is not None:
            student = db.session.get(Student, student_id) if student_id else None
            q = AttendanceRecord.query
            if student_id:
                q = q.filter_by(student_id=student_id)
            if start_date:
                q = q.filter(AttendanceRecord.date >= start_date)
            if end_date:
                q = q.filter(AttendanceRecord.date <= end_date)
            records = q.order_by(AttendanceRecord.date.desc()).limit(30).all()

            story.append(Paragraph(f"<b>Attendance Roster:</b> {student.full_name if student else 'Organization Interns'} (ID: {student.student_uid if student else 'ALL'})", styles['Heading3']))
            table_data = [[Paragraph('DATE', th_style), Paragraph('STATUS', th_style), Paragraph('CHECK IN', th_style), Paragraph('CHECK OUT', th_style), Paragraph('HOURS', th_style), Paragraph('REGULARIZED', th_style), Paragraph('REASON / NOTES', th_style)]]
            for r in records:
                table_data.append([
                    Paragraph(str(r.date), td_style),
                    Paragraph(r.status, td_style),
                    Paragraph(r.check_in_time.strftime('%H:%M') if r.check_in_time else '-', td_style),
                    Paragraph(r.check_out_time.strftime('%H:%M') if r.check_out_time else '-', td_style),
                    Paragraph(str(r.work_duration_hours), td_style),
                    Paragraph('YES' if r.is_corrected else 'NO', td_style),
                    Paragraph((r.correction_reason or r.check_in_notes or '')[:40], td_style)
                ])

            t = Table(table_data, colWidths=[80, 80, 70, 70, 60, 80, 330])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#030A07')),
                ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
                ('BOX', (0,0), (-1,-1), 1, colors.HexColor('#CBD5E1')),
                ('TOPPADDING', (0,0), (-1,-1), 4),
                ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ]))
            story.append(t)

        doc.build(story)
        with open(file_path, 'rb') as f:
            pdf_bytes = f.read()

        return pdf_bytes, filename, "application/pdf"
