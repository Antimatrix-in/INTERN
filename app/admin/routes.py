import io
import os
import re
import json
import random
import string
import logging
from datetime import datetime, date, time, timedelta
from flask import Blueprint, render_template, redirect, url_for, flash, request, abort, jsonify, current_app, send_file, session, Response
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload, contains_eager
from app.extensions import db
from app.models import (
    User, Student, Internship, Project, ProjectAssignment, ProjectWeek, ProjectTask,
    WeeklyMilestone, WeeklyTask, WeeklySubmission, Meeting, Notification,
    AuditLog, Application, Payment, College, Department, InternshipPlan, Evaluation,
    TaskImportHistory, Employee, Document, IssuedDocument, EvaluationRubric,
    NotificationPreference, Announcement, AnnouncementRead, CertificateVerification,
    AttendanceRecord, LeaveRequest, AttendancePolicy, CompanyHoliday
)
from app.services.mentor_service import (
    get_approved_mentors, get_all_active_mentors,
    is_approved_mentor, generate_next_mentor_id, APPROVED_MENTOR_EMAILS
)
from app.services.project_allocation_engine import ProjectAllocationEngine
from app.services.submission_workflow_service import SubmissionWorkflowService
from app.services.analytics_reporting_service import AnalyticsReportingService
from app.services.notification_service import NotificationService
from app.services.document_service import DocumentService
from app.services.attendance_service import AttendanceService

logger = logging.getLogger(__name__)

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


# ─── Helpers ──────────────────────────────────────────────────────────────────

def generate_next_employee_id():
    """Generate the next sequential AM-INT-XXXX employee ID checking User, Student, and Employee records."""
    max_num = 0
    # 1. Check User table
    for u in User.query.filter(User.employee_id.like('AM-INT-%')).all():
        if u.employee_id:
            try:
                parts = u.employee_id.split('-')
                if parts[-1].isdigit():
                    num = int(parts[-1])
                    if num > max_num:
                        max_num = num
            except (ValueError, IndexError, AttributeError):
                pass

    # 2. Check Student table
    for s in Student.query.filter(Student.student_uid.like('AM-INT-%')).all():
        if s.student_uid:
            try:
                parts = s.student_uid.split('-')
                if parts[-1].isdigit():
                    num = int(parts[-1])
                    if num > max_num:
                        max_num = num
            except (ValueError, IndexError, AttributeError):
                pass

    # 3. Check Employee table
    for e in Employee.query.filter(Employee.employee_id.like('AM-INT-%')).all():
        if e.employee_id:
            try:
                parts = e.employee_id.split('-')
                if parts[-1].isdigit():
                    num = int(parts[-1])
                    if num > max_num:
                        max_num = num
            except (ValueError, IndexError, AttributeError):
                pass

    return f'AM-INT-{max_num + 1:04d}'


def generate_temp_password():
    """Generate a secure temporary password in format AM@XXXXXX."""
    chars = string.ascii_letters + string.digits
    suffix = ''.join(random.choices(chars, k=6))
    return f'AM@{suffix}'


def require_admin_role():
    """Check that the current user is admin/staff. Returns 403 response if not."""
    if not current_user.is_authenticated or not current_user.is_admin_or_staff or session.get('auth_realm') == 'employee':
        flash('Access restricted to ANTI MATRIX Administrators and Staff.', 'danger')
        abort(403)


# ─── Auth Guard ───────────────────────────────────────────────────────────────

@admin_bp.before_request
@login_required
def require_admin():
    # 1. Any session established through employee authentication is strictly denied
    if session.get('auth_realm') == 'employee':
        if (request.path.startswith('/admin/upload-tasks') or 
            request.is_json or 
            'application/json' in request.headers.get('Accept', '') or 
            request.headers.get('X-Requested-With') == 'XMLHttpRequest'):
            return jsonify({
                'success': False,
                'valid': False,
                'errors': ['Access restricted to ANTI MATRIX Administrators and Staff. Employees cannot access administrative areas.'],
                'error': 'Access restricted to ANTI MATRIX Administrators and Staff.'
            }), 403
        flash('Access restricted to ANTI MATRIX Administrators and Staff. Employees cannot access administrative areas.', 'danger')
        abort(403)

    # 2. Reject if user does not have administrator or staff privileges
    if not current_user.is_admin_or_staff:
        if (request.path.startswith('/admin/upload-tasks') or 
            request.is_json or 
            'application/json' in request.headers.get('Accept', '') or 
            request.headers.get('X-Requested-With') == 'XMLHttpRequest'):
            return jsonify({
                'success': False,
                'valid': False,
                'errors': ['Access restricted to ANTI MATRIX Administrators and Staff.'],
                'error': 'Access restricted to ANTI MATRIX Administrators and Staff.'
            }), 403
        flash('Access restricted to ANTI MATRIX Administrators and Staff.', 'danger')
        abort(403)

    # 3. Ensure active admin session realm
    if session.get('auth_realm') != 'admin':
        session['auth_realm'] = 'admin'


# ─── Dashboard ────────────────────────────────────────────────────────────────

@admin_bp.route('/dashboard')
@login_required
def dashboard():
    now = datetime.utcnow()
    total_employees = Student.query.count()
    active_interns = Internship.query.filter_by(status='ACTIVE').count()
    completed_interns = Internship.query.filter_by(status='COMPLETED').count()
    total_projects = Project.query.count()
    active_projects_count = Project.query.filter_by(is_active=True).count()
    assigned_projects = ProjectAssignment.query.count()
    pending_submissions = WeeklySubmission.query.filter(
        WeeklySubmission.status.in_(['SUBMITTED', 'UNDER_REVIEW'])
    ).count()
    scheduled_meetings = Meeting.query.filter_by(status='SCHEDULED').count()

    # Mentors and workload telemetry
    mentors = get_approved_mentors()
    total_mentors = len(mentors)
    mentor_workloads = []
    for m in mentors:
        assigned_c = Internship.query.filter_by(mentor_id=m.id, status='ACTIVE').count()
        mentor_student_ids = [i.student_id for i in Internship.query.filter_by(mentor_id=m.id, status='ACTIVE').all()]
        pending_rev_c = WeeklySubmission.query.filter(
            WeeklySubmission.student_id.in_(mentor_student_ids),
            WeeklySubmission.status.in_(['SUBMITTED', 'UNDER_REVIEW'])
        ).count() if mentor_student_ids else 0

        status_label = 'Optimal' if 1 <= assigned_c <= 5 else ('At Capacity' if assigned_c > 5 else 'Available')
        status_badge = 'badge-success' if 1 <= assigned_c <= 5 else ('badge-danger' if assigned_c > 5 else 'badge-neutral')

        mentor_workloads.append({
            'mentor': m,
            'name': m.full_name,
            'email': m.email,
            'employee_id': m.employee_id or f'AM-MTR-{m.id:03d}',
            'assigned_count': assigned_c,
            'pending_review_count': pending_rev_c,
            'workload_status': status_label,
            'status_badge': status_badge
        })

    # Track distribution (1-Month vs 3-Month)
    intern_track_1m = Internship.query.join(Internship.plan).filter(
        Internship.status == 'ACTIVE',
        InternshipPlan.duration_months == 1
    ).count()
    intern_track_3m = Internship.query.join(Internship.plan).filter(
        Internship.status == 'ACTIVE',
        InternshipPlan.duration_months == 3
    ).count()
    colleges_count = db.session.query(Student.college_id).filter(
        Student.college_id.isnot(None)
    ).distinct().count()

    # Milestones & Cohort Completion Rate
    total_milestones = WeeklyMilestone.query.count()
    completed_milestones = WeeklyMilestone.query.filter_by(status='COMPLETED').count()
    overall_completion_rate = round((completed_milestones / total_milestones * 100), 1) if total_milestones > 0 else 0

    # Overdue Milestones & Tasks Tracker
    overdue_milestones = WeeklyMilestone.query.options(
        joinedload(WeeklyMilestone.assignment).joinedload(ProjectAssignment.internship).joinedload(Internship.student).joinedload(Student.user),
        joinedload(WeeklyMilestone.assignment).joinedload(ProjectAssignment.project)
    ).filter(
        WeeklyMilestone.due_at < now,
        WeeklyMilestone.status.notin_(['COMPLETED', 'APPROVED'])
    ).order_by(WeeklyMilestone.due_at.asc()).all()

    overdue_tasks_count = WeeklyTask.query.join(WeeklyTask.milestone).filter(
        WeeklyMilestone.due_at < now,
        WeeklyMilestone.status.notin_(['COMPLETED', 'APPROVED']),
        WeeklyTask.is_completed == False
    ).count()

    # Active Projects List with metrics
    active_projects = Project.query.filter_by(is_active=True).order_by(Project.id.asc()).limit(6).all()
    active_projects_list = []
    for p in active_projects:
        p_assignments = ProjectAssignment.query.filter_by(project_id=p.id).all()
        pa_ids = [pa.id for pa in p_assignments]
        p_milestones_total = WeeklyMilestone.query.filter(WeeklyMilestone.assignment_id.in_(pa_ids)).count() if pa_ids else 0
        p_milestones_done = WeeklyMilestone.query.filter(
            WeeklyMilestone.assignment_id.in_(pa_ids),
            WeeklyMilestone.status == 'COMPLETED'
        ).count() if pa_ids else 0
        p_rate = round((p_milestones_done / p_milestones_total * 100), 1) if p_milestones_total > 0 else 0
        active_projects_list.append({
            'project': p,
            'assigned_count': len(p_assignments),
            'completion_rate': p_rate,
            'total_milestones': p_milestones_total,
            'completed_milestones': p_milestones_done
        })

    # Milestone Distribution
    milestone_distribution = {
        'COMPLETED': completed_milestones,
        'IN_PROGRESS': WeeklyMilestone.query.filter_by(status='IN_PROGRESS').count(),
        'AVAILABLE': WeeklyMilestone.query.filter_by(status='AVAILABLE').count(),
        'LOCKED': WeeklyMilestone.query.filter_by(status='LOCKED').count(),
        'UNDER_REVIEW': WeeklyMilestone.query.filter_by(status='UNDER_REVIEW').count(),
        'REDO_REQUIRED': WeeklyMilestone.query.filter_by(status='REDO_REQUIRED').count(),
        'OVERDUE': len(overdue_milestones)
    }

    # Submission Verdict Distribution
    submission_verdict_distribution = {
        'APPROVED': WeeklySubmission.query.filter_by(status='APPROVED').count(),
        'SUBMITTED': pending_submissions,
        'REDO_REQUIRED': WeeklySubmission.query.filter_by(status='REDO_REQUIRED').count()
    }

    # Recent Students Roster
    recent_students = Student.query.options(
        joinedload(Student.user)
    ).order_by(Student.id.desc()).limit(8).all()

    if recent_students:
        recent_student_ids = [s.id for s in recent_students]
        internships = Internship.query.filter(
            Internship.student_id.in_(recent_student_ids),
            Internship.status == 'ACTIVE'
        ).order_by(Internship.id.desc()).all()
        intern_map = {}
        for intern in internships:
            if intern.student_id not in intern_map:
                intern_map[intern.student_id] = intern
        for s in recent_students:
            s._preloaded_active_internship = intern_map.get(s.id)

    # Pending Submissions Review Queue
    pending_sub_list = WeeklySubmission.query.options(
        joinedload(WeeklySubmission.student).joinedload(Student.user),
        joinedload(WeeklySubmission.milestone).joinedload(WeeklyMilestone.assignment).joinedload(ProjectAssignment.project)
    ).filter(
        WeeklySubmission.status.in_(['SUBMITTED', 'UNDER_REVIEW'])
    ).order_by(WeeklySubmission.id.desc()).limit(8).all()

    # 1. Weekly Task Completion Stats
    weekly_task_stats = []
    for w in range(1, 5):
        milestones = WeeklyMilestone.query.filter_by(week_number=w).all()
        m_ids = [m.id for m in milestones]
        tot = WeeklyTask.query.filter(WeeklyTask.milestone_id.in_(m_ids)).count() if m_ids else 0
        comp = WeeklyTask.query.filter(WeeklyTask.milestone_id.in_(m_ids), WeeklyTask.is_completed == True).count() if m_ids else 0
        rate = round(comp / tot * 100, 1) if tot > 0 else 0
        weekly_task_stats.append({'week': w, 'total': tot, 'completed': comp, 'rate': rate})

    # 2. Internship Progress Distribution
    active_cohort = Internship.query.filter_by(status='ACTIVE').all()
    progress_distribution = {'0_25': 0, '26_50': 0, '51_75': 0, '76_100': 0}
    for i in active_cohort:
        pct = i.progress_percent or 0
        if pct <= 25:
            progress_distribution['0_25'] += 1
        elif pct <= 50:
            progress_distribution['26_50'] += 1
        elif pct <= 75:
            progress_distribution['51_75'] += 1
        else:
            progress_distribution['76_100'] += 1

    # 3. Project Allocation Statistics
    total_active_interns = len(active_cohort)
    allocated_count = ProjectAssignment.query.count()
    allocation_rate = round(allocated_count / total_active_interns * 100, 1) if total_active_interns > 0 else 0
    allocation_stats = {
        'total_active': total_active_interns,
        'allocated_count': allocated_count,
        'unallocated_count': max(0, total_active_interns - allocated_count),
        'allocation_rate': allocation_rate
    }

    # 4. Submission Review Statistics
    total_submissions_count = WeeklySubmission.query.count()
    approved_sub_count = WeeklySubmission.query.filter_by(status='APPROVED').count()
    pending_sub_count = pending_submissions
    redo_sub_count = WeeklySubmission.query.filter_by(status='REDO_REQUIRED').count()
    submission_review_stats = {
        'total': total_submissions_count,
        'approved': approved_sub_count,
        'pending': pending_sub_count,
        'redo': redo_sub_count,
        'approval_rate': round(approved_sub_count / total_submissions_count * 100, 1) if total_submissions_count > 0 else 0
    }

    # 5. Recent Activity Feed
    recent_activities = []
    for s in Student.query.order_by(Student.id.desc()).limit(4).all():
        if s.user:
            recent_activities.append({
                'type': 'registration',
                'title': f'Intern Onboarded: {s.user.full_name}',
                'detail': f'ID Generated: {s.student_uid}',
                'timestamp': s.created_at or now,
                'badge': 'badge-success'
            })
    for pa in ProjectAssignment.query.order_by(ProjectAssignment.id.desc()).limit(4).all():
        st = pa.internship.student if pa.internship else None
        st_name = st.user.full_name if st and st.user else 'Intern'
        pj_title = pa.project.title if pa.project else 'Project'
        recent_activities.append({
            'type': 'allocation',
            'title': f'Project Allocated: {pj_title[:32]}...',
            'detail': f'Assigned to {st_name}',
            'timestamp': pa.assigned_at or (pa.internship.created_at if pa.internship else now),
            'badge': 'badge-info'
        })
    for i in Internship.query.filter(Internship.mentor_id.isnot(None)).order_by(Internship.id.desc()).limit(4).all():
        if i.assigned_mentor and i.student and i.student.user:
            recent_activities.append({
                'type': 'mentor',
                'title': f'Mentor Assigned: {i.assigned_mentor.full_name}',
                'detail': f'Supervising {i.student.user.full_name} ({i.student.student_uid})',
                'timestamp': i.updated_at or i.created_at or now,
                'badge': 'badge-neutral'
            })
    for sub in WeeklySubmission.query.order_by(WeeklySubmission.id.desc()).limit(4).all():
        st_name = sub.student.user.full_name if sub.student and sub.student.user else 'Intern'
        w_num = sub.milestone.week_number if sub.milestone else 1
        recent_activities.append({
            'type': 'submission',
            'title': f'Milestone Submission: Week {w_num}',
            'detail': f'{st_name} — {sub.status}',
            'timestamp': sub.submitted_at or now,
            'badge': 'badge-warning' if sub.status in ['SUBMITTED', 'UNDER_REVIEW'] else 'badge-success'
        })
    recent_activities.sort(key=lambda a: a['timestamp'] or datetime.min, reverse=True)
    recent_activities = recent_activities[:8]

    # 6. Upcoming Deadlines
    upcoming_deadlines = WeeklyMilestone.query.options(
        joinedload(WeeklyMilestone.assignment).joinedload(ProjectAssignment.internship).joinedload(Internship.student).joinedload(Student.user),
        joinedload(WeeklyMilestone.assignment).joinedload(ProjectAssignment.project)
    ).filter(
        WeeklyMilestone.due_at.isnot(None),
        WeeklyMilestone.status.notin_(['COMPLETED', 'APPROVED'])
    ).order_by(WeeklyMilestone.due_at.asc()).limit(8).all()

    # 7. Project Overview Table Data
    projects_overview = []
    for p in Project.query.order_by(Project.id.asc()).all():
        assignments = ProjectAssignment.query.filter_by(project_id=p.id).all()
        intern_count = len(assignments)
        mentor_names = set()
        for a in assignments:
            if a.internship and a.internship.assigned_mentor:
                mentor_names.add(a.internship.assigned_mentor.full_name)
        pa_ids = [a.id for a in assignments]
        p_milestones_total = WeeklyMilestone.query.filter(WeeklyMilestone.assignment_id.in_(pa_ids)).count() if pa_ids else 0
        p_milestones_done = WeeklyMilestone.query.filter(
            WeeklyMilestone.assignment_id.in_(pa_ids),
            WeeklyMilestone.status == 'COMPLETED'
        ).count() if pa_ids else 0
        comp_pct = round((p_milestones_done / p_milestones_total * 100), 1) if p_milestones_total > 0 else 0
        projects_overview.append({
            'project': p,
            'name': p.title,
            'code': p.project_code,
            'domain': p.domain or 'Engineering',
            'allocated_interns': intern_count,
            'assigned_mentors': list(mentor_names) if mentor_names else ['Pending Allocation'],
            'completion_pct': comp_pct,
            'duration_weeks': p.duration_weeks,
            'status': 'Active' if p.is_active else 'Inactive'
        })

    return render_template(
        'admin/dashboard.html',
        total_employees=total_employees,
        active_interns=active_interns,
        completed_interns=completed_interns,
        total_projects=total_projects,
        active_projects_count=active_projects_count,
        assigned_projects=assigned_projects,
        pending_submissions=pending_submissions,
        scheduled_meetings=scheduled_meetings,
        total_mentors=total_mentors,
        approved_mentors=mentors,
        mentor_workloads=mentor_workloads,
        intern_track_1m=intern_track_1m,
        intern_track_3m=intern_track_3m,
        colleges_count=colleges_count,
        total_milestones=total_milestones,
        completed_milestones=completed_milestones,
        overall_completion_rate=overall_completion_rate,
        active_projects_list=active_projects_list,
        overdue_milestones=overdue_milestones,
        overdue_tasks_count=overdue_tasks_count,
        milestone_distribution=milestone_distribution,
        submission_verdict_distribution=submission_verdict_distribution,
        recent_students=recent_students,
        pending_sub_list=pending_sub_list,
        weekly_task_stats=weekly_task_stats,
        progress_distribution=progress_distribution,
        allocation_stats=allocation_stats,
        submission_review_stats=submission_review_stats,
        recent_activities=recent_activities,
        upcoming_deadlines=upcoming_deadlines,
        projects_overview=projects_overview
    )


# ─── Application ID Lookup API ────────────────────────────────────────────────

@admin_bp.route('/api/application/<app_id>')
@login_required
def api_application_lookup(app_id):
    """Server-side AJAX endpoint: look up a career application by its Application ID."""
    from app.services.career_integration import CareerIntegrationService
    from app.services.problem_allocation_service import ProblemAllocationService

    app = CareerIntegrationService.get_application_by_id(app_id)
    if not app:
        return jsonify({'success': False, 'error': 'Application ID not found.'}), 404

    # Validate application status
    app_status = (app.status or '').upper()
    if app_status in ['REJECTED', 'CANCELLED']:
        return jsonify({'success': False, 'error': 'Application has been rejected or cancelled.'}), 400

    # Validate payment status
    has_payment = False
    if app.payments.filter(
        (Payment.status.in_(['paid', 'PAID', 'SUCCESS', 'SUCCESSFUL', 'COMPLETED', 'completed'])) |
        (Payment.payment_status.in_(['paid', 'PAID', 'SUCCESS', 'SUCCESSFUL', 'COMPLETED', 'completed']))
    ).first():
        has_payment = True
    elif app_status in ['APPROVED', 'PAID', 'SUCCESS', 'VERIFIED']:
        has_payment = True

    if not has_payment:
        return jsonify({'success': False, 'error': 'Payment has not been completed for this application.'}), 400

    # Determine duration from application source of truth
    duration_months = CareerIntegrationService.get_application_duration_months(app)
    duration_str = f"{duration_months} Month{'s' if duration_months > 1 else ''}"
    plan_code = '3_MONTH_PROFESSIONAL' if duration_months == 3 else '1_MONTH_PROJECT'

    # College resolution
    college_obj = None
    if app.college_name:
        college_obj = College.query.filter(College.name.ilike(f"%{app.college_name.strip()}%")).first()
    if not college_obj:
        college_obj = College.query.filter_by(code='OTHER-COLLEGE').first() or College.query.first()

    # Check if already converted / employee exists
    student, user = CareerIntegrationService.get_employee_by_application_id(app.application_no)
    if app.is_converted_to_employee or student:
        emp_id = app.converted_employee_id or (user.employee_id if user else (student.student_uid if student else ''))
        username_val = user.email if user else (app.candidate_email or '')
        assigned_proj = student.active_internship.active_assignment.project if (student and student.active_internship and student.active_internship.active_assignment) else None

        return jsonify({
            'success': True,
            'already_exists': True,
            'message': 'Employee already exists.',
            'application_no': app.application_no,
            'candidate_name': app.candidate_name or (user.full_name if user else ''),
            'candidate_email': app.candidate_email or (user.email if user else ''),
            'candidate_phone': app.candidate_phone or '',
            'college_name': app.college_name or (student.college.name if student and student.college else ''),
            'department_name': app.department_name or (student.department.name if student and student.department else ''),
            'course': app.course or (student.degree if student else (app.department_name or 'Engineering')),
            'applied_role': app.applied_role or 'Intern',
            'duration': duration_str,
            'duration_months': duration_months,
            'duration_plan': plan_code,
            'payment_status': 'SUCCESS',
            'status': 'APPROVED',
            'employee_id': emp_id,
            'username': username_val,
            'student_id': student.id if student else None,
            'problem_id': assigned_proj.project_code if assigned_proj else None,
            'problem_title': assigned_proj.title if assigned_proj else None
        }), 200

    # Look up eligible problems for this college & duration pool
    eligible_problems = ProblemAllocationService.get_eligible_problems(
        college_id=college_obj.id if college_obj else 1,
        duration_months=duration_months,
        domain=app.applied_role
    )

    allocated_preview = None
    allocation_warning = None
    if eligible_problems:
        p_preview = eligible_problems[0]
        allocated_preview = {
            'problem_id': p_preview.project_code,
            'title': p_preview.title,
            'domain': p_preview.domain,
            'duration': f"{p_preview.duration_months} Month{'s' if p_preview.duration_months > 1 else ''}",
            'available_pool_count': len(eligible_problems)
        }
    else:
        allocation_warning = f"No available {duration_months} Month problem for {college_obj.name if college_obj else 'this college'}. Please upload another {duration_months} Month problem before creating this employee."

    username_val = app.candidate_email or ''
    return jsonify({
        'success': True,
        'already_exists': False,
        'application_no': app.application_no,
        'candidate_name': app.candidate_name or '',
        'candidate_email': app.candidate_email or '',
        'candidate_phone': app.candidate_phone or '',
        'candidate_dob': app.candidate_dob or '',
        'candidate_gender': app.candidate_gender or '',
        'college_name': app.college_name or (college_obj.name if college_obj else ''),
        'department_name': app.department_name or '',
        'course': app.course or (app.department_name or 'Engineering'),
        'year_of_study': app.year_of_study or '',
        'roll_number': app.roll_number or '',
        'applied_role': app.applied_role or 'Intern',
        'duration': duration_str,
        'duration_months': duration_months,
        'duration_plan': plan_code,
        'payment_status': 'SUCCESS',
        'status': 'APPROVED',
        'employee_id': app.converted_employee_id or 'Generated on Creation',
        'username': username_val,
        'city': app.city or '',
        'state': app.state or '',
        'aadhaar_masked': app.aadhaar_masked or '',
        'allocated_problem_preview': allocated_preview,
        'allocation_warning': allocation_warning
    })


# ─── Employee Directory ───────────────────────────────────────────────────────

@admin_bp.route('/employees')
@login_required
def employees_list():
    query = request.args.get('q', '').strip()
    duration = request.args.get('duration', '').strip()
    status_filter = request.args.get('status', '').strip().upper()
    college_id = request.args.get('college_id', type=int)
    department_id = request.args.get('department_id', type=int)
    sort_by = request.args.get('sort_by', 'id').strip().lower()
    order = request.args.get('order', 'desc').strip().lower()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 15, type=int)

    # Base query joining Student, User, Internship, InternshipPlan
    students_query = Student.query.options(
        joinedload(Student.user),
        joinedload(Student.college),
        joinedload(Student.department)
    ).join(User, Student.user_id == User.id)\
     .join(Internship, Internship.student_id == Student.id, isouter=True)\
     .join(InternshipPlan, Internship.plan_id == InternshipPlan.id, isouter=True)

    # Status filtering
    if status_filter in ['ACTIVE', 'ALLOCATED', 'ONBOARDING', 'REGISTERED', 'COMPLETED', 'ARCHIVED']:
        students_query = students_query.filter(Internship.status == status_filter)
    elif status_filter == 'ALL':
        # Admin explicitly asked for all statuses
        pass
    else:
        # Default behavior: Show active interns when no status filter is explicitly selected
        # This guarantees 100% backward compatibility for all existing tests
        students_query = students_query.filter(User.is_active == True, Internship.status == 'ACTIVE')

    # Duration filtering
    if duration in ['1', '3']:
        students_query = students_query.filter(InternshipPlan.duration_months == int(duration))

    # College & Department filtering
    if college_id:
        students_query = students_query.filter(Student.college_id == college_id)
    if department_id:
        students_query = students_query.filter(Student.department_id == department_id)

    # Search filter
    if query:
        search = f'%{query}%'
        students_query = students_query.join(College, Student.college_id == College.id, isouter=True)\
            .join(Department, Student.department_id == Department.id, isouter=True)\
            .join(Application, Application.student_id == Student.id, isouter=True)\
            .filter(
                db.or_(
                    User.full_name.ilike(search),
                    User.email.ilike(search),
                    User.phone.ilike(search),
                    Student.student_uid.ilike(search),
                    Student.roll_number.ilike(search),
                    College.name.ilike(search),
                    Department.name.ilike(search),
                    Application.application_no.ilike(search)
                )
            )

    # Sorting
    if sort_by == 'name':
        sort_col = User.full_name
    elif sort_by == 'start_date':
        sort_col = Internship.start_date
    elif sort_by == 'status':
        sort_col = Internship.status
    elif sort_by == 'progress':
        sort_col = Internship.progress_percent
    else:
        sort_col = Student.id

    if order == 'asc':
        students_query = students_query.order_by(sort_col.asc())
    else:
        students_query = students_query.order_by(sort_col.desc())

    # Pagination
    pagination = students_query.paginate(page=page, per_page=per_page, error_out=False)
    students = pagination.items

    # Preload active internships, active assignments, and applications in batch to eliminate N+1 queries
    if students:
        student_ids = [s.id for s in students]
        
        # 1. Batch load active internships with plan & mentor
        internships = Internship.query.options(
            joinedload(Internship.plan),
            joinedload(Internship.mentor)
        ).filter(
            Internship.student_id.in_(student_ids)
        ).order_by(Internship.id.desc()).all()
        
        internship_map = {}
        for intern in internships:
            if intern.student_id not in internship_map:
                internship_map[intern.student_id] = intern
        
        # 2. Batch load active assignments with project for these internships
        internship_ids = [intern.id for intern in internship_map.values()]
        if internship_ids:
            assignments = ProjectAssignment.query.options(
                joinedload(ProjectAssignment.project)
            ).filter(
                ProjectAssignment.internship_id.in_(internship_ids)
            ).order_by(ProjectAssignment.id.desc()).all()
            
            assignment_map = {}
            for assign in assignments:
                if assign.internship_id not in assignment_map:
                    assignment_map[assign.internship_id] = assign
            
            for intern in internship_map.values():
                intern._preloaded_active_assignment = assignment_map.get(intern.id)
        
        # 3. Batch load applications for these students
        applications = Application.query.filter(
            Application.student_id.in_(student_ids)
        ).order_by(Application.id.desc()).all()
        
        app_map = {}
        for app in applications:
            if app.student_id not in app_map:
                app_map[app.student_id] = app
        
        for s in students:
            s._preloaded_active_internship = internship_map.get(s.id)
            s._preloaded_application = app_map.get(s.id)

    # Consolidated badge count metrics across directory
    counts_by_status = dict(
        db.session.query(
            Internship.status,
            db.func.count(Student.id)
        ).join(Student, Internship.student_id == Student.id)\
         .group_by(Internship.status).all()
    )
    count_active = counts_by_status.get('ACTIVE', 0)
    count_onboarding = counts_by_status.get('ONBOARDING', 0)
    count_allocated = counts_by_status.get('ALLOCATED', 0)
    count_registered = counts_by_status.get('REGISTERED', 0)
    count_completed = counts_by_status.get('COMPLETED', 0)
    count_archived = counts_by_status.get('ARCHIVED', 0)

    counts_by_duration = dict(
        db.session.query(
            InternshipPlan.duration_months,
            db.func.count(Student.id)
        ).join(User, Student.user_id == User.id)\
         .join(Internship, Internship.student_id == Student.id)\
         .join(InternshipPlan, Internship.plan_id == InternshipPlan.id)\
         .filter(User.is_active == True, Internship.status == 'ACTIVE')\
         .group_by(InternshipPlan.duration_months).all()
    )

    count_1m = counts_by_duration.get(1, 0)
    count_3m = counts_by_duration.get(3, 0)
    count_all = count_1m + count_3m
    total_interns_count = Student.query.count()

    colleges = College.query.filter_by(is_active=True).order_by(College.name.asc()).all()
    departments = Department.query.filter_by(is_active=True).order_by(Department.name.asc()).all()

    return render_template(
        'admin/employees.html',
        students=students,
        pagination=pagination,
        search_query=query,
        current_duration=duration,
        current_status=status_filter,
        current_college_id=college_id,
        current_department_id=department_id,
        sort_by=sort_by,
        order=order,
        count_1m=count_1m,
        count_3m=count_3m,
        count_all=count_all,
        count_active=count_active,
        count_onboarding=count_onboarding,
        count_allocated=count_allocated,
        count_registered=count_registered,
        count_completed=count_completed,
        count_archived=count_archived,
        total_interns_count=total_interns_count,
        colleges=colleges,
        departments=departments
    )


@admin_bp.route('/employees/<int:student_id>')
@login_required
def employee_detail(student_id):
    student = Student.query.get_or_404(student_id)
    internship = student.active_internship
    assignment = internship.active_assignment if internship else None
    milestones = assignment.weekly_milestones.all() if assignment else []
    meetings = Meeting.query.filter_by(student_id=student.id).order_by(Meeting.id.desc()).all()
    submissions = WeeklySubmission.query.filter_by(student_id=student.id).order_by(WeeklySubmission.id.desc()).all()
    documents = Document.query.filter_by(student_id=student.id).order_by(Document.id.desc()).all()
    issued_documents = IssuedDocument.query.filter_by(student_id=student.id).order_by(IssuedDocument.id.desc()).all()

    # Filter staff to ensure mentors are all valid active mentors
    approved_mentors = get_all_active_mentors()
    all_staff = User.query.filter(User.role.in_(['super_admin', 'admin', 'evaluator'])).all()
    staff_members = all_staff + approved_mentors
    all_projects = Project.query.filter_by(is_active=True).order_by(Project.title.asc()).all()

    # Retrieve audit history for this intern
    audit_logs = AuditLog.query.filter(
        db.or_(
            AuditLog.target_id == str(student.id),
            AuditLog.target_id == student.student_uid,
            AuditLog.target_id == str(student.user_id)
        )
    ).order_by(AuditLog.id.desc()).limit(20).all()

    return render_template(
        'admin/student_detail.html',
        student=student,
        internship=internship,
        assignment=assignment,
        milestones=milestones,
        meetings=meetings,
        submissions=submissions,
        documents=documents,
        issued_documents=issued_documents,
        audit_logs=audit_logs,
        staff_members=staff_members,
        approved_mentors=approved_mentors,
        all_projects=all_projects
    )


@admin_bp.route('/employees/<int:student_id>/assign-mentor', methods=['POST'])
@login_required
def assign_mentor(student_id):
    student = Student.query.get_or_404(student_id)
    internship = student.active_internship
    if not internship:
        flash(f'No active internship found for {student.user.full_name}.', 'danger')
        return redirect(url_for('admin.employee_detail', student_id=student.id))

    try:
        mentor_id = int(request.form.get('mentor_id', 0))
    except (ValueError, TypeError):
        mentor_id = 0

    mentor = User.query.get(mentor_id) if mentor_id else None
    if not mentor or not is_approved_mentor(mentor):
        flash('Invalid mentor selection. Please select an active technical mentor.', 'danger')
        return redirect(url_for('admin.employee_detail', student_id=student.id))

    old_mentor = internship.assigned_mentor_name or 'Unassigned'
    internship.mentor_id = mentor.id

    capacity_note = ""
    if mentor.is_at_capacity:
        capacity_note = f" (Notice: Mentor is at capacity: {mentor.active_interns_count}/{mentor.max_capacity})"

    # Record Audit Log
    audit = AuditLog(
        actor_id=current_user.id,
        actor_role=current_user.role,
        action='ASSIGN_MENTOR',
        target_entity='internships',
        target_id=str(internship.id),
        details_json=json.dumps({
            'student_id': student.id,
            'student_uid': student.student_uid,
            'old_mentor': old_mentor,
            'new_mentor': mentor.full_name,
            'timestamp': datetime.utcnow().isoformat()
        }),
        ip_address=request.remote_addr
    )
    db.session.add(audit)

    db.session.commit()
    flash(f'Assigned technical mentor successfully updated to {mentor.full_name}.', 'success')
    return redirect(url_for('admin.employee_detail', student_id=student.id))


# ─── Delete All Employees ─────────────────────────────────────────────────────

@admin_bp.route('/employees/delete-all', methods=['POST'])
@login_required
def delete_all_employees():
    """
    Destructive action: Deletes all eligible employee records from the Internship Portal.
    Requires admin authorization and exact case-sensitive confirmation phrase 'DELETE ALL EMPLOYEES'.
    Safely inspects dependencies: if historical submissions or evaluations exist, halts and rolls back.
    """
    # 1. Server-side Administrator Authorization
    if session.get('auth_realm') == 'employee':
        flash('Access restricted to ANTI MATRIX Administrators and Staff. Employees cannot access administrative actions.', 'danger')
        abort(403)

    if not current_user.is_authenticated or not current_user.is_admin_or_staff or not current_user.is_active:
        flash('Administrator authentication required to perform this action.', 'danger')
        abort(403)

    # 2. Strict case-sensitive phrase verification (no whitespace normalization)
    confirmation_phrase = request.form.get('confirmation_phrase', '')
    if confirmation_phrase != 'DELETE ALL EMPLOYEES':
        flash('Confirmation phrase did not match. Deletion aborted.', 'danger')
        return redirect(url_for('admin.employees_list'))

    # 3. Transactional Deletion
    try:
        # A. Query active directory students (strictly excluding admin/staff accounts)
        active_students = Student.query.join(User, Student.user_id == User.id)\
            .join(Internship, Internship.student_id == Student.id)\
            .filter(
                User.is_active == True,
                Internship.status == 'ACTIVE',
                User.role.notin_(['super_admin', 'admin', 'mentor', 'hr', 'evaluator'])
            ).all()

        target_student_ids = [s.id for s in active_students]

        # B. Dependency check: do any employees have protected historical records?
        has_submissions = False
        has_evaluations = False

        if target_student_ids:
            has_submissions = WeeklySubmission.query.filter(WeeklySubmission.student_id.in_(target_student_ids)).count() > 0
            has_evaluations = Evaluation.query.filter(Evaluation.employee_id.in_(target_student_ids)).count() > 0

        if has_submissions or has_evaluations:
            db.session.rollback()
            flash("Employees could not be deleted because related records require attention. No employee records were removed.", "danger")
            return redirect(url_for('admin.employees_list'))

        deleted_count = 0

        # C. Retrieve employee records directly from database and delete from employees table
        emp_records = Employee.query.all()
        for emp in emp_records:
            db.session.delete(emp)
            deleted_count += 1

        # D. Decommission active student internships and revoke student user access
        for s in active_students:
            for intern in s.internships:
                if intern.status == 'ACTIVE':
                    intern.status = 'TERMINATED'
                    deleted_count += 1

            if s.user and s.user.role == 'student':
                s.user.is_active = False

            if hasattr(s, 'applications') and s.applications:
                for app_rec in s.applications:
                    app_rec.is_converted_to_employee = False
                    app_rec.converted_employee_id = None

        # E. Clean up any active internship records for admin users in the directory without touching admin accounts
        admin_internships = Internship.query.join(Student, Internship.student_id == Student.id)\
            .join(User, Student.user_id == User.id)\
            .filter(
                Internship.status == 'ACTIVE',
                User.role.in_(['super_admin', 'admin'])
            ).all()
        for ai in admin_internships:
            ai.status = 'TERMINATED'
            deleted_count += 1

        # F. Record audit log
        audit = AuditLog(
            actor_id=current_user.id,
            actor_role=current_user.role,
            action='DELETE_ALL_EMPLOYEES',
            target_entity='employees',
            target_id='ALL',
            details_json=json.dumps({
                'action': 'DELETE_ALL_EMPLOYEES',
                'affected_count': deleted_count,
                'timestamp': datetime.utcnow().isoformat()
            }),
            ip_address=request.remote_addr
        )
        db.session.add(audit)

        db.session.commit()
        flash("All eligible employee records have been deleted successfully.", "success")

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting employees: {e}")
        flash("Employees could not be deleted because related records require attention. No employee records were removed.", "danger")

    return redirect(url_for('admin.employees_list'))


# ─── Create Employee / Register Intern ────────────────────────────────────────

@admin_bp.route('/employees/create', methods=['GET', 'POST'])
@login_required
def create_employee():
    from app.services.career_integration import CareerIntegrationService
    from app.services.problem_allocation_service import ProblemAllocationService
    from dateutil.relativedelta import relativedelta
    from werkzeug.utils import secure_filename

    plans = InternshipPlan.query.filter_by(is_active=True).all()
    colleges = College.query.filter_by(is_active=True).order_by(College.name.asc()).all()
    departments = Department.query.filter_by(is_active=True).order_by(Department.name.asc()).all()
    approved_mentors = get_approved_mentors()
    projects = Project.query.filter_by(is_active=True).order_by(Project.title.asc()).all()

    if request.method == 'POST':
        reg_mode = request.form.get('reg_mode', '').strip()
        application_no = request.form.get('application_no', '').strip().upper()

        # Pathway A: Career Application ID Import (preserves 100% of existing behavior and test cases)
        if application_no and reg_mode != 'manual':
            duration_plan = request.form.get('duration_plan', '1_MONTH_PROJECT').strip()

            # Validate application ID on server side
            app = CareerIntegrationService.get_application_by_id(application_no)
            if not app:
                flash(f'Application ID "{application_no}" not found in the system.', 'danger')
                return render_template(
                    'admin/create_employee.html',
                    plans=plans,
                    colleges=colleges,
                    departments=departments,
                    approved_mentors=approved_mentors,
                    projects=projects
                )

            is_valid, err_msg, err_code = CareerIntegrationService.validate_application_for_onboarding(app)
            if not is_valid:
                if err_msg == 'Employee already exists.':
                    flash(f'An employee account already exists for application {application_no}. Employee ID: {app.converted_employee_id}', 'warning')
                else:
                    flash(err_msg, 'danger')
                return render_template(
                    'admin/create_employee.html',
                    plans=plans,
                    colleges=colleges,
                    departments=departments,
                    approved_mentors=approved_mentors,
                    projects=projects
                )

            try:
                student, new_user, emp_id, temp_password, allocated_problem = CareerIntegrationService.create_or_link_employee_from_application(
                    app=app,
                    duration_plan=duration_plan,
                    admin_user=current_user,
                    raw_ip=request.remote_addr
                )

                problem_text = f" and assigned Problem {allocated_problem.project_code} ({allocated_problem.title})" if allocated_problem else ""
                flash(f'Employee {new_user.full_name} ({emp_id}) created successfully from Application {app.application_no}{problem_text}!', 'success')
                return render_template(
                    'admin/create_employee.html',
                    plans=plans,
                    colleges=colleges,
                    departments=departments,
                    approved_mentors=approved_mentors,
                    projects=projects,
                    created=True,
                    created_employee_id=emp_id,
                    created_temp_password=temp_password,
                    created_name=new_user.full_name,
                    created_email=new_user.email,
                    created_student_id=student.id,
                    created_status=student.active_internship.status if student.active_internship else 'ACTIVE',
                    created_problem_id=allocated_problem.project_code if allocated_problem else None,
                    created_problem_title=allocated_problem.title if allocated_problem else None,
                    allocated_problem=allocated_problem
                )

            except Exception as e:
                db.session.rollback()
                logger.error(f"Failed to create employee from application: {e}")
                flash(f'Failed to create employee: {str(e)}', 'danger')
                return render_template(
                    'admin/create_employee.html',
                    plans=plans,
                    colleges=colleges,
                    departments=departments,
                    approved_mentors=approved_mentors,
                    projects=projects
                )

        # Pathway B: Direct Manual Intern Registration
        else:
            full_name = request.form.get('full_name', '').strip()
            email = request.form.get('email', '').strip().lower()
            phone = request.form.get('phone', '').strip()
            gender = request.form.get('gender', '').strip()
            dob = request.form.get('dob', '').strip()
            college_id = request.form.get('college_id')
            custom_college = request.form.get('custom_college', '').strip()
            department_id = request.form.get('department_id')
            custom_department = request.form.get('custom_department', '').strip()
            roll_number = request.form.get('roll_number', '').strip()
            degree = request.form.get('degree', 'B.Tech / B.E.').strip()
            academic_year = request.form.get('academic_year', '3rd Year').strip()
            duration_plan = request.form.get('duration_plan', '1_MONTH_PROJECT').strip()
            start_date_val = request.form.get('start_date', '').strip()
            end_date_val = request.form.get('end_date', '').strip()
            initial_status = request.form.get('initial_status', 'ACTIVE').strip().upper()
            mentor_id = request.form.get('mentor_id')
            project_id = request.form.get('project_id')

            if not full_name:
                flash('Full name is required for intern registration.', 'danger')
                return render_template(
                    'admin/create_employee.html',
                    plans=plans,
                    colleges=colleges,
                    departments=departments,
                    approved_mentors=approved_mentors,
                    projects=projects
                )

            if not email or '@' not in email:
                flash('A valid email address is required.', 'danger')
                return render_template(
                    'admin/create_employee.html',
                    plans=plans,
                    colleges=colleges,
                    departments=departments,
                    approved_mentors=approved_mentors,
                    projects=projects
                )

            # Strict Duplicate Email Prevention
            existing_user = User.query.filter(db.func.lower(User.email) == email).first()
            if existing_user:
                flash(f'An account with email "{email}" already exists (Employee ID: {existing_user.employee_id or "N/A"}). Duplicate email addresses are not permitted.', 'danger')
                return render_template(
                    'admin/create_employee.html',
                    plans=plans,
                    colleges=colleges,
                    departments=departments,
                    approved_mentors=approved_mentors,
                    projects=projects
                )

            try:
                # 1. College Resolution
                college = None
                if college_id and str(college_id).isdigit():
                    college = College.query.get(int(college_id))
                elif custom_college:
                    college = College.query.filter(College.name.ilike(custom_college)).first()
                    if not college:
                        code = (''.join(c for c in custom_college.upper() if c.isalnum())[:8] or 'COL')
                        college = College(
                            code=f"{code}-{random.randint(100, 999)}",
                            name=custom_college,
                            state='Tamil Nadu',
                            city='Chennai'
                        )
                        db.session.add(college)
                        db.session.flush()
                if not college:
                    college = College.query.first()

                # 2. Department Resolution
                department = None
                if department_id and str(department_id).isdigit():
                    department = Department.query.get(int(department_id))
                elif custom_department:
                    department = Department.query.filter(
                        Department.college_id == college.id,
                        Department.name.ilike(custom_department)
                    ).first()
                    if not department:
                        code = (''.join(c for c in custom_department.upper() if c.isalnum())[:6] or 'DEPT')
                        department = Department(college_id=college.id, code=code, name=custom_department)
                        db.session.add(department)
                        db.session.flush()
                if not department:
                    department = Department.query.filter_by(college_id=college.id).first() or Department.query.first()

                # 3. Plan Resolution
                duration_months = 3 if '3' in duration_plan else 1
                plan = (InternshipPlan.query.filter_by(duration_months=duration_months).first() or 
                        InternshipPlan.query.filter_by(plan_code=duration_plan).first() or 
                        InternshipPlan.query.first())

                # 4. Generate Unique Intern ID & Password
                emp_id = generate_next_employee_id()
                temp_password = generate_temp_password()

                # 5. Create User
                new_user = User(
                    email=email,
                    employee_id=emp_id,
                    role='student',
                    full_name=full_name,
                    phone=phone or None,
                    is_active=True
                )
                new_user.set_password(temp_password)
                db.session.add(new_user)
                db.session.flush()

                # 6. Create Student
                student = Student(
                    user_id=new_user.id,
                    student_uid=emp_id,
                    dob=dob or None,
                    gender=gender or None,
                    college_id=college.id if college else 1,
                    department_id=department.id if department else 1,
                    roll_number=roll_number or emp_id,
                    degree=degree or 'B.Tech / B.E.',
                    current_year=academic_year or '3rd Year',
                    graduation_year='2026',
                    is_verified=True
                )
                db.session.add(student)
                db.session.flush()

                # 7. Date calculations
                today_dt = datetime.utcnow().date()
                if start_date_val:
                    try:
                        start_dt = datetime.strptime(start_date_val, '%Y-%m-%d').date()
                    except Exception:
                        start_dt = today_dt
                else:
                    start_dt = today_dt

                if end_date_val:
                    try:
                        end_dt = datetime.strptime(end_date_val, '%Y-%m-%d').date()
                    except Exception:
                        end_dt = start_dt + relativedelta(months=duration_months)
                else:
                    end_dt = start_dt + relativedelta(months=duration_months)

                start_str = start_dt.strftime('%d %b %Y')
                end_str = end_dt.strftime('%d %b %Y')

                # 8. Lifecycle Status
                valid_statuses = ['REGISTERED', 'ONBOARDING', 'ALLOCATED', 'ACTIVE', 'COMPLETED', 'ARCHIVED']
                chosen_status = initial_status if initial_status in valid_statuses else 'ACTIVE'

                # 9. Mentor Resolution
                assigned_mentor_id = None
                if mentor_id and str(mentor_id).isdigit():
                    m = User.query.get(int(mentor_id))
                    if m and is_approved_mentor(m):
                        assigned_mentor_id = m.id
                if not assigned_mentor_id and approved_mentors:
                    assigned_mentor_id = approved_mentors[0].id

                # 10. Create Internship
                internship = Internship(
                    internship_no=emp_id,
                    student_id=student.id,
                    plan_id=plan.id,
                    status=chosen_status,
                    start_date=start_str,
                    end_date=end_str,
                    progress_percent=0,
                    current_stage='Week 1 Understanding & Setup',
                    mentor_id=assigned_mentor_id
                )
                db.session.add(internship)
                db.session.flush()

                # 11. Project Assignment (if project selected)
                allocated_project = None
                if project_id and str(project_id).isdigit():
                    allocated_project = Project.query.get(int(project_id))
                    if allocated_project:
                        ProblemAllocationService.assign_problem_to_internship(internship, allocated_project)

                # 12. Handle Document Upload
                if 'document' in request.files:
                    doc_file = request.files['document']
                    if doc_file and doc_file.filename:
                        raw_filename = secure_filename(doc_file.filename)
                        ext = raw_filename.rsplit('.', 1)[-1].lower() if '.' in raw_filename else ''
                        if ext in ['pdf', 'doc', 'docx', 'png', 'jpg', 'jpeg']:
                            upload_dir = os.path.join(current_app.config.get('UPLOAD_FOLDER', 'app/static/uploads'), 'resumes')
                            os.makedirs(upload_dir, exist_ok=True)
                            safe_name = f"{emp_id}_resume_{int(datetime.utcnow().timestamp())}.{ext}"
                            file_path = os.path.join(upload_dir, safe_name)
                            doc_file.save(file_path)
                            file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

                            doc_record = Document(
                                student_id=student.id,
                                doc_type='RESUME',
                                file_name=raw_filename,
                                file_path=os.path.join('resumes', safe_name),
                                file_size=file_size,
                                mime_type=doc_file.content_type or ('application/pdf' if ext == 'pdf' else 'image/jpeg'),
                                status='VERIFIED',
                                verified_by=current_user.id,
                                verified_at=datetime.utcnow()
                            )
                            db.session.add(doc_record)

                # 13. Audit Log Entry
                audit = AuditLog(
                    actor_id=current_user.id,
                    actor_role=current_user.role,
                    action='REGISTER_INTERN_MANUAL',
                    target_entity='students',
                    target_id=str(student.id),
                    details_json=json.dumps({
                        'student_uid': emp_id,
                        'full_name': full_name,
                        'email': email,
                        'status': chosen_status,
                        'duration_months': duration_months,
                        'college': college.name if college else '',
                        'mentor_id': assigned_mentor_id,
                        'timestamp': datetime.utcnow().isoformat()
                    }),
                    ip_address=request.remote_addr
                )
                db.session.add(audit)

                db.session.commit()
                flash(f'Intern {new_user.full_name} ({emp_id}) successfully registered with status {chosen_status}!', 'success')

                return render_template(
                    'admin/create_employee.html',
                    plans=plans,
                    colleges=colleges,
                    departments=departments,
                    approved_mentors=approved_mentors,
                    projects=projects,
                    created=True,
                    created_employee_id=emp_id,
                    created_temp_password=temp_password,
                    created_name=new_user.full_name,
                    created_email=new_user.email,
                    created_student_id=student.id,
                    created_status=chosen_status,
                    created_problem_id=allocated_project.project_code if allocated_project else None,
                    created_problem_title=allocated_project.title if allocated_project else None,
                    allocated_problem=allocated_project
                )

            except Exception as e:
                db.session.rollback()
                logger.error(f"Error registering intern: {e}")
                flash(f'Registration failed: {str(e)}', 'danger')
                return render_template(
                    'admin/create_employee.html',
                    plans=plans,
                    colleges=colleges,
                    departments=departments,
                    approved_mentors=approved_mentors,
                    projects=projects
                )

    return render_template(
        'admin/create_employee.html',
        plans=plans,
        colleges=colleges,
        departments=departments,
        approved_mentors=approved_mentors,
        projects=projects
    )


# ─── Edit Intern Profile ──────────────────────────────────────────────────────

@admin_bp.route('/employees/<int:student_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_employee(student_id):
    student = Student.query.get_or_404(student_id)
    user = student.user
    internship = student.active_internship

    colleges = College.query.filter_by(is_active=True).order_by(College.name.asc()).all()
    departments = Department.query.filter_by(is_active=True).order_by(Department.name.asc()).all()
    plans = InternshipPlan.query.filter_by(is_active=True).all()
    approved_mentors = get_approved_mentors()

    if request.method == 'POST':
        new_full_name = request.form.get('full_name', '').strip()
        new_email = request.form.get('email', '').strip().lower()
        new_phone = request.form.get('phone', '').strip()
        college_id = request.form.get('college_id', type=int)
        department_id = request.form.get('department_id', type=int)
        degree = request.form.get('degree', '').strip()
        roll_number = request.form.get('roll_number', '').strip()
        current_year = request.form.get('current_year', '').strip()
        start_date = request.form.get('start_date', '').strip()
        end_date = request.form.get('end_date', '').strip()
        plan_id = request.form.get('plan_id', type=int)
        status_val = request.form.get('status', '').strip().upper()
        mentor_id = request.form.get('mentor_id', type=int)

        if not new_full_name:
            flash('Full name cannot be empty.', 'danger')
            return render_template(
                'admin/edit_employee.html',
                student=student,
                user=user,
                internship=internship,
                colleges=colleges,
                departments=departments,
                plans=plans,
                approved_mentors=approved_mentors
            )

        if not new_email or '@' not in new_email:
            flash('A valid email address is required.', 'danger')
            return render_template(
                'admin/edit_employee.html',
                student=student,
                user=user,
                internship=internship,
                colleges=colleges,
                departments=departments,
                plans=plans,
                approved_mentors=approved_mentors
            )

        # Check duplicate email against other users
        if new_email != user.email:
            dup_user = User.query.filter(db.func.lower(User.email) == new_email, User.id != user.id).first()
            if dup_user:
                flash(f'Email "{new_email}" is already used by another account (User ID: {dup_user.id}).', 'danger')
                return render_template(
                    'admin/edit_employee.html',
                    student=student,
                    user=user,
                    internship=internship,
                    colleges=colleges,
                    departments=departments,
                    plans=plans,
                    approved_mentors=approved_mentors
                )
            user.email = new_email

        user.full_name = new_full_name
        user.phone = new_phone or None

        if college_id:
            student.college_id = college_id
        if department_id:
            student.department_id = department_id
        if degree:
            student.degree = degree
        if roll_number:
            student.roll_number = roll_number
        if current_year:
            student.current_year = current_year

        if internship:
            if plan_id:
                internship.plan_id = plan_id
            if start_date:
                internship.start_date = start_date
            if end_date:
                internship.end_date = end_date
            if status_val in ['REGISTERED', 'ONBOARDING', 'ALLOCATED', 'ACTIVE', 'COMPLETED', 'ARCHIVED']:
                internship.status = status_val
            if mentor_id:
                m = User.query.get(mentor_id)
                if m and is_approved_mentor(m):
                    internship.mentor_id = m.id

        # Record Audit Log
        audit = AuditLog(
            actor_id=current_user.id,
            actor_role=current_user.role,
            action='EDIT_INTERN_RECORD',
            target_entity='students',
            target_id=str(student.id),
            details_json=json.dumps({
                'full_name': new_full_name,
                'email': new_email,
                'status': status_val if internship else None,
                'timestamp': datetime.utcnow().isoformat()
            }),
            ip_address=request.remote_addr
        )
        db.session.add(audit)

        db.session.commit()
        flash(f'Profile and internship details for {user.full_name} updated successfully.', 'success')
        return redirect(url_for('admin.employee_detail', student_id=student.id))

    return render_template(
        'admin/edit_employee.html',
        student=student,
        user=user,
        internship=internship,
        colleges=colleges,
        departments=departments,
        plans=plans,
        approved_mentors=approved_mentors
    )


# ─── Lifecycle Status Transition ──────────────────────────────────────────────

@admin_bp.route('/employees/<int:student_id>/status', methods=['POST'])
@login_required
def update_intern_status(student_id):
    student = Student.query.get_or_404(student_id)
    internship = student.active_internship
    if not internship:
        flash('No active internship record found to transition.', 'danger')
        return redirect(url_for('admin.employee_detail', student_id=student.id))

    new_status = request.form.get('new_status', '').strip().upper()
    valid_statuses = ['REGISTERED', 'ONBOARDING', 'ALLOCATED', 'ACTIVE', 'COMPLETED', 'ARCHIVED']

    if new_status not in valid_statuses:
        flash(f'Invalid status "{new_status}". Must be one of: {", ".join(valid_statuses)}.', 'danger')
        return redirect(url_for('admin.employee_detail', student_id=student.id))

    old_status = internship.status
    internship.status = new_status

    # Safe lifecycle access adjustment
    if new_status == 'ARCHIVED':
        student.user.is_active = False
    elif new_status in ['ACTIVE', 'ALLOCATED', 'ONBOARDING', 'REGISTERED']:
        student.user.is_active = True

    audit = AuditLog(
        actor_id=current_user.id,
        actor_role=current_user.role,
        action='TRANSITION_INTERNSHIP_STATUS',
        target_entity='internships',
        target_id=str(internship.id),
        details_json=json.dumps({
            'student_id': student.id,
            'student_uid': student.student_uid,
            'previous_status': old_status,
            'new_status': new_status,
            'timestamp': datetime.utcnow().isoformat()
        }),
        ip_address=request.remote_addr
    )
    db.session.add(audit)

    db.session.commit()
    flash(f'Internship status successfully changed from {old_status} to {new_status}.', 'success')
    return redirect(url_for('admin.employee_detail', student_id=student.id))


# ─── Toggle Account Active Access ─────────────────────────────────────────────

@admin_bp.route('/employees/<int:student_id>/toggle-active', methods=['POST'])
@login_required
def toggle_intern_active(student_id):
    student = Student.query.get_or_404(student_id)
    user = student.user
    if not user:
        flash('No associated user account found.', 'danger')
        return redirect(url_for('admin.employee_detail', student_id=student.id))

    user.is_active = not user.is_active
    state_str = 'activated' if user.is_active else 'deactivated'

    audit = AuditLog(
        actor_id=current_user.id,
        actor_role=current_user.role,
        action='TOGGLE_USER_ACTIVE',
        target_entity='users',
        target_id=str(user.id),
        details_json=json.dumps({
            'is_active': user.is_active,
            'student_uid': student.student_uid,
            'timestamp': datetime.utcnow().isoformat()
        }),
        ip_address=request.remote_addr
    )
    db.session.add(audit)

    db.session.commit()
    flash(f'User portal access for {user.full_name} has been {state_str}.', 'info')
    return redirect(url_for('admin.employee_detail', student_id=student.id))


# ─── Archive Intern ───────────────────────────────────────────────────────────

@admin_bp.route('/employees/<int:student_id>/archive', methods=['POST'])
@login_required
def archive_intern(student_id):
    student = Student.query.get_or_404(student_id)
    internship = student.active_internship
    if internship:
        internship.status = 'ARCHIVED'
    if student.user:
        student.user.is_active = False

    audit = AuditLog(
        actor_id=current_user.id,
        actor_role=current_user.role,
        action='ARCHIVE_INTERN',
        target_entity='students',
        target_id=str(student.id),
        details_json=json.dumps({
            'student_uid': student.student_uid,
            'full_name': student.user.full_name if student.user else '',
            'timestamp': datetime.utcnow().isoformat()
        }),
        ip_address=request.remote_addr
    )
    db.session.add(audit)

    db.session.commit()
    flash(f'Intern {student.user.full_name if student.user else student.student_uid} has been safely archived. All historical records are preserved.', 'success')
    return redirect(url_for('admin.employees_list'))


# ─── Interns Aliases ──────────────────────────────────────────────────────────

@admin_bp.route('/interns')
@login_required
def interns_list():
    return redirect(url_for('admin.employees_list'))


@admin_bp.route('/interns/<int:student_id>')
@login_required
def intern_detail(student_id):
    return redirect(url_for('admin.employee_detail', student_id=student_id))


# ─── Students (legacy alias) ──────────────────────────────────────────────────

@admin_bp.route('/students')
@login_required
def students_list():
    return redirect(url_for('admin.employees_list'))


@admin_bp.route('/students/<int:student_id>')
@login_required
def student_detail(student_id):
    return redirect(url_for('admin.employee_detail', student_id=student_id))


# ─── Projects Directory / Project Database ────────────────────────────────────

@admin_bp.route('/projects')
@login_required
def projects_list():
    query = request.args.get('q', '').strip()
    duration = request.args.get('duration', '').strip()
    status_filter = request.args.get('status', 'all').strip().lower()

    projects_query = Project.query

    if duration in ['1', '3']:
        projects_query = projects_query.filter(Project.duration_months == int(duration))

    if status_filter == 'active':
        projects_query = projects_query.filter(Project.is_active == True, Project.is_archived == False)
    elif status_filter == 'archived':
        projects_query = projects_query.filter(Project.is_archived == True)
    elif status_filter == 'inactive':
        projects_query = projects_query.filter(Project.is_active == False, Project.is_archived == False)

    if query:
        search = f'%{query}%'
        projects_query = projects_query.filter(
            db.or_(
                Project.project_code.ilike(search),
                Project.title.ilike(search),
                Project.domain.ilike(search),
                Project.tech_stack_json.ilike(search)
            )
        )

    projects = projects_query.order_by(Project.id.desc()).all()

    # Preload active colleges count in a single batch query
    if projects:
        project_ids = [p.id for p in projects]
        college_counts_query = db.session.query(
            ProjectAssignment.project_id,
            db.func.count(db.func.distinct(Student.college_id))
        ).join(
            Internship, Internship.id == ProjectAssignment.internship_id
        ).join(
            Student, Student.id == Internship.student_id
        ).filter(
            ProjectAssignment.project_id.in_(project_ids),
            ProjectAssignment.status.in_(['ASSIGNED', 'IN_PROGRESS', 'SUBMITTED', 'REVISION_REQUIRED', 'UNDER_EVALUATION', 'ACTIVE']),
            Internship.status == 'ACTIVE'
        ).group_by(ProjectAssignment.project_id).all()
        
        counts_map = dict(college_counts_query)
        for p in projects:
            p._preloaded_active_colleges_count = counts_map.get(p.id, 0)

    # Consolidated count queries
    counts_by_duration = dict(
        db.session.query(
            Project.duration_months,
            db.func.count(Project.id)
        ).group_by(Project.duration_months).all()
    )
    count_1m = counts_by_duration.get(1, 0)
    count_3m = counts_by_duration.get(3, 0)
    count_all = count_1m + count_3m

    count_active = Project.query.filter(Project.is_active == True, Project.is_archived == False).count()
    count_archived = Project.query.filter(Project.is_archived == True).count()
    total_allocated_interns = ProjectAssignment.query.filter(
        ProjectAssignment.status.in_(['ASSIGNED', 'IN_PROGRESS', 'SUBMITTED', 'REVISION_REQUIRED', 'UNDER_EVALUATION', 'ACTIVE'])
    ).join(Internship).filter(Internship.status == 'ACTIVE').count()

    return render_template(
        'admin/projects.html',
        projects=projects,
        search_query=query,
        current_duration=duration,
        current_status=status_filter,
        count_1m=count_1m,
        count_3m=count_3m,
        count_all=count_all,
        count_active=count_active,
        count_archived=count_archived,
        total_allocated_interns=total_allocated_interns
    )


# ─── Create Project ───────────────────────────────────────────────────────────

@admin_bp.route('/projects/create', methods=['GET', 'POST'])
@login_required
def create_project():
    if request.method == 'POST':
        project_code = request.form.get('project_code', '').strip().upper()
        title = request.form.get('title', '').strip()
        domain = request.form.get('domain', '').strip()
        description = request.form.get('description', '').strip()
        problem_statement = request.form.get('problem_statement', '').strip()
        expected_outcome = request.form.get('expected_outcome', '').strip()
        duration_months = int(request.form.get('duration_months', 1))
        difficulty = request.form.get('difficulty', 'Intermediate').strip()
        max_capacity_str = request.form.get('max_capacity', '10').strip()
        max_capacity = int(max_capacity_str) if max_capacity_str.isdigit() else 10
        status_val = request.form.get('status', 'ACTIVE').strip().upper()
        is_archived = (status_val == 'ARCHIVED')
        is_active = (status_val == 'ACTIVE')
        tech_stack_raw = request.form.get('tech_stack', '').strip()
        objectives_raw = request.form.get('objectives', '').strip()

        if not title or not domain or not description:
            flash('Project Title, Domain / Category, and Description are required.', 'danger')
            return render_template('admin/create_project.html')

        duration_weeks = 4 if duration_months == 1 else 12

        # Generate or validate project code
        if not project_code:
            prefix = f'AM-{duration_months}M'
            count = Project.query.filter(Project.project_code.like(f'{prefix}-%')).count()
            project_code = f'{prefix}-{count + 1:03d}'
            while Project.query.filter_by(project_code=project_code).first():
                count += 1
                project_code = f'{prefix}-{count + 1:03d}'
        else:
            existing = Project.query.filter_by(project_code=project_code).first()
            if existing:
                flash(f'Project ID "{project_code}" already exists. Please choose a unique Project ID.', 'danger')
                return render_template('admin/create_project.html')

        tech_list = [t.strip() for t in tech_stack_raw.split(',') if t.strip()]
        obj_list = [o.strip() for o in objectives_raw.split('\n') if o.strip()]

        try:
            project = Project(
                project_code=project_code,
                title=title,
                domain=domain,
                description=description,
                problem_statement=problem_statement,
                expected_outcome=expected_outcome,
                objectives_json=json.dumps(obj_list),
                tech_stack_json=json.dumps(tech_list),
                requirements_json=json.dumps([]),
                instructions_md=f'Follow the {duration_weeks}-week structured milestone roadmap.',
                reference_links_json=json.dumps([]),
                duration_weeks=duration_weeks,
                duration_months=duration_months,
                difficulty=difficulty,
                max_capacity=max_capacity,
                is_archived=is_archived,
                is_active=is_active
            )
            db.session.add(project)
            db.session.flush()

            # Create ProjectWeek entries for each week (4 or 12 weeks)
            for w_num in range(1, duration_weeks + 1):
                week_title_raw = request.form.get(f'week_{w_num}_title', '').strip()
                week_title = week_title_raw or f'Week {w_num} Milestone'
                week_desc = request.form.get(f'week_{w_num}_description', '').strip()
                week_obj = request.form.get(f'week_{w_num}_objective', '').strip()
                week_instr = request.form.get(f'week_{w_num}_instructions', '').strip()

                pw = ProjectWeek(
                    project_id=project.id,
                    week_number=w_num,
                    title=week_title,
                    description=week_desc,
                    objective=week_obj,
                    instructions=week_instr,
                    deliverables_json=json.dumps([])
                )
                db.session.add(pw)
                db.session.flush()

                # Process tasks for this week
                tasks_raw = request.form.get(f'week_{w_num}_tasks', '[]')
                try:
                    tasks_data = json.loads(tasks_raw) if tasks_raw else []
                except (json.JSONDecodeError, ValueError):
                    tasks_data = []

                for t_data in tasks_data:
                    if not t_data.get('title'):
                        continue
                    pt = ProjectTask(
                        week_id=pw.id,
                        title=t_data.get('title', '').strip(),
                        description=t_data.get('description', '').strip(),
                        instructions=t_data.get('instructions', '').strip(),
                        expected_output=t_data.get('expected_output', '').strip(),
                        priority=t_data.get('priority', 'Medium'),
                        estimated_hours=float(t_data.get('estimated_hours') or 0) or None
                    )
                    db.session.add(pt)

            db.session.commit()
            flash(f'Project "{title}" ({project_code}) created successfully with {duration_weeks} weeks!', 'success')
            return redirect(url_for('admin.edit_project', project_id=project.id))

        except Exception as e:
            db.session.rollback()
            flash(f'Failed to create project: {str(e)}', 'danger')
            return render_template('admin/create_project.html')

    # Default suggested codes for GET
    p1_count = Project.query.filter(Project.project_code.like('AM-1M-%')).count()
    suggested_1m_code = f'AM-1M-{p1_count + 1:03d}'
    p3_count = Project.query.filter(Project.project_code.like('AM-3M-%')).count()
    suggested_3m_code = f'AM-3M-{p3_count + 1:03d}'

    return render_template(
        'admin/create_project.html',
        suggested_1m_code=suggested_1m_code,
        suggested_3m_code=suggested_3m_code
    )


# ─── Upload Tasks (JSON Import) ───────────────────────────────────────────────

@admin_bp.route('/upload-tasks')
@login_required
def upload_tasks():
    recent_imports = TaskImportHistory.query.order_by(TaskImportHistory.id.desc()).limit(15).all()
    total_projects = Project.query.count()
    return render_template('admin/upload_tasks.html', recent_imports=recent_imports, total_projects=total_projects)


@admin_bp.route('/upload-tasks/validate', methods=['POST'])
@login_required
def validate_task_upload():
    from app.services.task_import_service import TaskImportService
    selected_duration = request.form.get('duration', '1 Month').strip()

    if 'file' not in request.files:
        return jsonify({
            'success': False,
            'valid': False,
            'errors': ['No file uploaded. Please choose a JSON file.'],
            'warnings': [],
            'preview': None,
            'error': 'No file uploaded. Please choose a JSON file.'
        }), 400

    file = request.files['file']
    if not file or file.filename == '':
        return jsonify({
            'success': False,
            'valid': False,
            'errors': ['No file selected. Please choose a JSON file.'],
            'warnings': [],
            'preview': None,
            'error': 'No file selected. Please choose a JSON file.'
        }), 400

    if not file.filename.lower().endswith('.json'):
        return jsonify({
            'success': False,
            'valid': False,
            'errors': ['Invalid file type. Only .json files are permitted.'],
            'warnings': [],
            'preview': None,
            'error': 'Invalid file type. Only .json files are permitted.'
        }), 400

    try:
        content = file.read().decode('utf-8')
        if not content.strip():
            return jsonify({
                'success': False,
                'valid': False,
                'errors': ['Uploaded JSON file is empty.'],
                'warnings': [],
                'preview': None,
                'error': 'Uploaded JSON file is empty.'
            }), 400

        is_valid, err_msg, preview_data, parsed_payload = TaskImportService.validate_and_parse(
            json_content=content,
            selected_duration=selected_duration,
            filename=file.filename
        )
        if not is_valid:
            return jsonify({
                'success': False,
                'valid': False,
                'errors': [err_msg] if err_msg else ['Invalid task plan JSON format.'],
                'warnings': [],
                'preview': None,
                'error': err_msg
            }), 400

        is_dup = preview_data.get('is_duplicate', False)
        dup_code = preview_data.get('existing_project_code')
        warnings = []
        if is_dup:
            warnings.append(f"Problem ID '{dup_code or preview_data.get('problem_id')}' already exists in database. Final import will prompt to overwrite.")

        return jsonify({
            'success': True,
            'valid': True,
            'errors': [],
            'warnings': warnings,
            'preview': preview_data,
            'is_duplicate': is_dup,
            'existing_project_code': dup_code
        })
    except UnicodeDecodeError:
        return jsonify({
            'success': False,
            'valid': False,
            'errors': ['File encoding error: The file is not a valid UTF-8 encoded text file.'],
            'warnings': [],
            'preview': None,
            'error': 'File encoding error: The file is not a valid UTF-8 encoded text file.'
        }), 400
    except Exception as e:
        logger.exception("Error validating task plan JSON")
        return jsonify({
            'success': False,
            'valid': False,
            'errors': ['Task plan validation failed due to a server error.'],
            'warnings': [],
            'preview': None,
            'error': 'Task plan validation failed due to a server error.'
        }), 500


@admin_bp.route('/upload-tasks/import', methods=['POST'])
@login_required
def import_task_upload():
    from app.services.task_import_service import TaskImportService
    selected_duration = request.form.get('duration', '1 Month').strip()
    overwrite = request.form.get('overwrite', 'false').lower() in ['true', '1', 'yes']

    if 'file' not in request.files:
        return jsonify({
            'success': False,
            'valid': False,
            'errors': ['No file uploaded. Please choose a JSON file.'],
            'error': 'No file uploaded. Please choose a JSON file.'
        }), 400

    file = request.files['file']
    if not file or not file.filename.lower().endswith('.json'):
        return jsonify({
            'success': False,
            'valid': False,
            'errors': ['Invalid file type. Only .json files are permitted.'],
            'error': 'Invalid file type. Only .json files are permitted.'
        }), 400

    try:
        content = file.read().decode('utf-8')
        if not content.strip():
            return jsonify({
                'success': False,
                'valid': False,
                'errors': ['Uploaded JSON file is empty.'],
                'error': 'Uploaded JSON file is empty.'
            }), 400

        is_valid, err_msg, preview_data, parsed_payload = TaskImportService.validate_and_parse(
            json_content=content,
            selected_duration=selected_duration,
            filename=file.filename
        )
        if not is_valid:
            return jsonify({
                'success': False,
                'valid': False,
                'errors': [err_msg] if err_msg else ['Invalid task plan JSON format.'],
                'error': err_msg
            }), 400

        project, action_type = TaskImportService.commit_import(
            parsed_payload=parsed_payload,
            admin_user=current_user,
            overwrite=overwrite
        )

        flash(f'Task plan "{project.title}" ({project.project_code}) {action_type.lower()} successfully!', 'success')
        return jsonify({
            'success': True,
            'valid': True,
            'project_id': project.id,
            'project_code': project.project_code,
            'action': action_type,
            'redirect_url': url_for('admin.projects_list')
        })
    except ValueError as ve:
        return jsonify({
            'success': False,
            'valid': False,
            'errors': [str(ve)],
            'error': str(ve),
            'is_duplicate': True
        }), 400
    except Exception as e:
        logger.exception("Error importing task plan JSON")
        return jsonify({
            'success': False,
            'valid': False,
            'errors': ['Task plan could not be imported due to an internal server error.'],
            'error': 'Task plan could not be imported. No changes were made.'
        }), 500


@admin_bp.route('/task-imports')
@login_required
def task_imports_history():
    imports = TaskImportHistory.query.order_by(TaskImportHistory.id.desc()).all()
    return render_template('admin/upload_tasks.html', recent_imports=imports, total_projects=Project.query.count())


# ─── Edit Project ─────────────────────────────────────────────────────────────

@admin_bp.route('/projects/<int:project_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_project(project_id):
    project = Project.query.get_or_404(project_id)

    if request.method == 'POST':
        # Project ID is immutable and cannot be changed!
        project.title = request.form.get('title', project.title).strip()
        project.domain = request.form.get('domain', project.domain).strip()
        project.description = request.form.get('description', project.description).strip()
        project.problem_statement = request.form.get('problem_statement', project.problem_statement or '').strip()
        project.expected_outcome = request.form.get('expected_outcome', project.expected_outcome or '').strip()
        project.difficulty = request.form.get('difficulty', project.difficulty).strip()
        
        status_val = request.form.get('status', project.status).strip().upper()
        if status_val == 'ARCHIVED':
            project.is_archived = True
            project.is_active = False
        elif status_val == 'ACTIVE':
            project.is_archived = False
            project.is_active = True
        elif status_val == 'INACTIVE':
            project.is_archived = False
            project.is_active = False

        max_capacity_str = request.form.get('max_capacity', '').strip()
        if max_capacity_str.isdigit():
            project.max_capacity = int(max_capacity_str)

        tech_stack_raw = request.form.get('tech_stack', '').strip()
        if tech_stack_raw:
            project.tech_stack_json = json.dumps([t.strip() for t in tech_stack_raw.split(',') if t.strip()])

        objectives_raw = request.form.get('objectives', '').strip()
        if objectives_raw:
            project.objectives_json = json.dumps([o.strip() for o in objectives_raw.split('\n') if o.strip()])

        db.session.commit()
        flash(f'Project "{project.title}" updated successfully.', 'success')
        return redirect(url_for('admin.edit_project', project_id=project.id))

    weeks = project.project_weeks.all()
    return render_template('admin/edit_project.html', project=project, weeks=weeks)


@admin_bp.route('/projects/<int:project_id>/toggle-status', methods=['POST'])
@login_required
def toggle_project_status(project_id):
    project = Project.query.get_or_404(project_id)
    project.is_active = not project.is_active
    db.session.commit()
    new_status = 'ACTIVE' if project.is_active else 'INACTIVE'
    flash(f'Project "{project.project_code}" is now {new_status}.', 'info')
    return redirect(request.referrer or url_for('admin.projects_list'))


@admin_bp.route('/projects/<int:project_id>/archive', methods=['POST'])
@login_required
def archive_project(project_id):
    project = Project.query.get_or_404(project_id)
    project.is_archived = not project.is_archived
    if project.is_archived:
        project.is_active = False
    else:
        project.is_active = True
    db.session.commit()
    audit = AuditLog(
        actor_id=current_user.id if current_user.is_authenticated else 1,
        actor_role=getattr(current_user, 'role', 'admin'),
        action='ARCHIVE_PROJECT' if project.is_archived else 'RESTORE_PROJECT',
        target_entity='projects',
        target_id=str(project.id),
        details_json=json.dumps({'project_code': project.project_code, 'status': project.status}),
        ip_address=request.remote_addr
    )
    db.session.add(audit)
    db.session.commit()
    flash(f'Project "{project.project_code}" is now {project.status}.', 'info')
    return redirect(request.referrer or url_for('admin.projects_list'))


# ─── Week & Task Management ───────────────────────────────────────────────────

@admin_bp.route('/weeks/<int:week_id>/edit', methods=['POST'])
@login_required
def edit_week(week_id):
    week = ProjectWeek.query.get_or_404(week_id)
    week.title = request.form.get('title', week.title).strip()
    week.description = request.form.get('description', week.description or '').strip()
    week.objective = request.form.get('objective', week.objective or '').strip()
    week.instructions = request.form.get('instructions', week.instructions or '').strip()
    db.session.commit()
    flash(f'Week {week.week_number} details updated.', 'success')
    return redirect(url_for('admin.edit_project', project_id=week.project_id))


@admin_bp.route('/projects/<int:project_id>/weeks/<int:week_id>/tasks/add', methods=['POST'])
@login_required
def add_task(project_id, week_id):
    week = ProjectWeek.query.get_or_404(week_id)
    if week.project_id != project_id:
        abort(403)

    title = request.form.get('title', '').strip()
    if not title:
        flash('Task title is required.', 'danger')
        return redirect(url_for('admin.edit_project', project_id=project_id))

    task = ProjectTask(
        week_id=week_id,
        title=title,
        description=request.form.get('description', '').strip(),
        instructions=request.form.get('instructions', '').strip(),
        expected_output=request.form.get('expected_output', '').strip(),
        priority=request.form.get('priority', 'Medium'),
        estimated_hours=float(request.form.get('estimated_hours') or 0) or None
    )
    db.session.add(task)
    db.session.commit()
    flash(f'Task "{title}" added to Week {week.week_number}.', 'success')
    return redirect(url_for('admin.edit_project', project_id=project_id))


@admin_bp.route('/tasks/<int:task_id>/edit', methods=['POST'])
@login_required
def edit_task(task_id):
    task = ProjectTask.query.get_or_404(task_id)
    project_id = task.week.project_id

    task.title = request.form.get('title', task.title).strip()
    task.description = request.form.get('description', task.description or '').strip()
    task.instructions = request.form.get('instructions', task.instructions or '').strip()
    task.expected_output = request.form.get('expected_output', task.expected_output or '').strip()
    task.priority = request.form.get('priority', task.priority)
    eh = request.form.get('estimated_hours', '')
    task.estimated_hours = float(eh) if eh else None

    db.session.commit()
    flash(f'Task "{task.title}" updated.', 'success')
    return redirect(url_for('admin.edit_project', project_id=project_id))


@admin_bp.route('/tasks/<int:task_id>/delete', methods=['POST'])
@login_required
def delete_task(task_id):
    task = ProjectTask.query.get_or_404(task_id)
    project_id = task.week.project_id
    title = task.title
    db.session.delete(task)
    db.session.commit()
    flash(f'Task "{title}" deleted.', 'success')
    return redirect(url_for('admin.edit_project', project_id=project_id))


# ─── API: Get tasks for a project week ───────────────────────────────────────

@admin_bp.route('/api/project/<int:project_id>/weeks')
@login_required
def api_project_weeks(project_id):
    project = Project.query.get_or_404(project_id)
    weeks_data = []
    for week in project.project_weeks.all():
        tasks = []
        for task in week.tasks.all():
            tasks.append({
                'id': task.id,
                'title': task.title,
                'description': task.description or '',
                'priority': task.priority,
                'estimated_hours': task.estimated_hours,
            })
        weeks_data.append({
            'id': week.id,
            'week_number': week.week_number,
            'title': week.title,
            'objective': week.objective or '',
            'instructions': week.instructions or '',
            'tasks': tasks
        })
    return jsonify({'success': True, 'weeks': weeks_data})


# ─── Assign Project Page ──────────────────────────────────────────────────────

@admin_bp.route('/assign-project')
@admin_bp.route('/assign-tasks')
@login_required
def assign_tasks():
    query = request.args.get('q', '').strip()
    duration = request.args.get('duration', '').strip()

    students_query = Student.query.join(User, Student.user_id == User.id)\
        .join(Internship, Internship.student_id == Student.id)\
        .join(InternshipPlan, Internship.plan_id == InternshipPlan.id)\
        .filter(User.is_active == True, Internship.status == 'ACTIVE')

    if duration in ['1', '3']:
        students_query = students_query.filter(InternshipPlan.duration_months == int(duration))

    if query:
        search = f'%{query}%'
        students_query = students_query.join(College, Student.college_id == College.id, isouter=True)\
            .filter(
                db.or_(
                    User.full_name.ilike(search),
                    Student.student_uid.ilike(search),
                    College.name.ilike(search)
                )
            )

    students = students_query.order_by(Student.id.desc()).all()
    return render_template('admin/assign_tasks.html', students=students, search_query=query, current_duration=duration)


@admin_bp.route('/assign-project/<int:student_id>', methods=['GET', 'POST'])
@admin_bp.route('/assign-tasks/<int:student_id>', methods=['GET', 'POST'])
@admin_bp.route('/employees/<int:student_id>/assign-project', methods=['GET', 'POST'])
@login_required
def assign_project_to_student(student_id):
    student = Student.query.get_or_404(student_id)
    internship = student.active_internship

    if not internship:
        flash(f'{student.user.full_name} does not have an active internship.', 'danger')
        return redirect(url_for('admin.assign_tasks'))

    student_duration_months = internship.plan.duration_months if internship.plan else 1

    # Fetch active projects matching duration
    projects = Project.query.filter_by(
        duration_months=student_duration_months,
        is_active=True
    ).order_by(Project.id.desc()).all()

    existing_assignment = internship.active_assignment

    # Calculate availability for each project for this student's college
    for p in projects:
        conflict_assignment = db.session.query(ProjectAssignment)\
            .join(Internship, ProjectAssignment.internship_id == Internship.id)\
            .join(Student, Internship.student_id == Student.id)\
            .filter(
                ProjectAssignment.project_id == p.id,
                ProjectAssignment.status.in_(['ASSIGNED', 'IN_PROGRESS', 'SUBMITTED', 'REVISION_REQUIRED', 'UNDER_EVALUATION', 'ACTIVE']),
                Internship.status == 'ACTIVE',
                Student.college_id == student.college_id,
                Student.id != student.id
            ).first()

        if conflict_assignment:
            p.is_available_for_student = False
            p.unavailable_reason = f"Already assigned to another active student from {student.college.name}"
        else:
            p.is_available_for_student = True
            p.unavailable_reason = None

    if request.method == 'POST':
        project_id = request.form.get('project_id', type=int)
        deadline = request.form.get('deadline', '').strip()

        if not project_id:
            flash('Please select a project to assign.', 'danger')
            return render_template(
                'admin/assign_project_detail.html',
                student=student,
                internship=internship,
                projects=projects,
                existing_assignment=existing_assignment,
                approved_mentors=get_approved_mentors()
            )

        project = Project.query.get_or_404(project_id)

        # 1. Check if project is active
        if not project.is_active:
            flash(f'Project "{project.title}" is currently inactive and cannot be assigned.', 'danger')
            return redirect(url_for('admin.assign_project_to_student', student_id=student.id))

        # 2. Check duration compatibility
        if project.duration_months != student_duration_months:
            flash(f'Project duration ({project.duration_months} Month) does not match student internship duration ({student_duration_months} Month).', 'danger')
            return redirect(url_for('admin.assign_project_to_student', student_id=student.id))

        # 3. CRITICAL VALIDATION: Project Uniqueness by College for Active Assignments
        conflict = db.session.query(ProjectAssignment)\
            .join(Internship, ProjectAssignment.internship_id == Internship.id)\
            .join(Student, Internship.student_id == Student.id)\
            .filter(
                ProjectAssignment.project_id == project.id,
                ProjectAssignment.status.in_(['ASSIGNED', 'IN_PROGRESS', 'SUBMITTED', 'REVISION_REQUIRED', 'UNDER_EVALUATION', 'ACTIVE']),
                Internship.status == 'ACTIVE',
                Student.college_id == student.college_id,
                Student.id != student.id
            ).first()

        if conflict:
            flash(f'Project {project.project_code} has already been assigned to another active student from {student.college.name}. Please select a different project.', 'danger')
            return redirect(url_for('admin.assign_project_to_student', student_id=student.id))

        try:
            if existing_assignment:
                existing_assignment.status = 'COMPLETED'

            # Optional mentor assignment
            mentor_id = request.form.get('mentor_id', type=int)
            if mentor_id:
                mentor = User.query.get(mentor_id)
                if mentor and is_approved_mentor(mentor):
                    internship.mentor_id = mentor.id

            # Create new ProjectAssignment
            assignment = ProjectAssignment(
                internship_id=internship.id,
                project_id=project.id,
                assigned_by=current_user.id,
                deadline=deadline or internship.end_date,
                status='IN_PROGRESS',
                description=f'Project assigned by {current_user.full_name} on {datetime.utcnow().strftime("%d %b %Y")}'
            )
            db.session.add(assignment)
            db.session.flush()

            # Clone ProjectWeeks and ProjectTasks to WeeklyMilestones and WeeklyTasks
            weeks = project.project_weeks.all()
            now = datetime.utcnow()
            for pw in weeks:
                m_status = 'AVAILABLE' if pw.week_number == 1 else 'LOCKED'
                milestone = WeeklyMilestone(
                    assignment_id=assignment.id,
                    week_number=pw.week_number,
                    title=f'Week {pw.week_number}: {pw.title}',
                    objective=pw.objective or pw.title,
                    instructions=pw.instructions or '',
                    deliverables_json=pw.deliverables_json or '[]',
                    status=m_status,
                    started_at=now if m_status == 'AVAILABLE' else None,
                    due_at=(now + timedelta(days=7)) if m_status == 'AVAILABLE' else None,
                    unlocked_at=now if m_status == 'AVAILABLE' else None
                )
                db.session.add(milestone)
                db.session.flush()

                for pt in pw.tasks.all():
                    wt = WeeklyTask(
                        milestone_id=milestone.id,
                        task_text=pt.title,
                        is_completed=False,
                        order_num=pt.id
                    )
                    db.session.add(wt)

            # Notification
            notif = Notification(
                user_id=student.user.id,
                title=f'Project Assigned: {project.title}',
                message=f'You have been assigned the project "{project.title}" ({project.project_code}) by {current_user.full_name}. Week 1 is now available.',
                type='SUCCESS',
                link='/project'
            )
            db.session.add(notif)

            # Audit
            audit = AuditLog(
                actor_id=current_user.id,
                actor_role=current_user.role,
                action='ASSIGN_PROJECT',
                target_entity='project_assignments',
                target_id=str(assignment.id),
                details_json=json.dumps({
                    'project_id': project.id,
                    'project_code': project.project_code,
                    'student_id': student.id,
                    'college_id': student.college_id,
                    'college_name': student.college.name
                }),
                ip_address=request.remote_addr
            )
            db.session.add(audit)

            # Update internship stage
            internship.current_stage = 'Week 1 Requirement & Architecture'
            internship.progress_percent = 0

            db.session.commit()
            flash(f'Project "{project.title}" ({project.project_code}) successfully assigned to {student.user.full_name}. Week 1 is AVAILABLE.', 'success')
            return redirect(url_for('admin.employee_detail', student_id=student.id))

        except Exception as e:
            db.session.rollback()
            flash(f'Failed to assign project: {str(e)}', 'danger')
            return redirect(url_for('admin.assign_project_to_student', student_id=student.id))

    return render_template(
        'admin/assign_project_detail.html',
        student=student,
        internship=internship,
        projects=projects,
        existing_assignment=existing_assignment,
        approved_mentors=get_approved_mentors()
    )


# ─── Milestone Review ─────────────────────────────────────────────────────────

@admin_bp.route('/milestone/<int:milestone_id>/review', methods=['POST'])
@login_required
def review_milestone(milestone_id):
    milestone = WeeklyMilestone.query.get_or_404(milestone_id)
    action = request.form.get('action', 'approve')
    feedback = request.form.get('feedback', '').strip()
    student = milestone.assignment.internship.student

    if action == 'approve':
        milestone.status = 'COMPLETED'
        milestone.completed_at = db.func.now()
        milestone.approved_by = current_user.id
        milestone.approved_at = db.func.now()
        milestone.admin_notes = feedback or 'Milestone reviewed and approved.'

        latest_sub = milestone.latest_submission
        if latest_sub:
            latest_sub.status = 'APPROVED'
            latest_sub.review_feedback = feedback
            latest_sub.reviewed_by = current_user.id
            latest_sub.reviewed_at = db.func.now()

        next_milestone = WeeklyMilestone.query.filter_by(
            assignment_id=milestone.assignment_id,
            week_number=milestone.week_number + 1
        ).first()

        if next_milestone:
            next_milestone.status = 'AVAILABLE'
            next_milestone.unlocked_at = db.func.now()
            notif = Notification(
                user_id=student.user.id,
                title=f'Week {milestone.week_number} Approved — Week {next_milestone.week_number} Unlocked!',
                message=f'Your submission for Week {milestone.week_number} was approved by {current_user.full_name}. Week {next_milestone.week_number} is now available.',
                type='SUCCESS',
                link=url_for('student.week_detail', milestone_id=next_milestone.id)
            )
            db.session.add(notif)
            flash(f'Week {milestone.week_number} marked COMPLETED. Week {next_milestone.week_number} UNLOCKED for {student.user.full_name}.', 'success')
        else:
            milestone.assignment.status = 'COMPLETED'
            milestone.assignment.internship.status = 'COMPLETED'
            notif = Notification(
                user_id=student.user.id,
                title='All Internship Milestones Completed!',
                message=f'Congratulations! You have completed all weekly project milestones for {milestone.assignment.project.title}.',
                type='SUCCESS',
                link=url_for('student.project_detail')
            )
            db.session.add(notif)
            flash(f'Final Week {milestone.week_number} approved! Student has completed all milestones.', 'success')

    elif action == 'revision':
        milestone.status = 'REVISION_REQUIRED'
        milestone.admin_notes = feedback
        latest_sub = milestone.latest_submission
        if latest_sub:
            latest_sub.status = 'REVISION_REQUIRED'
            latest_sub.review_feedback = feedback
            latest_sub.reviewed_by = current_user.id
            latest_sub.reviewed_at = db.func.now()
        notif = Notification(
            user_id=student.user.id,
            title=f'Revision Requested: Week {milestone.week_number}',
            message=f'Your mentor reviewed Week {milestone.week_number} and requested revisions: {feedback}',
            type='WARNING',
            link=url_for('student.week_detail', milestone_id=milestone.id)
        )
        db.session.add(notif)
        flash(f'Revision request submitted for Week {milestone.week_number}.', 'warning')

    elif action == 'unlock':
        milestone.status = 'AVAILABLE'
        milestone.unlocked_at = db.func.now()
        notif = Notification(
            user_id=student.user.id,
            title=f'Week {milestone.week_number} Manually Unlocked',
            message=f'Administrator unlocked Week {milestone.week_number} ({milestone.title}) for your project.',
            type='INFO',
            link=url_for('student.week_detail', milestone_id=milestone.id)
        )
        db.session.add(notif)
        flash(f'Week {milestone.week_number} manually unlocked.', 'info')

    milestone.assignment.internship.update_progress()
    db.session.commit()
    return redirect(url_for('admin.employee_detail', student_id=student.id))


# ─── Schedule Meeting ─────────────────────────────────────────────────────────

@admin_bp.route('/meeting/schedule', methods=['POST'])
@login_required
def schedule_meeting():
    student_id = request.form.get('student_id', type=int)
    student = Student.query.get_or_404(student_id)
    internship = student.active_internship

    title = request.form.get('title', '').strip()
    meeting_date = request.form.get('meeting_date', '').strip()
    meeting_time = request.form.get('meeting_time', '').strip()
    meeting_link = request.form.get('meeting_link', '').strip()
    host_id = request.form.get('host_id', type=int) or current_user.id
    milestone_id = request.form.get('milestone_id', type=int) or None
    meeting_notes = request.form.get('meeting_notes', '').strip()

    meeting = Meeting(
        milestone_id=milestone_id,
        internship_id=internship.id if internship else 1,
        student_id=student.id,
        host_id=host_id,
        title=title,
        meeting_date=meeting_date,
        meeting_time=meeting_time,
        meeting_link=meeting_link,
        status='SCHEDULED',
        meeting_notes=meeting_notes
    )
    db.session.add(meeting)

    notif = Notification(
        user_id=student.user.id,
        title=f'Evaluation Meeting Scheduled: {title}',
        message=f'Your mentor scheduled an evaluation session on {meeting_date} at {meeting_time}. Meeting link: {meeting_link}',
        type='INFO',
        link=url_for('student.meetings')
    )
    db.session.add(notif)
    db.session.commit()

    flash(f"Meeting '{title}' scheduled successfully for {student.user.full_name}.", 'success')
    return redirect(url_for('admin.employee_detail', student_id=student.id))


# ─── Manage Employee Tasks Dashboard ──────────────────────────────────────────

@admin_bp.route('/employee-tasks')
@login_required
def employee_tasks():
    # Filter params
    duration_filter = request.args.get('duration', 'all').lower()
    status_filter = request.args.get('status', 'all').lower()
    search_query = request.args.get('q', '').strip()

    # Metrics
    now = datetime.utcnow()
    seven_days_ago = now - timedelta(days=7)
    pending_count = WeeklySubmission.query.filter(WeeklySubmission.status.in_(['SUBMITTED', 'UNDER_REVIEW'])).count()
    redo_count = WeeklySubmission.query.filter(WeeklySubmission.status.in_(['REDO_REQUIRED', 'REVISION_REQUIRED'])).count()
    approved_this_week_count = WeeklySubmission.query.filter(
        WeeklySubmission.status.in_(['APPROVED', 'COMPLETED']),
        WeeklySubmission.reviewed_at >= seven_days_ago
    ).count()
    completed_weeks_count = WeeklyMilestone.query.filter(WeeklyMilestone.status.in_(['COMPLETED', 'APPROVED'])).count()

    # Query for submissions with eager loaded joins
    query = WeeklySubmission.query.options(
        contains_eager(WeeklySubmission.student).contains_eager(Student.user),
        contains_eager(WeeklySubmission.student).contains_eager(Student.college),
        contains_eager(WeeklySubmission.milestone).contains_eager(WeeklyMilestone.assignment).contains_eager(ProjectAssignment.project)
    ).join(
        WeeklyMilestone, WeeklySubmission.milestone_id == WeeklyMilestone.id
    ).join(
        ProjectAssignment, WeeklyMilestone.assignment_id == ProjectAssignment.id
    ).join(
        Project, ProjectAssignment.project_id == Project.id
    ).join(
        Student, WeeklySubmission.student_id == Student.id
    ).join(
        User, Student.user_id == User.id
    ).join(
        College, Student.college_id == College.id
    )

    # Duration filter
    if duration_filter == '1m':
        query = query.filter((Project.duration_months == 1) | (Project.duration_weeks == 4))
    elif duration_filter == '3m':
        query = query.filter((Project.duration_months == 3) | (Project.duration_weeks == 12))

    # Status filter
    if status_filter in ['submitted', 'under_review']:
        query = query.filter(WeeklySubmission.status.in_(['SUBMITTED', 'UNDER_REVIEW']))
    elif status_filter in ['redo_required', 'redo']:
        query = query.filter(WeeklySubmission.status.in_(['REDO_REQUIRED', 'REVISION_REQUIRED']))
    elif status_filter == 'approved':
        query = query.filter(WeeklySubmission.status == 'APPROVED')
    elif status_filter == 'completed':
        query = query.filter(WeeklySubmission.status == 'COMPLETED')

    # Search filter
    if search_query:
        search_pattern = f'%{search_query}%'
        query = query.outerjoin(Application, Application.student_id == Student.id).filter(
            (User.full_name.ilike(search_pattern)) |
            (User.employee_id.ilike(search_pattern)) |
            (Student.student_uid.ilike(search_pattern)) |
            (College.name.ilike(search_pattern)) |
            (Project.project_code.ilike(search_pattern)) |
            (Project.title.ilike(search_pattern)) |
            (Application.application_no.ilike(search_pattern))
        )

    submissions_list = query.order_by(WeeklySubmission.id.desc()).all()

    return render_template(
        'admin/employee_tasks.html',
        submissions=submissions_list,
        pending_count=pending_count,
        redo_count=redo_count,
        approved_this_week_count=approved_this_week_count,
        completed_weeks_count=completed_weeks_count,
        duration_filter=duration_filter,
        status_filter=status_filter,
        search_query=search_query
    )


# ─── Submission Review Page ───────────────────────────────────────────────────

@admin_bp.route('/submissions/<int:submission_id>/review')
@login_required
def review_submission(submission_id):
    submission = WeeklySubmission.query.get_or_404(submission_id)
    milestone = submission.milestone
    assignment = milestone.assignment
    student = submission.student
    project = assignment.project

    tasks = milestone.tasks.all()
    submission_history = WeeklySubmission.query.filter_by(
        milestone_id=milestone.id
    ).order_by(WeeklySubmission.id.desc()).all()

    evaluations_list = Evaluation.query.filter_by(
        milestone_id=milestone.id
    ).order_by(Evaluation.id.desc()).all()

    # Active Configurable Rubric (Phase 2 Module 5)
    active_rubric = EvaluationRubric.get_active()

    return render_template(
        'admin/submission_review.html',
        submission=submission,
        milestone=milestone,
        assignment=assignment,
        student=student,
        project=project,
        tasks=tasks,
        submission_history=submission_history,
        evaluations=evaluations_list,
        rubric=active_rubric
    )


# ─── Admin Review Action: REDO ────────────────────────────────────────────────

@admin_bp.route('/submissions/<int:submission_id>/redo', methods=['POST'])
@login_required
def submission_action_redo(submission_id):
    submission = WeeklySubmission.query.get_or_404(submission_id)
    student = submission.student

    remarks = request.form.get('admin_remarks', '').strip() or request.form.get('remarks', '').strip()
    if not remarks:
        flash('Admin Remarks are required when requesting a REDO from the student.', 'danger')
        return redirect(url_for('admin.review_submission', submission_id=submission.id))

    scores_dict = {
        'technical_score': request.form.get('technical_score', 40),
        'code_quality_score': request.form.get('code_quality_score', 40),
        'functionality_score': request.form.get('functionality_score', 40),
        'documentation_score': request.form.get('documentation_score', 40),
        'presentation_score': request.form.get('presentation_score', 40)
    }

    try:
        success, msg = SubmissionWorkflowService.process_evaluation_redo(
            submission=submission,
            evaluator=current_user,
            feedback=remarks,
            scores_dict=scores_dict,
            ip_address=request.remote_addr
        )
        if success:
            flash(f'Submission marked REDO REQUIRED. Feedback transmitted to {student.user.full_name}.', 'warning')
        else:
            flash(msg, 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Failed to process REDO action: {str(e)}', 'danger')

    return redirect(url_for('admin.review_submission', submission_id=submission.id))


# ─── Admin Review Action: VERIFY ──────────────────────────────────────────────

@admin_bp.route('/submissions/<int:submission_id>/verify', methods=['POST'])
@login_required
def submission_action_verify(submission_id):
    submission = WeeklySubmission.query.get_or_404(submission_id)
    milestone = submission.milestone
    student = submission.student

    remarks = request.form.get('admin_remarks', '').strip() or request.form.get('remarks', '').strip() or 'Work verified and approved by evaluator.'

    try:
        now = datetime.utcnow()
        submission.status = 'APPROVED'
        submission.review_feedback = remarks
        submission.reviewed_by = current_user.id
        submission.reviewed_at = now

        milestone.status = 'APPROVED'
        milestone.admin_notes = remarks
        milestone.approved_by = current_user.id
        milestone.approved_at = now

        eval_log = Evaluation(
            submission_id=submission.id,
            milestone_id=milestone.id,
            assignment_id=milestone.assignment_id,
            employee_id=student.id,
            admin_id=current_user.id,
            evaluator_id=current_user.id,
            action='VERIFY',
            remarks=remarks,
            feedback=remarks,
            result='VERIFIED',
            evaluated_at=now
        )
        db.session.add(eval_log)

        notif = Notification(
            user_id=student.user.id,
            title=f'Week {milestone.week_number} Verified',
            message=f'Your deliverables for Week {milestone.week_number} have passed verification. Evaluation notes: "{remarks}"',
            type='SUCCESS',
            link=url_for('student.week_detail', milestone_id=milestone.id)
        )
        db.session.add(notif)

        audit = AuditLog(
            actor_id=current_user.id,
            actor_role=current_user.role,
            action='SUBMISSION_VERIFY',
            target_entity='weekly_submissions',
            target_id=str(submission.id),
            details_json=json.dumps({
                'milestone_id': milestone.id,
                'student_id': student.id,
                'remarks': remarks
            }),
            ip_address=request.remote_addr
        )
        db.session.add(audit)

        milestone.assignment.internship.update_progress()
        db.session.commit()

        flash(f'Submission for Week {milestone.week_number} verified successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Failed to verify submission: {str(e)}', 'danger')

    return redirect(url_for('admin.review_submission', submission_id=submission.id))


# ─── Admin Review Action: PROCEED TO NEXT ─────────────────────────────────────

@admin_bp.route('/submissions/<int:submission_id>/proceed-next', methods=['POST'])
@login_required
def submission_action_proceed_next(submission_id):
    submission = WeeklySubmission.query.get_or_404(submission_id)
    milestone = submission.milestone
    student = submission.student

    # Double-click / Duplicate progression protection
    if milestone.status == 'COMPLETED':
        flash(f'Week {milestone.week_number} has already been completed and progressed.', 'info')
        return redirect(url_for('admin.employee_tasks'))

    remarks = request.form.get('admin_remarks', '').strip() or request.form.get('remarks', '').strip() or 'Milestone approved and progressed to next week.'

    scores_dict = {
        'technical_score': request.form.get('technical_score') or request.form.get('score_technical') or 85,
        'code_quality_score': request.form.get('code_quality_score') or request.form.get('score_code_quality') or 85,
        'functionality_score': request.form.get('functionality_score') or request.form.get('score_functionality') or 85,
        'documentation_score': request.form.get('documentation_score') or request.form.get('score_documentation') or 80,
        'presentation_score': request.form.get('presentation_score') or request.form.get('score_presentation') or 80
    }

    try:
        success, total_score = SubmissionWorkflowService.process_evaluation_approval(
            submission=submission,
            evaluator=current_user,
            scores_dict=scores_dict,
            feedback=remarks,
            ip_address=request.remote_addr
        )
        if success:
            flash(f'Week {milestone.week_number} approved! Total Score: {total_score}/100. Deliverables recorded and progression updated for {student.user.full_name}.', 'success')
        else:
            flash(f'Notice: {total_score}', 'warning')
    except Exception as e:
        db.session.rollback()
        flash(f'Failed to progress milestone: {str(e)}', 'danger')

    return redirect(url_for('admin.employee_tasks'))


# ─── Protected Admin Video Stream ─────────────────────────────────────────────

@admin_bp.route('/submission/<int:sub_id>/video')
@login_required
def stream_admin_submission_video(sub_id):
    """Securely stream demo video for authenticated administrators."""
    sub = WeeklySubmission.query.get_or_404(sub_id)

    filename = sub.demo_video_url or sub.demo_video_path or sub.file_path
    if not filename:
        abort(404)

    # Redirect directly if Google Drive URL or HTTP URL
    if filename.startswith(('http://', 'https://')) or 'drive.google.com' in filename:
        return redirect(filename)

    upload_folder = current_app.config.get('VIDEO_UPLOAD_FOLDER')
    full_path = os.path.join(upload_folder, filename)

    if not os.path.exists(full_path):
        abort(404)

    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    mimetype = 'video/mp4'
    if ext == 'webm':
        mimetype = 'video/webm'
    elif ext == 'mov':
        mimetype = 'video/quicktime'

    return send_file(full_path, mimetype=mimetype, conditional=True)


# ─── Legacy Evaluations & Meetings Views ──────────────────────────────────────

@admin_bp.route('/evaluations')
@login_required
def evaluations():
    pending_submissions = WeeklySubmission.query.order_by(WeeklySubmission.id.desc()).all()
    return render_template('admin/evaluations.html', submissions=pending_submissions)


@admin_bp.route('/meetings')
@login_required
def meetings():
    all_meetings = Meeting.query.order_by(Meeting.id.desc()).all()
    return render_template('admin/meetings.html', meetings=all_meetings)


# ─── Configurable Evaluation Rubrics (Phase 2 Module 5) ───────────────────────

@admin_bp.route('/rubrics', methods=['GET', 'POST'])
@login_required
def rubrics():
    if current_user.role not in ['admin', 'super_admin']:
        flash('Access restricted to administrators.', 'danger')
        return abort(403)

    active_rubric = EvaluationRubric.get_active()
    all_rubrics = EvaluationRubric.query.order_by(EvaluationRubric.id.desc()).all()

    if request.method == 'POST':
        name = request.form.get('name', '').strip() or 'Standard Evaluation Rubric'
        desc = request.form.get('description', '').strip()

        try:
            w_tech = int(request.form.get('weight_technical', 25))
            w_code = int(request.form.get('weight_code_quality', 25))
            w_func = int(request.form.get('weight_functionality', 25))
            w_doc = int(request.form.get('weight_documentation', 15))
            w_pres = int(request.form.get('weight_presentation', 10))
        except (ValueError, TypeError):
            flash('Invalid weights provided. All weights must be valid integers.', 'danger')
            return redirect(url_for('admin.rubrics'))

        total_weight = w_tech + w_code + w_func + w_doc + w_pres
        if total_weight != 100:
            flash(f'Total rubric weights must equal exactly 100%. Currently: {total_weight}% ({w_tech}% + {w_code}% + {w_func}% + {w_doc}% + {w_pres}%).', 'danger')
            return render_template('admin/rubrics.html', rubric=active_rubric, rubrics=all_rubrics, form_data=request.form)

        # Update active rubric
        active_rubric.name = name
        active_rubric.description = desc
        active_rubric.weight_technical = w_tech
        active_rubric.weight_code_quality = w_code
        active_rubric.weight_functionality = w_func
        active_rubric.weight_documentation = w_doc
        active_rubric.weight_presentation = w_pres
        active_rubric.updated_at = datetime.utcnow()
        active_rubric.created_by = current_user.id

        audit = AuditLog(
            actor_id=current_user.id,
            actor_role=current_user.role,
            action='UPDATE_EVALUATION_RUBRIC',
            target_entity='evaluation_rubrics',
            target_id=str(active_rubric.id),
            details_json=json.dumps({
                'weights': {
                    'technical': w_tech,
                    'code_quality': w_code,
                    'functionality': w_func,
                    'documentation': w_doc,
                    'presentation': w_pres
                },
                'total_weight': total_weight
            }),
            ip_address=request.remote_addr
        )
        db.session.add(audit)
        db.session.commit()

        flash(f'Evaluation Rubric "{name}" updated successfully. All subsequent submissions will be scored using these 100% weights.', 'success')
        return redirect(url_for('admin.rubrics'))

    return render_template('admin/rubrics.html', rubric=active_rubric, rubrics=all_rubrics)


# ─── Admin Milestone Task Management ──────────────────────────────────────────

@admin_bp.route('/milestones/<int:milestone_id>/tasks/create', methods=['POST'])
@login_required
def create_milestone_task(milestone_id):
    if current_user.role not in ['admin', 'super_admin']:
        return abort(403)

    milestone = WeeklyMilestone.query.get_or_404(milestone_id)
    student = milestone.assignment.internship.student

    task_title = request.form.get('title', '').strip() or request.form.get('task_text', '').strip()
    if not task_title:
        flash('Task title is required.', 'danger')
        return redirect(request.referrer or url_for('admin.student_detail', student_id=student.id))

    desc = request.form.get('description', '').strip()
    priority = request.form.get('priority', 'Medium')
    tech_req = request.form.get('technical_requirements', '').strip()
    due_date_str = request.form.get('due_date', '').strip()

    due_dt = None
    if due_date_str:
        try:
            due_dt = datetime.strptime(due_date_str, '%Y-%m-%d')
        except ValueError:
            pass

    max_order = db.session.query(db.func.max(WeeklyTask.order_num)).filter_by(milestone_id=milestone.id).scalar() or 0

    task = WeeklyTask(
        milestone_id=milestone.id,
        task_text=task_title,
        description=desc,
        priority=priority,
        technical_requirements=tech_req,
        due_date=due_dt,
        status='ASSIGNED',
        order_num=max_order + 1
    )
    db.session.add(task)

    SubmissionWorkflowService.notify_task_assigned(
        student_user_id=student.user.id,
        milestone=milestone,
        task_title=task_title,
        actor_name=current_user.full_name
    )

    db.session.commit()
    flash(f'Task "{task_title}" assigned to Week {milestone.week_number} for {student.user.full_name}.', 'success')
    return redirect(request.referrer or url_for('admin.student_detail', student_id=student.id))


@admin_bp.route('/milestones/tasks/<int:task_id>/edit', methods=['POST'])
@login_required
def edit_milestone_task(task_id):
    if current_user.role not in ['admin', 'super_admin']:
        return abort(403)

    task = WeeklyTask.query.get_or_404(task_id)
    title = request.form.get('title', '').strip() or request.form.get('task_text', '').strip()
    if title:
        task.task_text = title
    task.description = request.form.get('description', task.description or '').strip()
    task.priority = request.form.get('priority', task.priority)
    task.technical_requirements = request.form.get('technical_requirements', task.technical_requirements or '').strip()

    db.session.commit()
    flash(f'Task "{task.task_text}" updated.', 'success')
    return redirect(request.referrer or url_for('admin.employee_tasks'))


@admin_bp.route('/milestones/tasks/<int:task_id>/delete', methods=['POST'])
@login_required
def delete_milestone_task(task_id):
    if current_user.role not in ['admin', 'super_admin']:
        return abort(403)

    task = WeeklyTask.query.get_or_404(task_id)
    title = task.task_text
    milestone = task.milestone
    db.session.delete(task)
    db.session.commit()
    flash(f'Task "{title}" removed from Week {milestone.week_number}.', 'success')
    return redirect(request.referrer or url_for('admin.employee_tasks'))


@admin_bp.route('/milestones/<int:milestone_id>/configure', methods=['POST'])
@login_required
def configure_milestone(milestone_id):
    if current_user.role not in ['admin', 'super_admin']:
        return abort(403)

    milestone = WeeklyMilestone.query.get_or_404(milestone_id)
    objective = request.form.get('objective', '').strip()
    instructions = request.form.get('instructions', '').strip()
    tech_req = request.form.get('technical_requirements', '').strip()
    due_date_str = request.form.get('due_date', '').strip()

    if objective:
        milestone.objective = objective
    if instructions:
        milestone.instructions = instructions
    if tech_req:
        milestone.technical_requirements = tech_req

    if due_date_str:
        try:
            milestone.due_at = datetime.strptime(due_date_str, '%Y-%m-%d')
        except ValueError:
            pass

    db.session.commit()
    flash(f'Week {milestone.week_number} configuration updated.', 'success')
    return redirect(request.referrer or url_for('admin.employee_tasks'))


@admin_bp.route('/projects/<int:project_id>/broadcast-task', methods=['POST'])
@login_required
def broadcast_project_task(project_id):
    if current_user.role not in ['admin', 'super_admin']:
        return abort(403)

    title = request.form.get('task_title', '').strip() or request.form.get('title', '').strip()
    if not title:
        flash('Task title is required to broadcast.', 'danger')
        return redirect(url_for('admin.edit_project', project_id=project_id))

    desc = request.form.get('description', '').strip()
    priority = request.form.get('priority', 'Medium')
    tech_req = request.form.get('technical_requirements', '').strip()

    assigned_count, err = SubmissionWorkflowService.broadcast_task_to_project(
        project_id=project_id,
        task_title=title,
        description=desc,
        priority=priority,
        technical_requirements=tech_req,
        actor=current_user
    )

    if err:
        flash(f'Broadcast failed: {err}', 'danger')
    else:
        flash(f'Task "{title}" successfully broadcasted and assigned to {assigned_count} active interns in this project.', 'success')

    return redirect(url_for('admin.edit_project', project_id=project_id))


# ─── Mentor Management Module (Module 3) ──────────────────────────────────────

@admin_bp.route('/mentors')
@login_required
def mentors_list():
    """
    Enterprise Mentor Directory with real-time workload telemetry, capacity monitoring,
    and allocation status.
    """
    q = request.args.get('q', '').strip()
    status_filter = request.args.get('status', 'ALL').upper()
    availability_filter = request.args.get('availability', 'ALL').upper()
    dept_filter = request.args.get('department', '').strip()

    query = User.query.filter(User.role == 'mentor')

    if status_filter == 'ACTIVE':
        query = query.filter(User.is_active == True)
    elif status_filter == 'INACTIVE':
        query = query.filter(User.is_active == False)

    if availability_filter in ['AVAILABLE', 'BUSY', 'ON_LEAVE']:
        query = query.filter(User.availability_status == availability_filter)

    if dept_filter:
        query = query.filter(User.department == dept_filter)

    mentors = query.order_by(User.id.asc()).all()

    # Search query filtering
    if q:
        q_lower = q.lower()
        mentors = [
            m for m in mentors
            if (
                q_lower in (m.full_name or '').lower() or
                q_lower in (m.email or '').lower() or
                q_lower in (m.employee_id or '').lower() or
                q_lower in (m.department or '').lower() or
                q_lower in (m.skills or '').lower()
            )
        ]

    # Telemetry KPI calculations across all mentors
    all_system_mentors = User.query.filter(User.role == 'mentor').all()
    total_mentors = len(all_system_mentors)
    active_mentors = sum(1 for m in all_system_mentors if m.is_active)
    
    total_assigned_interns = Internship.query.filter(
        Internship.mentor_id.isnot(None),
        Internship.status == 'ACTIVE'
    ).count()

    total_capacity = sum((m.max_capacity or 5) for m in all_system_mentors if m.is_active)
    open_capacity = max(0, total_capacity - total_assigned_interns)
    avg_utilization = int((total_assigned_interns / total_capacity * 100)) if total_capacity > 0 else 0

    # Distinct departments for filtering
    departments = sorted(list({m.department for m in all_system_mentors if m.department}))

    return render_template(
        'admin/mentors.html',
        mentors=mentors,
        total_mentors=total_mentors,
        active_mentors=active_mentors,
        total_assigned_interns=total_assigned_interns,
        open_capacity=open_capacity,
        avg_utilization=avg_utilization,
        departments=departments,
        q=q,
        status_filter=status_filter,
        availability_filter=availability_filter,
        dept_filter=dept_filter
    )


@admin_bp.route('/mentors/create', methods=['GET', 'POST'])
@login_required
def create_mentor():
    """
    Admin-controlled Mentor Onboarding with automatic unique ID generation,
    department assignment, technical skills specification, and capacity limits.
    """
    next_mentor_id = generate_next_mentor_id()

    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        department = request.form.get('department', '').strip() or 'Engineering'
        designation = request.form.get('designation', '').strip() or 'Technical Mentor'
        skills = request.form.get('skills', '').strip() or 'Full-Stack Development'
        max_capacity_str = request.form.get('max_capacity', '5').strip()
        availability_status = request.form.get('availability_status', 'AVAILABLE').upper()
        custom_emp_id = request.form.get('employee_id', '').strip() or next_mentor_id
        password = request.form.get('password', '').strip() or 'Mentor@2026Password!'

        if not full_name:
            flash('Full Name is required.', 'danger')
            return render_template('admin/create_mentor.html', next_mentor_id=next_mentor_id)

        if not email:
            flash('Email address is required.', 'danger')
            return render_template('admin/create_mentor.html', next_mentor_id=next_mentor_id)

        # 1. Duplicate email prevention
        existing_email = User.query.filter(db.func.lower(User.email) == email).first()
        if existing_email:
            flash(f'An account with email "{email}" already exists (Role: {existing_email.role}). Please use a unique email.', 'danger')
            return render_template('admin/create_mentor.html', next_mentor_id=next_mentor_id)

        # 2. Duplicate employee_id check
        existing_id = User.query.filter(db.func.upper(User.employee_id) == custom_emp_id.upper()).first()
        if existing_id:
            flash(f'Mentor ID "{custom_emp_id}" is already assigned to {existing_id.full_name}. Please choose another.', 'danger')
            return render_template('admin/create_mentor.html', next_mentor_id=next_mentor_id)

        try:
            max_capacity = max(1, min(50, int(max_capacity_str)))
        except ValueError:
            max_capacity = 5

        if availability_status not in ['AVAILABLE', 'BUSY', 'ON_LEAVE']:
            availability_status = 'AVAILABLE'

        try:
            mentor = User(
                email=email,
                employee_id=custom_emp_id,
                role='mentor',
                full_name=full_name,
                phone=phone,
                department=department,
                designation=designation,
                skills=skills,
                max_capacity=max_capacity,
                availability_status=availability_status,
                is_active=True
            )
            mentor.set_password(password)
            db.session.add(mentor)
            db.session.flush()

            # Record Audit Log
            audit = AuditLog(
                actor_id=current_user.id,
                actor_role=current_user.role,
                action='CREATE_MENTOR',
                target_entity='users',
                target_id=str(mentor.id),
                details_json=json.dumps({
                    'mentor_id': mentor.id,
                    'employee_id': mentor.employee_id,
                    'full_name': mentor.full_name,
                    'email': mentor.email,
                    'department': mentor.department,
                    'max_capacity': mentor.max_capacity
                }),
                ip_address=request.remote_addr
            )
            db.session.add(audit)
            db.session.commit()

            flash(f'Technical Mentor "{mentor.full_name}" ({mentor.employee_id}) successfully provisioned!', 'success')
            return redirect(url_for('admin.mentors_list'))

        except Exception as e:
            db.session.rollback()
            flash(f'Error creating mentor record: {str(e)}', 'danger')

    return render_template(
        'admin/create_mentor.html',
        next_mentor_id=next_mentor_id
    )


@admin_bp.route('/mentors/<int:mentor_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_mentor(mentor_id):
    """Edit mentor profile, contact, skills, department, and capacity configuration."""
    mentor = User.query.get_or_404(mentor_id)
    if mentor.role != 'mentor':
        flash('Selected user is not a mentor.', 'danger')
        return redirect(url_for('admin.mentors_list'))

    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        department = request.form.get('department', '').strip() or 'Engineering'
        designation = request.form.get('designation', '').strip() or 'Technical Mentor'
        skills = request.form.get('skills', '').strip() or 'Full-Stack Development'
        max_capacity_str = request.form.get('max_capacity', '5').strip()
        availability_status = request.form.get('availability_status', 'AVAILABLE').upper()
        is_active = request.form.get('is_active') == '1'

        if not full_name or not email:
            flash('Full Name and Email are required.', 'danger')
            return render_template('admin/edit_mentor.html', mentor=mentor)

        # Uniqueness check for email
        if email != mentor.email:
            existing = User.query.filter(
                (db.func.lower(User.email) == email) & (User.id != mentor.id)
            ).first()
            if existing:
                flash(f'Email "{email}" is already used by another account.', 'danger')
                return render_template('admin/edit_mentor.html', mentor=mentor)

        try:
            max_capacity = max(1, min(50, int(max_capacity_str)))
        except ValueError:
            max_capacity = 5

        if availability_status not in ['AVAILABLE', 'BUSY', 'ON_LEAVE']:
            availability_status = 'AVAILABLE'

        try:
            old_name = mentor.full_name
            mentor.full_name = full_name
            mentor.name = full_name
            mentor.email = email
            mentor.phone = phone
            mentor.department = department
            mentor.designation = designation
            mentor.skills = skills
            mentor.max_capacity = max_capacity
            mentor.availability_status = availability_status
            mentor.is_active = is_active

            # Optional password update
            new_pw = request.form.get('new_password', '').strip()
            if new_pw:
                mentor.set_password(new_pw)

            audit = AuditLog(
                actor_id=current_user.id,
                actor_role=current_user.role,
                action='EDIT_MENTOR',
                target_entity='users',
                target_id=str(mentor.id),
                details_json=json.dumps({
                    'mentor_id': mentor.id,
                    'full_name': mentor.full_name,
                    'max_capacity': mentor.max_capacity,
                    'is_active': mentor.is_active
                }),
                ip_address=request.remote_addr
            )
            db.session.add(audit)
            db.session.commit()

            flash(f'Mentor profile for "{mentor.full_name}" successfully updated.', 'success')
            return redirect(url_for('admin.mentors_list'))

        except Exception as e:
            db.session.rollback()
            flash(f'Failed to update mentor profile: {str(e)}', 'danger')

    return render_template(
        'admin/edit_mentor.html',
        mentor=mentor
    )


@admin_bp.route('/mentors/<int:mentor_id>')
@login_required
def mentor_detail(mentor_id):
    """Detailed view of a mentor's profile, assigned mentees, workload, and allocation options."""
    mentor = User.query.get_or_404(mentor_id)
    if mentor.role != 'mentor':
        flash('Selected user is not a mentor.', 'danger')
        return redirect(url_for('admin.mentors_list'))

    assigned_internships = Internship.query.filter_by(
        mentor_id=mentor.id
    ).options(
        joinedload(Internship.student).joinedload(Student.user),
        joinedload(Internship.student).joinedload(Student.college),
        joinedload(Internship.plan)
    ).order_by(Internship.id.desc()).all()

    active_internships = [i for i in assigned_internships if i.status == 'ACTIVE']
    completed_internships = [i for i in assigned_internships if i.status == 'COMPLETED']

    # Available active interns who are currently unassigned or assigned elsewhere
    all_active_internships = Internship.query.filter_by(status='ACTIVE').options(
        joinedload(Internship.student).joinedload(Student.user)
    ).all()

    evaluations_conducted = Evaluation.query.filter_by(
        evaluator_id=mentor.id
    ).order_by(Evaluation.evaluated_at.desc()).limit(10).all()

    return render_template(
        'admin/mentor_detail.html',
        mentor=mentor,
        assigned_internships=assigned_internships,
        active_internships=active_internships,
        completed_internships=completed_internships,
        all_active_internships=all_active_internships,
        evaluations_conducted=evaluations_conducted
    )


@admin_bp.route('/mentors/<int:mentor_id>/toggle-active', methods=['POST'])
@login_required
def toggle_mentor_active(mentor_id):
    """Toggle mentor account activation status."""
    mentor = User.query.get_or_404(mentor_id)
    if mentor.role != 'mentor':
        flash('Selected user is not a mentor.', 'danger')
        return redirect(url_for('admin.mentors_list'))

    mentor.is_active = not mentor.is_active
    audit = AuditLog(
        actor_id=current_user.id,
        actor_role=current_user.role,
        action='TOGGLE_MENTOR_ACTIVE',
        target_entity='users',
        target_id=str(mentor.id),
        details_json=json.dumps({
            'mentor_id': mentor.id,
            'is_active': mentor.is_active
        }),
        ip_address=request.remote_addr
    )
    db.session.add(audit)
    db.session.commit()

    state = 'Activated' if mentor.is_active else 'Deactivated'
    flash(f'Mentor account for {mentor.full_name} is now {state}.', 'success')
    return redirect(request.referrer or url_for('admin.mentors_list'))


@admin_bp.route('/mentors/<int:mentor_id>/allocate', methods=['POST'])
@login_required
def allocate_interns_to_mentor(mentor_id):
    """
    Assign or reassign one or more interns to a mentor, validating capacity limits
    and preserving audit logs.
    """
    mentor = User.query.get_or_404(mentor_id)
    if mentor.role != 'mentor':
        flash('Selected user is not a mentor.', 'danger')
        return redirect(url_for('admin.mentors_list'))

    student_ids_raw = request.form.getlist('student_ids')
    single_student_id = request.form.get('student_id')
    if single_student_id and single_student_id not in student_ids_raw:
        student_ids_raw.append(single_student_id)

    student_ids = []
    for sid in student_ids_raw:
        try:
            student_ids.append(int(sid))
        except (ValueError, TypeError):
            pass

    if not student_ids:
        flash('Please select at least one intern to allocate.', 'warning')
        return redirect(url_for('admin.mentor_detail', mentor_id=mentor.id))

    current_assigned = mentor.active_interns_count
    cap = mentor.max_capacity or 5
    new_total = current_assigned + len(student_ids)

    capacity_warning = False
    if new_total > cap:
        capacity_warning = True

    assigned_names = []
    for sid in student_ids:
        student = Student.query.get(sid)
        if not student:
            continue
        internship = student.active_internship
        if not internship:
            continue

        old_mentor_name = internship.assigned_mentor_name or 'Unassigned'
        internship.mentor_id = mentor.id

        audit = AuditLog(
            actor_id=current_user.id,
            actor_role=current_user.role,
            action='MENTOR_ALLOCATION',
            target_entity='internships',
            target_id=str(internship.id),
            details_json=json.dumps({
                'student_id': student.id,
                'student_uid': student.student_uid,
                'old_mentor': old_mentor_name,
                'new_mentor': mentor.full_name,
                'mentor_id': mentor.id
            }),
            ip_address=request.remote_addr
        )
        db.session.add(audit)
        assigned_names.append(student.user.full_name if student.user else f'ID {student.id}')

    db.session.commit()

    if capacity_warning:
        flash(f'Allocated {len(assigned_names)} intern(s) to {mentor.full_name}. Note: Mentor has exceeded designated capacity ({mentor.active_interns_count}/{cap}).', 'warning')
    else:
        flash(f'Successfully allocated {len(assigned_names)} intern(s) to Mentor {mentor.full_name}.', 'success')

    return redirect(url_for('admin.mentor_detail', mentor_id=mentor.id))


# ─── Module 4: Intelligent Project & Mentor Allocation Engine ──────────────────

@admin_bp.route('/allocations')
@login_required
def allocations_hub():
    """
    Intelligent Allocation Hub:
    Overview of unallocated interns, active allocations, project capacities, and allocation audit log.
    """
    # 1. Unallocated active interns (interns whose active internship has no active assignment)
    active_internships = Internship.query.filter_by(status='ACTIVE').all()
    unallocated_students = []
    for i in active_internships:
        curr_ass = i.active_assignment
        if not curr_ass or curr_ass.status in ['COMPLETED', 'PASSED', 'FAILED', 'SUPERSEDED']:
            if i.student and i.student not in unallocated_students:
                unallocated_students.append(i.student)

    # 2. Active allocations
    active_assignments = ProjectAssignment.query.filter(
        ProjectAssignment.status.in_(['ASSIGNED', 'IN_PROGRESS', 'SUBMITTED', 'REVISION_REQUIRED', 'UNDER_EVALUATION', 'ACTIVE'])
    ).join(Internship).filter(Internship.status == 'ACTIVE').order_by(ProjectAssignment.id.desc()).all()

    # Pre-calculate match scores for active assignments
    for a in active_assignments:
        if a.internship and a.internship.student and a.project:
            m_data = ProjectAllocationEngine.calculate_intern_project_match(a.internship.student, a.project)
            a._cached_match_score = m_data['match_score']
            a._cached_match_tier = m_data['match_tier']
        else:
            a._cached_match_score = 75
            a._cached_match_tier = 'GOOD'

    # 3. Available Projects & Mentors
    available_projects = Project.query.filter(Project.is_active == True, Project.is_archived == False).all()
    available_mentors = [m for m in get_all_active_mentors() if m.availability_status != 'ON_LEAVE']

    # 4. Recent allocation audit history
    recent_allocations = AuditLog.query.filter(
        AuditLog.action.in_(['ALLOCATE_PROJECT_INTELLIGENT', 'ASSIGN_PROBLEM', 'MENTOR_ALLOCATION'])
    ).order_by(AuditLog.id.desc()).limit(15).all()

    return render_template(
        'admin/allocations.html',
        unallocated_students=unallocated_students,
        active_assignments=active_assignments,
        available_projects=available_projects,
        available_mentors=available_mentors,
        recent_allocations=recent_allocations
    )


@admin_bp.route('/allocations/wizard', methods=['GET', 'POST'])
@admin_bp.route('/employees/<int:student_id>/allocate', methods=['GET', 'POST'])
@login_required
def allocation_wizard(student_id=None):
    """
    Intelligent Multi-Step Allocation Wizard:
    1. Select Intern & review profile/skills.
    2. Algorithmic Project Matching & Ranking (match score %, matching/missing skills).
    3. Mentor Matching & Telemetry (suitability, capacity, availability).
    4. Atomic confirmation with override recording.
    """
    sid = request.args.get('student_id', type=int) or student_id or request.form.get('student_id', type=int)

    if request.method == 'POST':
        sid = request.form.get('student_id', type=int)
        project_id = request.form.get('project_id', type=int)
        mentor_id = request.form.get('mentor_id', type=int)
        override_reason = request.form.get('override_reason', '').strip()
        force_override = (request.form.get('force_override') == '1' or bool(override_reason))
        deadline = request.form.get('deadline', '').strip()

        if not sid or not project_id or not mentor_id:
            flash('Intern, Project, and Mentor selections are all required to complete allocation.', 'danger')
            return redirect(url_for('admin.allocation_wizard', student_id=sid))

        student = Student.query.get_or_404(sid)
        internship = student.active_internship
        if not internship:
            flash(f'Intern {student.user.full_name} does not have an active internship record.', 'danger')
            return redirect(url_for('admin.allocations_hub'))

        project = Project.query.get_or_404(project_id)
        mentor = User.query.get_or_404(mentor_id)

        assignment, err = ProjectAllocationEngine.execute_allocation(
            internship=internship,
            project=project,
            mentor=mentor,
            assigned_by_user=current_user,
            override_reason=override_reason,
            force_override=force_override,
            deadline=deadline
        )

        if err:
            flash(f'Allocation Blocked: {err}', 'danger')
            return redirect(url_for('admin.allocation_wizard', student_id=student.id, project_id=project.id))

        flash(
            f'Intelligent Allocation Confirmed! {student.user.full_name} ({student.student_uid}) '
            f'allocated to {project.project_code} ({project.title}) under Mentor {mentor.full_name}.',
            'success'
        )
        return redirect(url_for('admin.student_detail', student_id=student.id))

    # GET Request
    if not sid:
        # Show Intern Selection Directory for Allocation
        active_internships = Internship.query.filter_by(status='ACTIVE').all()
        interns = [i.student for i in active_internships if i.student]
        return render_template('admin/select_intern_for_allocation.html', interns=interns)

    student = Student.query.get_or_404(sid)
    internship = student.active_internship
    if not internship:
        flash(f'{student.user.full_name} does not have an active internship.', 'danger')
        return redirect(url_for('admin.allocations_hub'))

    # Calculate recommended projects
    recommended_projects = ProjectAllocationEngine.get_recommended_projects_for_intern(student)

    # Determine currently selected project for mentor matching
    selected_project_id = request.args.get('project_id', type=int)
    selected_project = None
    if selected_project_id:
        selected_project = Project.query.get(selected_project_id)
    if not selected_project and recommended_projects:
        selected_project = recommended_projects[0]['project']

    # Calculate recommended mentors for selected project
    recommended_mentors = []
    if selected_project:
        recommended_mentors = ProjectAllocationEngine.get_recommended_mentors_for_project(selected_project)

    existing_assignment = internship.active_assignment

    return render_template(
        'admin/allocation_wizard.html',
        student=student,
        internship=internship,
        recommended_projects=recommended_projects,
        selected_project=selected_project,
        recommended_mentors=recommended_mentors,
        existing_assignment=existing_assignment
    )


@admin_bp.route('/allocations/api/mentors/<int:project_id>')
@login_required
def api_recommend_mentors(project_id):
    """API endpoint to dynamically return ranked mentor recommendations for a selected project."""
    project = Project.query.get_or_404(project_id)
    mentors_ranked = ProjectAllocationEngine.get_recommended_mentors_for_project(project)
    
    output = []
    for m_data in mentors_ranked:
        m = m_data['mentor']
        output.append({
            'id': m.id,
            'name': m.full_name,
            'email': m.email,
            'department': m.department or 'Engineering',
            'skills': m.skills or 'Full-Stack Development',
            'match_score': m_data['match_score'],
            'suitability_score': m_data['suitability_score'],
            'matching_skills': m_data['matching_skills'],
            'active_interns_count': m_data['active_interns_count'],
            'max_capacity': m_data['max_capacity'],
            'remaining_capacity': m_data['remaining_capacity'],
            'availability_status': m_data['availability_status'],
            'is_available': m_data['is_available'],
            'is_at_capacity': m_data['is_at_capacity'],
            'is_recommended': m_data['is_recommended']
        })
    return jsonify({'project_id': project.id, 'mentors': output})


@admin_bp.route('/allocations/api/students/<int:student_id>/skills', methods=['POST'])
@login_required
def api_update_student_skills(student_id):
    """API endpoint to update student skills inline and trigger recalculation."""
    student = Student.query.get_or_404(student_id)
    data = request.get_json() or {}
    new_skills = data.get('skills', '').strip() or request.form.get('skills', '').strip()
    if new_skills:
        student.skills = new_skills
        db.session.commit()
    return jsonify({
        'success': True,
        'student_id': student.id,
        'skills': student.skills,
        'skills_list': student.skills_list
    })


# ─── 13. Performance Analytics & Reporting (Phase 2 Module 6) ────────────────

@admin_bp.route('/analytics')
@login_required
def analytics():
    """
    Comprehensive organization analytics, telemetry, and business intelligence hub.
    Supports tabbed views: overview, interns, mentors, projects.
    """
    tab = request.args.get('tab', 'overview')
    search = request.args.get('search', '').strip()
    status = request.args.get('status', '').strip()
    project_id = request.args.get('project_id', type=int)
    mentor_id = request.args.get('mentor_id', type=int)
    department = request.args.get('department', '').strip()
    domain = request.args.get('domain', '').strip()
    sort_by = request.args.get('sort_by', 'progress' if tab == 'interns' else ('workload' if tab == 'mentors' else 'allocated'))
    order = request.args.get('order', 'desc')

    kpis = AnalyticsReportingService.get_organization_kpis()
    
    # Query datasets based on active tab or all if requested
    interns_data = AnalyticsReportingService.get_interns_analytics(
        search=search, project_id=project_id, mentor_id=mentor_id,
        status=status, sort_by=sort_by, order=order
    )
    mentors_data = AnalyticsReportingService.get_mentors_analytics(
        search=search, department=department, sort_by=sort_by, order=order
    )
    projects_data = AnalyticsReportingService.get_projects_analytics(
        search=search, domain=domain, status=status, sort_by=sort_by, order=order
    )

    all_projects = Project.query.order_by(Project.title).all()
    all_mentors = User.query.filter(User.role.in_(['mentor', 'lead_mentor', 'employee'])).order_by(User.full_name).all()
    all_departments = Department.query.order_by(Department.name).all()

    # JSON API mode for external consumers / dynamic client rendering
    if request.args.get('format') == 'json':
        return jsonify({
            'kpis': kpis,
            'interns_count': len(interns_data),
            'mentors_count': len(mentors_data),
            'projects_count': len(projects_data)
        })

    return render_template(
        'admin/analytics.html',
        tab=tab,
        kpis=kpis,
        interns_data=interns_data,
        mentors_data=mentors_data,
        projects_data=projects_data,
        all_projects=all_projects,
        all_mentors=all_mentors,
        all_departments=all_departments,
        search=search,
        status=status,
        project_id=project_id,
        mentor_id=mentor_id,
        department=department,
        domain=domain,
        sort_by=sort_by,
        order=order
    )


@admin_bp.route('/analytics/interns/<int:student_id>')
@login_required
def intern_analytics_detail(student_id):
    """Detailed candidate performance scorecard and historical progress metrics."""
    scorecard = AnalyticsReportingService.get_intern_detail_analytics(student_id)
    if not scorecard:
        abort(404)
    return render_template('admin/intern_analytics_detail.html', **scorecard)


@admin_bp.route('/analytics/projects/<int:project_id>')
@login_required
def project_analytics_detail(project_id):
    """Detailed project health, capacity saturation, and assigned intern progression."""
    details = AnalyticsReportingService.get_project_detail_analytics(project_id)
    if not details:
        abort(404)
    return render_template('admin/project_analytics_detail.html', **details)


@admin_bp.route('/reports')
@login_required
def reports_hub():
    """Reporting center for generating, customizing, and exporting executive reports."""
    kpis = AnalyticsReportingService.get_organization_kpis()
    all_projects = Project.query.order_by(Project.title).all()
    all_mentors = User.query.filter(User.role.in_(['mentor', 'lead_mentor', 'employee'])).order_by(User.full_name).all()
    all_departments = Department.query.order_by(Department.name).all()
    return render_template(
        'admin/reports.html',
        kpis=kpis,
        all_projects=all_projects,
        all_mentors=all_mentors,
        all_departments=all_departments
    )


@admin_bp.route('/reports/export/csv')
@login_required
def export_csv_report():
    """Generates RFC 4180 compliant CSV export with attachment headers."""
    report_type = request.args.get('report_type', 'interns_performance')
    valid_reports = ['interns_performance', 'mentors_performance', 'projects_summary', 'organization_monthly', 'internship_completions']
    if report_type not in valid_reports:
        flash(f'Invalid report type "{report_type}".', 'danger')
        return redirect(url_for('admin.reports_hub'))

    filters = {
        'search': request.args.get('search'),
        'project_id': request.args.get('project_id', type=int),
        'mentor_id': request.args.get('mentor_id', type=int),
        'status': request.args.get('status'),
        'department': request.args.get('department'),
        'domain': request.args.get('domain')
    }

    csv_data = AnalyticsReportingService.generate_csv_report(report_type, filters)
    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    filename = f"antimatrix_{report_type}_{timestamp}.csv"

    # Add UTF-8 BOM so Excel opens it without encoding glitches
    bom_csv = '\ufeff' + csv_data

    return Response(
        bom_csv,
        mimetype='text/csv; charset=utf-8',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Type': 'text/csv; charset=utf-8'
        }
    )


@admin_bp.route('/reports/printable/<report_type>')
@login_required
def printable_report(report_type):
    """Printer-ready executive report view for PDF generation and audit documentation."""
    valid_reports = ['interns_performance', 'mentors_performance', 'projects_summary', 'organization_monthly', 'internship_completions']
    if report_type not in valid_reports:
        abort(404)

    filters = {
        'search': request.args.get('search'),
        'project_id': request.args.get('project_id', type=int),
        'mentor_id': request.args.get('mentor_id', type=int),
        'status': request.args.get('status'),
        'department': request.args.get('department'),
        'domain': request.args.get('domain')
    }

    kpis = AnalyticsReportingService.get_organization_kpis()
    records = []
    report_title = "Executive Report"

    if report_type == 'interns_performance':
        report_title = "Candidate Performance & Milestones Telemetry"
        records = AnalyticsReportingService.get_interns_analytics(**filters)
    elif report_type == 'mentors_performance':
        report_title = "Supervisory Performance & Workload Telemetry"
        records = AnalyticsReportingService.get_mentors_analytics(**filters)
    elif report_type == 'projects_summary':
        report_title = "Project Portfolio Health & Progression Summary"
        records = AnalyticsReportingService.get_projects_analytics(**filters)
    elif report_type == 'organization_monthly':
        report_title = "Executive Monthly Operational & Quality Review"
    elif report_type == 'internship_completions':
        report_title = "Internship Completion & Certification Verification Roster"

    generated_at = datetime.utcnow().strftime('%B %d, %Y - %H:%M UTC')

    return render_template(
        'admin/printable_report.html',
        report_type=report_type,
        report_title=report_title,
        kpis=kpis,
        records=records,
        generated_at=generated_at,
        generated_by=current_user.full_name or current_user.email
    )


# ─── Module 7: Broadcast Announcements & Deadline Automation ─────────────────

@admin_bp.route('/announcements', methods=['GET'])
@login_required
def announcements_dashboard():
    announcements_list = Announcement.query.order_by(Announcement.id.desc()).all()
    projects_list = Project.query.filter_by(is_active=True).all()
    departments_list = [d.name for d in Department.query.all()]

    return render_template(
        'admin/announcements.html',
        announcements=announcements_list,
        projects=projects_list,
        departments=departments_list,
        unread_count=current_user.unread_notifications_count
    )


@admin_bp.route('/announcements/create', methods=['POST'])
@login_required
def create_announcement():
    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    target_role = request.form.get('target_role', 'ALL').strip().upper()
    priority = request.form.get('priority', 'NORMAL').strip().upper()
    department = request.form.get('department', '').strip() or None
    project_id_raw = request.form.get('project_id', '').strip()
    project_id = int(project_id_raw) if project_id_raw and project_id_raw.isdigit() else None

    if not title or not content:
        flash('Announcement title and content are required.', 'danger')
        return redirect(url_for('admin.announcements_dashboard'))

    try:
        announcement = NotificationService.create_announcement(
            author_id=current_user.id,
            title=title,
            content=content,
            target_role=target_role,
            department=department,
            project_id=project_id,
            priority=priority
        )
        flash(f"Announcement '{title}' published and broadcast to {target_role} successfully.", 'success')
    except Exception as e:
        logger.error(f"Failed to publish announcement: {e}")
        flash(f"Error publishing announcement: {str(e)}", 'danger')

    return redirect(url_for('admin.announcements_dashboard'))


@admin_bp.route('/announcements/<int:announcement_id>/delete', methods=['POST'])
@login_required
def delete_announcement(announcement_id):
    announcement = Announcement.query.get(announcement_id)
    if not announcement:
        flash('Announcement not found.', 'warning')
        return redirect(url_for('admin.announcements_dashboard'))

    try:
        db.session.delete(announcement)
        db.session.commit()
        flash('Announcement removed successfully.', 'info')
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting announcement {announcement_id}: {e}")
        flash('Failed to delete announcement.', 'danger')

    return redirect(url_for('admin.announcements_dashboard'))


@admin_bp.route('/automation/deadline-check', methods=['POST'])
@login_required
def trigger_deadline_check():
    """
    On-demand administrative trigger to run the automated deadline reminder engine.
    """
    summary = NotificationService.check_and_send_deadline_reminders()

    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'success': True,
            'summary': summary
        })

    flash(
        f"Automated Deadline Check complete: {summary['checked_milestones']} milestones evaluated, "
        f"{summary['reminders_sent']} reminders sent, {summary['overdue_sent']} overdue alerts generated.",
        'success'
    )
    return redirect(request.referrer or url_for('admin.dashboard'))


# ─── MODULE 8: DOCUMENT MANAGEMENT & DIGITAL CERTIFICATES ────────────────────

@admin_bp.route('/documents')
@login_required
def documents_workspace():
    """
    Centralized administrative workspace for corporate documents, letters,
    verification submissions, and digital certificates.
    """
    doc_type_filter = request.args.get('doc_type', 'ALL').strip().upper()
    status_filter = request.args.get('status', 'ALL').strip().upper()
    search_q = request.args.get('q', '').strip()

    # Query Issued Documents
    issued_query = IssuedDocument.query.join(Student, IssuedDocument.student_id == Student.id)
    if doc_type_filter != 'ALL':
        issued_query = issued_query.filter(IssuedDocument.doc_type == doc_type_filter)
    if status_filter != 'ALL':
        issued_query = issued_query.filter(IssuedDocument.status == status_filter)
    if search_q:
        issued_query = issued_query.filter(
            or_(
                IssuedDocument.document_no.ilike(f"%{search_q}%"),
                Student.full_name.ilike(f"%{search_q}%"),
                Student.student_uid.ilike(f"%{search_q}%")
            )
        )
    issued_docs = issued_query.order_by(IssuedDocument.id.desc()).all()

    # Query Uploaded Student Documents
    uploaded_query = Document.query.join(Student, Document.student_id == Student.id)
    if search_q:
        uploaded_query = uploaded_query.filter(
            or_(
                Document.file_name.ilike(f"%{search_q}%"),
                Student.full_name.ilike(f"%{search_q}%"),
                Student.student_uid.ilike(f"%{search_q}%")
            )
        )
    uploaded_docs = uploaded_query.order_by(Document.id.desc()).all()

    # Query All Active Internships for generation selection
    internships = Internship.query.options(joinedload(Internship.student)).filter(
        Internship.status.in_(['ONBOARDING', 'ALLOCATED', 'ACTIVE', 'COMPLETED', 'VERIFIED'])
    ).all()

    # KPI Telemetry
    total_issued_docs = IssuedDocument.query.count()
    total_certificates = IssuedDocument.query.filter_by(doc_type='CERTIFICATE').count()
    active_verifications = CertificateVerification.query.filter(
        CertificateVerification.status.in_(['Successfully Completed', 'VALID', 'VERIFIED'])
    ).count()
    revoked_certificates = CertificateVerification.query.filter(
        CertificateVerification.status.in_(['Revoked', 'REVOKED'])
    ).count()
    pending_uploaded_reviews = Document.query.filter_by(status='UPLOADED').count()

    return render_template(
        'admin/documents.html',
        issued_docs=issued_docs,
        uploaded_docs=uploaded_docs,
        internships=internships,
        doc_type_filter=doc_type_filter,
        status_filter=status_filter,
        search_q=search_q,
        kpi={
            'total_issued_docs': total_issued_docs,
            'total_certificates': total_certificates,
            'active_verifications': active_verifications,
            'revoked_certificates': revoked_certificates,
            'pending_uploaded_reviews': pending_uploaded_reviews
        }
    )


@admin_bp.route('/documents/issue', methods=['POST'])
@login_required
def issue_document():
    """Generates and issues corporate letters (Offer, Joining, Experience, Agreement)."""
    internship_id = request.form.get('internship_id')
    doc_type = request.form.get('doc_type', '').strip().upper()
    notes = request.form.get('notes', '').strip()

    if not internship_id or not doc_type:
        flash('Internship selection and document type are required.', 'danger')
        return redirect(url_for('admin.documents_workspace'))

    try:
        doc = DocumentService.issue_document(
            doc_type=doc_type,
            internship_id=int(internship_id),
            admin_user_id=current_user.id,
            custom_notes=notes
        )
        flash(f"{doc.display_type} ({doc.document_no}) successfully generated and issued.", 'success')
    except Exception as e:
        logger.error(f"Error issuing document: {e}")
        flash(f"Failed to issue document: {str(e)}", 'danger')

    return redirect(request.referrer or url_for('admin.documents_workspace'))


@admin_bp.route('/certificates/issue', methods=['POST'])
@login_required
def issue_certificate():
    """Generates and issues an authentic digital Completion Certificate."""
    internship_id = request.form.get('internship_id')
    override_eligibility = request.form.get('override_eligibility') in ['1', 'true', 'on', True]
    override_reason = request.form.get('override_reason', '').strip()

    if not internship_id:
        flash('Internship selection is required.', 'danger')
        return redirect(url_for('admin.documents_workspace'))

    try:
        issued_doc, cert_ver = DocumentService.issue_certificate(
            internship_id=int(internship_id),
            admin_user_id=current_user.id,
            override_eligibility=override_eligibility,
            override_reason=override_reason
        )
        flash(
            f"Completion Certificate {cert_ver.certificate_no} issued successfully! Digital verification is now live.",
            'success'
        )
    except Exception as e:
        logger.error(f"Error issuing certificate: {e}")
        flash(f"Failed to issue certificate: {str(e)}", 'danger')

    return redirect(request.referrer or url_for('admin.documents_workspace'))


@admin_bp.route('/certificates/<certificate_no>/revoke', methods=['POST'])
@login_required
def revoke_certificate(certificate_no):
    """Revokes a previously issued certificate with a recorded reason."""
    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('A valid revocation reason is required.', 'danger')
        return redirect(request.referrer or url_for('admin.documents_workspace'))

    try:
        DocumentService.revoke_certificate(
            certificate_no=certificate_no.strip(),
            admin_user_id=current_user.id,
            reason=reason
        )
        flash(f"Certificate {certificate_no} has been revoked.", 'warning')
    except Exception as e:
        logger.error(f"Error revoking certificate {certificate_no}: {e}")
        flash(f"Failed to revoke certificate: {str(e)}", 'danger')

    return redirect(request.referrer or url_for('admin.documents_workspace'))


@admin_bp.route('/certificates/<certificate_no>/reissue', methods=['POST'])
@login_required
def reissue_certificate(certificate_no):
    """Reissues a certificate under a fresh certificate ID, marking the previous one superseded."""
    reason = request.form.get('reason', '').strip()
    if not reason:
        reason = "Administrative update and credential reissuance."

    try:
        new_doc, new_cert = DocumentService.reissue_certificate(
            old_certificate_no=certificate_no.strip(),
            admin_user_id=current_user.id,
            reason=reason
        )
        flash(
            f"Certificate {certificate_no} superseded. New certificate {new_cert.certificate_no} issued successfully.",
            'success'
        )
    except Exception as e:
        logger.error(f"Error reissuing certificate {certificate_no}: {e}")
        flash(f"Failed to reissue certificate: {str(e)}", 'danger')

    return redirect(request.referrer or url_for('admin.documents_workspace'))


@admin_bp.route('/documents/<int:document_id>/review', methods=['POST'])
@login_required
def review_uploaded_document(document_id):
    """Admin review endpoint for student verification documents."""
    new_status = request.form.get('status', '').strip().upper()
    rejection_reason = request.form.get('rejection_reason', '').strip()

    try:
        doc = DocumentService.review_student_document(
            document_id=document_id,
            admin_user_id=current_user.id,
            new_status=new_status,
            rejection_reason=rejection_reason
        )
        flash(f"Document #{doc.id} ({doc.file_name}) status updated to {new_status}.", 'info')
    except Exception as e:
        logger.error(f"Error reviewing document {document_id}: {e}")
        flash(f"Failed to update document: {str(e)}", 'danger')

    return redirect(request.referrer or url_for('admin.documents_workspace'))


@admin_bp.route('/documents/<doc_no>/download')
@login_required
def download_issued_document(doc_no):
    """Controlled administrative download for issued corporate documents."""
    try:
        file_path, download_name, mime = DocumentService.get_document_for_download(
            doc_identifier=doc_no,
            current_user_obj=current_user,
            is_issued=True
        )
        return send_file(file_path, mimetype=mime, as_attachment=True, download_name=download_name)
    except PermissionError:
        abort(403)
    except FileNotFoundError:
        abort(404)
    except Exception as e:
        flash(f"Download failed: {str(e)}", 'danger')
        return redirect(url_for('admin.documents_workspace'))


@admin_bp.route('/documents/<doc_no>/preview')
@login_required
def preview_issued_document(doc_no):
    """Controlled administrative inline preview for issued corporate documents."""
    try:
        file_path, download_name, mime = DocumentService.get_document_for_download(
            doc_identifier=doc_no,
            current_user_obj=current_user,
            is_issued=True
        )
        return send_file(file_path, mimetype=mime, as_attachment=False)
    except PermissionError:
        abort(403)
    except FileNotFoundError:
        abort(404)
    except Exception as e:
        flash(f"Preview failed: {str(e)}", 'danger')
        return redirect(url_for('admin.documents_workspace'))


@admin_bp.route('/documents/uploaded/<int:doc_id>/download')
@login_required
def download_admin_uploaded_document(doc_id):
    """Controlled administrative download for student uploaded verification documents."""
    try:
        file_path, download_name, mime = DocumentService.get_document_for_download(
            doc_identifier=doc_id,
            current_user_obj=current_user,
            is_issued=False
        )
        return send_file(file_path, mimetype=mime, as_attachment=True, download_name=download_name)
    except PermissionError:
        abort(403)
    except FileNotFoundError:
        abort(404)
    except Exception as e:
        flash(f"Download failed: {str(e)}", 'danger')
        return redirect(url_for('admin.documents_workspace'))


@admin_bp.route('/documents/uploaded/<int:doc_id>/preview')
@login_required
def preview_admin_uploaded_document(doc_id):
    """Controlled administrative preview for student uploaded verification documents."""
    try:
        file_path, download_name, mime = DocumentService.get_document_for_download(
            doc_identifier=doc_id,
            current_user_obj=current_user,
            is_issued=False
        )
        return send_file(file_path, mimetype=mime, as_attachment=False)
    except PermissionError:
        abort(403)
    except FileNotFoundError:
        abort(404)
    except Exception as e:
        flash(f"Preview failed: {str(e)}", 'danger')
        return redirect(url_for('admin.documents_workspace'))


@admin_bp.route('/internships/<int:internship_id>/certificate-eligibility')
@login_required
def check_certificate_eligibility(internship_id):
    """JSON API to check certificate eligibility for a given internship."""
    internship = db.session.get(Internship, internship_id)
    if not internship:
        return jsonify({'error': 'Internship not found'}), 404
    eligibility = DocumentService.validate_certificate_eligibility(internship)
    return jsonify(eligibility)


# ─── 13. Advanced Attendance, Leave & Scheduling (Phase 2 Module 9) ───────────

@admin_bp.route('/attendance', endpoint='attendance_dashboard')
@login_required
def attendance_dashboard():
    """
    Administrative attendance overview & console:
    - Real-time daily KPIs (checked-in, present, late, half-day, absent, on-leave)
    - Full filterable roster with search by name/ID, status filter, and date selector
    - Inline regularization and manual entry triggers
    """
    date_str = request.args.get('date', '').strip()
    if date_str:
        try:
            target_date = AttendanceService.parse_date(date_str)
        except Exception:
            target_date = datetime.now().date()
    else:
        target_date = datetime.now().date()

    daily_overview = AttendanceService.get_organization_daily_overview(target_date)

    status_filter = request.args.get('status', 'all').upper().strip()
    search_q = request.args.get('q', '').strip()

    # Query attendance records for the target date
    records_query = AttendanceRecord.query.filter_by(date=target_date)
    if status_filter in ['PRESENT', 'LATE', 'HALF_DAY', 'ABSENT', 'ON_LEAVE', 'HOLIDAY']:
        records_query = records_query.filter_by(status=status_filter)

    records = records_query.options(
        joinedload(AttendanceRecord.student).joinedload(Student.user)
    ).all()

    if search_q:
        q_lower = search_q.lower()
        filtered_records = []
        for r in records:
            name = (r.student.user.full_name if r.student and r.student.user else '').lower()
            uid = (r.student.student_uid if r.student else '').lower()
            emp_id = (r.student.user.employee_id if r.student and r.student.user and r.student.user.employee_id else '').lower()
            if q_lower in name or q_lower in uid or q_lower in emp_id:
                filtered_records.append(r)
        records = filtered_records

    # Active students for manual retroactive entry modal
    active_students = Student.query.join(User, Student.user_id == User.id).filter(User.is_active == True).options(joinedload(Student.user)).all()

    return render_template(
        'admin/attendance.html',
        target_date=target_date,
        daily_overview=daily_overview,
        records=records,
        status_filter=status_filter,
        search_q=search_q,
        active_students=active_students
    )


@admin_bp.route('/attendance/<int:record_id>/correct', methods=['POST'])
@login_required
def correct_attendance(record_id):
    """
    Administrative regularization of attendance:
    - Modifies status and timestamps with mandatory audit justification
    """
    new_status = request.form.get('status', '').upper().strip()
    check_in_str = request.form.get('check_in_time', '').strip()
    check_out_str = request.form.get('check_out_time', '').strip()
    reason = request.form.get('reason', '').strip()

    if not reason:
        flash("A clear, documented reason is required for administrative attendance correction.", "danger")
        return redirect(url_for('admin.attendance_dashboard'))

    record = db.session.get(AttendanceRecord, record_id)
    if not record:
        flash("Attendance record not found.", "danger")
        return redirect(url_for('admin.attendance_dashboard'))

    new_check_in = None
    if check_in_str:
        try:
            # Accepts HH:MM or ISO datetime
            if len(check_in_str) <= 5:
                parts = [int(p) for p in check_in_str.split(':')]
                new_check_in = datetime.combine(record.date, time(parts[0], parts[1]))
            else:
                new_check_in = datetime.fromisoformat(check_in_str)
        except Exception:
            pass

    new_check_out = None
    if check_out_str:
        try:
            if len(check_out_str) <= 5:
                parts = [int(p) for p in check_out_str.split(':')]
                new_check_out = datetime.combine(record.date, time(parts[0], parts[1]))
            else:
                new_check_out = datetime.fromisoformat(check_out_str)
        except Exception:
            pass

    try:
        updated = AttendanceService.admin_correct_attendance(
            record_id=record_id,
            admin_user_id=current_user.id,
            new_status=new_status,
            new_check_in=new_check_in,
            new_check_out=new_check_out,
            reason=reason
        )
        flash(f"Attendance record for {updated.student.full_name} ({updated.date}) successfully regularized to '{updated.display_status}'.", "success")
    except ValueError as ve:
        flash(str(ve), "danger")
    except Exception as e:
        flash(f"Error regularizing attendance: {str(e)}", "danger")

    return redirect(url_for('admin.attendance_dashboard', date=record.date.isoformat()))


@admin_bp.route('/attendance/manual-entry', methods=['POST'])
@login_required
def manual_attendance_entry():
    """Administrative creation of retroactive attendance records with audit logging."""
    student_id = request.form.get('student_id', type=int)
    date_str = request.form.get('date', '').strip()
    status = request.form.get('status', 'PRESENT').upper().strip()
    check_in_str = request.form.get('check_in_time', '').strip()
    check_out_str = request.form.get('check_out_time', '').strip()
    reason = request.form.get('reason', '').strip()

    if not student_id or not date_str or not reason:
        flash("Intern, date, and justification reason are mandatory for manual entry.", "danger")
        return redirect(url_for('admin.attendance_dashboard'))

    try:
        date_val = AttendanceService.parse_date(date_str)
        new_check_in = None
        if check_in_str:
            parts = [int(p) for p in check_in_str.split(':')]
            new_check_in = datetime.combine(date_val, time(parts[0], parts[1]))

        new_check_out = None
        if check_out_str:
            parts = [int(p) for p in check_out_str.split(':')]
            new_check_out = datetime.combine(date_val, time(parts[0], parts[1]))

        record = AttendanceService.admin_record_retroactive_attendance(
            admin_user_id=current_user.id,
            student_id=student_id,
            date_val=date_val,
            status=status,
            check_in_time=new_check_in,
            check_out_time=new_check_out,
            reason=reason
        )
        flash(f"Retroactive attendance record created for {record.student.full_name} on {date_val}.", "success")
        return redirect(url_for('admin.attendance_dashboard', date=date_val.isoformat()))
    except ValueError as ve:
        flash(str(ve), "danger")
    except Exception as e:
        flash(f"Manual entry failed: {str(e)}", "danger")

    return redirect(url_for('admin.attendance_dashboard'))


@admin_bp.route('/attendance/export')
@login_required
def export_attendance():
    """Generates organization-wide or filtered attendance reports in CSV or PDF format."""
    student_id = request.args.get('student_id', type=int)
    start_str = request.args.get('start_date', '').strip()
    end_str = request.args.get('end_date', '').strip()
    export_format = request.args.get('format', 'csv').lower()

    start_date = AttendanceService.parse_date(start_str) if start_str else None
    end_date = AttendanceService.parse_date(end_str) if end_str else None

    if export_format == 'pdf':
        try:
            pdf_bytes, filename, mime = AttendanceService.generate_attendance_pdf(
                student_id=student_id,
                start_date=start_date,
                end_date=end_date
            )
            return send_file(
                io.BytesIO(pdf_bytes),
                mimetype=mime,
                as_attachment=True,
                download_name=filename
            )
        except Exception as e:
            flash(f"PDF generation failed: {str(e)}", "danger")
            return redirect(url_for('admin.attendance_dashboard'))
    else:
        try:
            csv_content, filename, mime = AttendanceService.generate_attendance_csv(
                student_id=student_id,
                start_date=start_date,
                end_date=end_date
            )
            return Response(
                csv_content,
                mimetype=mime,
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )
        except Exception as e:
            flash(f"CSV export failed: {str(e)}", "danger")
            return redirect(url_for('admin.attendance_dashboard'))


@admin_bp.route('/leave', endpoint='leave_dashboard')
@login_required
def leave_dashboard():
    """Portal-wide leave management hub across all departments and cohorts."""
    status_filter = request.args.get('status', 'all').upper().strip()
    search_q = request.args.get('q', '').strip()

    q = LeaveRequest.query
    if status_filter in ['PENDING', 'APPROVED', 'REJECTED', 'CANCELLED']:
        q = q.filter(LeaveRequest.status == status_filter)

    leave_requests = q.options(
        joinedload(LeaveRequest.student).joinedload(Student.user),
        joinedload(LeaveRequest.reviewer)
    ).order_by(
        LeaveRequest.status == 'PENDING',
        LeaveRequest.created_at.desc()
    ).all()

    if search_q:
        q_lower = search_q.lower()
        leave_requests = [
            l for l in leave_requests
            if q_lower in (l.student.full_name.lower() if l.student else '') or
               q_lower in (l.student.student_uid.lower() if l.student else '')
        ]

    # Pre-calculate KPIs
    all_leaves = LeaveRequest.query.all()
    pending_count = sum(1 for l in all_leaves if l.status == 'PENDING')
    approved_count = sum(1 for l in all_leaves if l.status == 'APPROVED')
    rejected_count = sum(1 for l in all_leaves if l.status == 'REJECTED')
    total_leave_days = sum(l.days_count for l in all_leaves if l.status == 'APPROVED')

    return render_template(
        'admin/leave.html',
        leave_requests=leave_requests,
        status_filter=status_filter,
        search_q=search_q,
        pending_count=pending_count,
        approved_count=approved_count,
        rejected_count=rejected_count,
        total_leave_days=total_leave_days
    )


@admin_bp.route('/leave/<int:leave_id>/review', methods=['POST'])
@login_required
def review_leave(leave_id):
    """Administrative approval/rejection of leave application with auto-attendance sync."""
    action = request.form.get('action', '').upper().strip()
    remarks = request.form.get('remarks', '').strip()

    if action not in ['APPROVE', 'REJECT']:
        flash("Invalid review action specified.", "danger")
        return redirect(url_for('admin.leave_dashboard'))

    try:
        updated_leave = AttendanceService.review_leave(
            leave_id=leave_id,
            reviewer_id=current_user.id,
            action=action,
            remarks=remarks if remarks else None
        )
        flash(f"Leave request #{updated_leave.id} for {updated_leave.student.full_name} has been {updated_leave.status.lower()}.", "success")
    except ValueError as ve:
        flash(str(ve), "danger")
    except Exception as e:
        flash(f"Error reviewing leave: {str(e)}", "danger")

    return redirect(url_for('admin.leave_dashboard'))


@admin_bp.route('/schedule', endpoint='schedule_management')
@login_required
def schedule_management():
    """
    Attendance Policy, Holiday Registry, and Internship Schedule Management Hub:
    - View and update organizational attendance policy
    - Manage official company holidays (with auto-exclusion from working day calculations)
    - Monitor active internship schedules, elapsed working days, and completion trajectory
    """
    policy = AttendanceService.get_active_policy()
    holidays = CompanyHoliday.query.order_by(CompanyHoliday.date.asc()).all()

    # Active internships for schedule tracking
    active_internships = Internship.query.filter(
        Internship.status.in_(['ONBOARDING', 'ALLOCATED', 'ACTIVE'])
    ).options(
        joinedload(Internship.student).joinedload(Student.user),
        joinedload(Internship.mentor)
    ).all()

    internship_schedules = []
    for i in active_internships:
        try:
            summary = AttendanceService.get_internship_schedule_summary(i.id)
            internship_schedules.append({
                'internship': i,
                'summary': summary
            })
        except Exception as e:
            current_app.logger.warning(f"Error calculating schedule for internship {i.id}: {e}")

    return render_template(
        'admin/schedule.html',
        policy=policy,
        holidays=holidays,
        internship_schedules=internship_schedules,
        today_date=datetime.now().date()
    )


@admin_bp.route('/schedule/policy', methods=['POST'])
@login_required
def update_policy():
    """Updates organizational attendance policy thresholds."""
    start_t = request.form.get('work_start_time', '09:00').strip()
    end_t = request.form.get('work_end_time', '17:00').strip()
    late_t = request.form.get('late_threshold_time', '09:30').strip()
    half_h = request.form.get('half_day_minimum_hours', type=float) or 4.0
    full_h = request.form.get('full_day_minimum_hours', type=float) or 7.0
    working_days = request.form.get('working_days_csv', 'MON,TUE,WED,THU,FRI').strip()
    min_att = request.form.get('min_attendance_percentage', type=float) or 80.0

    try:
        policy = AttendanceService.update_policy(
            admin_user_id=current_user.id,
            work_start_time=start_t,
            work_end_time=end_t,
            late_threshold_time=late_t,
            half_day_minimum_hours=half_h,
            full_day_minimum_hours=full_h,
            working_days_csv=working_days,
            min_attendance_percentage=min_att
        )
        flash("Organizational Attendance Policy successfully updated.", "success")
    except ValueError as ve:
        flash(str(ve), "danger")
    except Exception as e:
        flash(f"Policy update failed: {str(e)}", "danger")

    return redirect(url_for('admin.schedule_management'))


@admin_bp.route('/schedule/holidays', methods=['POST'])
@login_required
def add_holiday():
    """Adds a recognized company holiday."""
    date_str = request.form.get('holiday_date', '').strip()
    name = request.form.get('name', '').strip()
    desc = request.form.get('description', '').strip()
    is_recurring = bool(request.form.get('is_recurring'))

    if not date_str or not name:
        flash("Holiday date and name are required.", "danger")
        return redirect(url_for('admin.schedule_management'))

    try:
        h_date = AttendanceService.parse_date(date_str)
        AttendanceService.add_holiday(
            holiday_date=h_date,
            name=name,
            description=desc if desc else None,
            is_optional=is_recurring
        )
        flash(f"Holiday '{name}' ({h_date}) added successfully.", "success")
    except ValueError as ve:
        flash(str(ve), "danger")
    except Exception as e:
        flash(f"Failed to add holiday: {str(e)}", "danger")

    return redirect(url_for('admin.schedule_management'))


@admin_bp.route('/schedule/holidays/<int:holiday_id>/delete', methods=['POST'])
@login_required
def delete_holiday(holiday_id):
    """Removes a company holiday."""
    try:
        AttendanceService.delete_holiday(holiday_id)
        flash("Holiday removed successfully.", "success")
    except ValueError as ve:
        flash(str(ve), "danger")
    except Exception as e:
        flash(f"Failed to delete holiday: {str(e)}", "danger")

    return redirect(url_for('admin.schedule_management'))


@admin_bp.route('/schedule/internships/<int:internship_id>/modify', methods=['POST'])
@login_required
def modify_internship_schedule(internship_id):
    """
    Authorizes modification of an internship schedule with mandatory audit trail:
    - Enforces business rule: automatic changes without authorized action are strictly prohibited.
    """
    new_end_date_str = request.form.get('new_end_date', '').strip()
    reason = request.form.get('reason', '').strip()

    if not new_end_date_str or not reason:
        flash("New completion date and an authorized modification reason are required.", "danger")
        return redirect(url_for('admin.schedule_management'))

    try:
        new_end_d = AttendanceService.parse_date(new_end_date_str)
        internship = AttendanceService.modify_internship_schedule(
            internship_id=internship_id,
            admin_user_id=current_user.id,
            new_end_date=new_end_d,
            reason=reason
        )
        flash(f"Internship schedule for {internship.student.full_name} updated to {new_end_d}.", "success")
    except ValueError as ve:
        flash(str(ve), "danger")
    except Exception as e:
        flash(f"Schedule adjustment failed: {str(e)}", "danger")

    return redirect(url_for('admin.schedule_management'))






