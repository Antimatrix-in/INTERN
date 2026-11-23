import io
import os
import re
import time
from datetime import datetime, date
from flask import Blueprint, render_template, redirect, url_for, flash, request, abort, jsonify, current_app, send_file, session, g, Response
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from sqlalchemy.orm import joinedload
from app.extensions import db
from app.models import (
    User, Student, College, Department, Application, Internship, InternshipPlan, ProjectAssignment, WeeklyMilestone,
    WeeklyTask, WeeklySubmission, Meeting, Notification, EvaluationRubric,
    NotificationPreference, Announcement, AnnouncementRead,
    Document, IssuedDocument, CertificateVerification,
    AttendanceRecord, LeaveRequest, AttendancePolicy, CompanyHoliday
)
from app.services.mentor_service import get_approved_mentors, APPROVED_MENTOR_EMAILS
from app.services.submission_workflow_service import SubmissionWorkflowService
from app.services.analytics_reporting_service import AnalyticsReportingService
from app.services.notification_service import NotificationService
from app.services.document_service import DocumentService
from app.services.attendance_service import AttendanceService

student_bp = Blueprint('student', __name__)

GITHUB_URL_PATTERN = re.compile(r'^https?:\/\/(www\.)?github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(\/.*)?$', re.IGNORECASE)


def allowed_video_file(filename):
    """Check if uploaded file has a permitted video extension."""
    if not filename or '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    allowed = current_app.config.get('ALLOWED_VIDEO_EXTENSIONS', {'mp4', 'mov', 'webm'})
    return ext in allowed


def is_valid_github_url(url):
    """Validate that the provided string is a valid GitHub repository URL."""
    if not url:
        return False
    return bool(GITHUB_URL_PATTERN.match(url.strip()))


def is_valid_google_drive_url(url):
    """Validate that the provided string is a valid Google Drive URL."""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        netloc = parsed.netloc.lower()
        if not (netloc == 'drive.google.com' or netloc.endswith('.drive.google.com') or
                netloc == 'docs.google.com' or netloc.endswith('.docs.google.com')):
            return False
        path = parsed.path
        query = parsed.query
        if re.search(r'\/file\/d\/[a-zA-Z0-9_-]+', path):
            return True
        if path.startswith('/open') or path.startswith('/uc'):
            if re.search(r'[?&]?id=[a-zA-Z0-9_-]+', query) or 'id=' in query:
                return True
        if re.search(r'\/drive\/folders\/[a-zA-Z0-9_-]+', path):
            return True
        if re.search(r'\/d\/[a-zA-Z0-9_-]+', path):
            return True
        return False
    except Exception:
        return False


ALLOWED_STUDENT_ROLES = {'student', 'employee', 'candidate', 'member', 'intern'}


def get_current_student():
    """Retrieve and validate student profile for current authenticated user."""
    if hasattr(g, 'current_student') and g.current_student:
        return g.current_student

    student = getattr(current_user, 'student_profile', None)
    if not student and current_user.is_authenticated:
        student = Student.query.options(
            joinedload(Student.college),
            joinedload(Student.department),
            joinedload(Student.user)
        ).filter_by(user_id=current_user.id).first()

    if not student and current_user.is_authenticated:
        # Auto-provision or link student profile for authenticated employee
        from app.auth.routes import _ensure_student_profile
        student = _ensure_student_profile(current_user)

    if not student:
        flash('No active employee/student profile linked to your account. Please contact Anti Matrix support.', 'danger')
        abort(403)

    g.current_student = student
    return student


@student_bp.before_request
@login_required
def check_student_role():
    # If authenticated in admin realm, redirect to admin dashboard to preserve strict isolation
    if session.get('auth_realm') == 'admin' and current_user.is_admin_or_staff:
        return redirect(url_for('admin.dashboard'))

    if session.get('auth_realm') == 'employee':
        return

    if current_user.role not in ALLOWED_STUDENT_ROLES:
        flash('Access restricted to enrolled internship employees.', 'danger')
        return redirect(url_for('auth.login'))

    # Establish active employee session realm
    session['auth_realm'] = 'employee'


# ─── Dashboard ────────────────────────────────────────────────────────────────

@student_bp.route('/dashboard')
@login_required
def dashboard():
    student = get_current_student()
    internship = student.active_internship

    # Ensure student has an active internship record
    if not internship:
        from app.services.career_integration import CareerIntegrationService
        app_record = student.applications.order_by(Application.id.desc()).first()
        duration_months = CareerIntegrationService.get_application_duration_months(app_record) if app_record else 1
        plan_code = '3_MONTH_PROFESSIONAL' if duration_months == 3 else '1_MONTH_PROJECT'
        plan = InternshipPlan.query.filter_by(plan_code=plan_code).first() or InternshipPlan.query.first()
        start_dt = datetime.utcnow().strftime('%d %b %Y')
        from dateutil.relativedelta import relativedelta
        from datetime import date
        end_dt = (date.today() + relativedelta(months=duration_months)).strftime('%d %b %Y')
        approved_mentors = get_approved_mentors()
        mentor = approved_mentors[0] if approved_mentors else None
        internship = Internship(
            internship_no=student.student_uid or current_user.employee_id or f"AM-INT-{student.id}",
            student_id=student.id,
            application_id=app_record.id if app_record else None,
            plan_id=plan.id if plan else 1,
            status='ACTIVE',
            start_date=start_dt,
            end_date=end_dt,
            progress_percent=0,
            current_stage='Week 1 Understanding & Setup',
            mentor_id=mentor.id if mentor else None
        )
        db.session.add(internship)
        db.session.commit()

    # Ensure student has an active project assignment (auto-allocate if none exists)
    assignment = internship.active_assignment if internship else None
    if internship and not assignment:
        from app.services.problem_allocation_service import ProblemAllocationService
        duration_months = internship.plan.duration_months if (internship.plan and internship.plan.duration_months) else 1
        app_record = student.applications.first()
        domain = app_record.applied_role if app_record and app_record.applied_role else None
        try:
            allocated_problem, err = ProblemAllocationService.allocate_random_problem(
                college_id=student.college_id,
                duration_months=duration_months,
                domain=domain,
                exclude_student_id=student.id
            )
            if allocated_problem:
                ProblemAllocationService.assign_problem_to_internship(
                    internship=internship,
                    project=allocated_problem
                )
                db.session.commit()
                assignment = internship.active_assignment
        except Exception:
            db.session.rollback()

    milestones = []
    current_milestone = None
    project = None

    if assignment:
        project = assignment.project
        milestones = assignment.weekly_milestones.all()
        current_milestone = assignment.current_milestone

        if milestones:
            m_ids = [m.id for m in milestones]
            all_tasks = WeeklyTask.query.filter(WeeklyTask.milestone_id.in_(m_ids)).all()
            totals = {}
            completed = {}
            for t in all_tasks:
                totals[t.milestone_id] = totals.get(t.milestone_id, 0) + 1
                if t.is_completed:
                    completed[t.milestone_id] = completed.get(t.milestone_id, 0) + 1
            for m in milestones:
                m._preloaded_total_tasks_count = totals.get(m.id, 0)
                m._preloaded_completed_tasks_count = completed.get(m.id, 0)

    upcoming_meeting = Meeting.query.filter_by(
        student_id=student.id,
        status='SCHEDULED'
    ).order_by(Meeting.id.asc()).first()

    notifications = current_user.notifications.limit(5).all()
    progress_percent = assignment.progress_percent if assignment else 0

    return render_template(
        'student/dashboard.html',
        student=student,
        internship=internship,
        assignment=assignment,
        project=project,
        milestones=milestones,
        current_milestone=current_milestone,
        upcoming_meeting=upcoming_meeting,
        notifications=notifications,
        progress_percent=progress_percent
    )


# ─── Project Detail ───────────────────────────────────────────────────────────

@student_bp.route('/project')
@student_bp.route('/project/<int:assignment_id>')
@login_required
def project_detail(assignment_id=None):
    student = get_current_student()
    internship = student.active_internship

    if not internship or not internship.active_assignment:
        flash('No active project has been assigned to your profile yet.', 'warning')
        return redirect(url_for('student.dashboard'))

    assignment = internship.active_assignment

    # IDOR check if explicit assignment_id is passed in URL
    if assignment_id is not None and assignment_id != assignment.id:
        requested = ProjectAssignment.query.get_or_404(assignment_id)
        if requested.internship.student_id != student.id:
            abort(403)
        assignment = requested

    milestones = assignment.weekly_milestones.all()
    if milestones:
        m_ids = [m.id for m in milestones]
        all_tasks = WeeklyTask.query.filter(WeeklyTask.milestone_id.in_(m_ids)).all()
        totals = {}
        completed = {}
        for t in all_tasks:
            totals[t.milestone_id] = totals.get(t.milestone_id, 0) + 1
            if t.is_completed:
                completed[t.milestone_id] = completed.get(t.milestone_id, 0) + 1
        for m in milestones:
            m._preloaded_total_tasks_count = totals.get(m.id, 0)
            m._preloaded_completed_tasks_count = completed.get(m.id, 0)

    return render_template(
        'student/project_detail.html',
        student=student,
        internship=internship,
        assignment=assignment,
        project=assignment.project,
        milestones=milestones
    )


# ─── Weekly Progress ──────────────────────────────────────────────────────────

@student_bp.route('/weekly-progress')
@login_required
def weekly_progress():
    student = get_current_student()
    internship = student.active_internship

    if not internship or not internship.active_assignment:
        flash('No active project roadmap found.', 'warning')
        return redirect(url_for('student.dashboard'))

    assignment = internship.active_assignment
    milestones = assignment.weekly_milestones.all() if assignment else []

    if milestones:
        m_ids = [m.id for m in milestones]
        all_tasks = WeeklyTask.query.filter(WeeklyTask.milestone_id.in_(m_ids)).all()
        totals = {}
        completed = {}
        for t in all_tasks:
            totals[t.milestone_id] = totals.get(t.milestone_id, 0) + 1
            if t.is_completed:
                completed[t.milestone_id] = completed.get(t.milestone_id, 0) + 1
        for m in milestones:
            m._preloaded_total_tasks_count = totals.get(m.id, 0)
            m._preloaded_completed_tasks_count = completed.get(m.id, 0)

    return render_template(
        'student/weekly_progress.html',
        student=student,
        internship=internship,
        assignment=assignment,
        milestones=milestones
    )


# ─── Week Detail & Tasks ──────────────────────────────────────────────────────

@student_bp.route('/week/<int:milestone_id>')
@login_required
def week_detail(milestone_id):
    student = get_current_student()
    milestone = WeeklyMilestone.query.get_or_404(milestone_id)

    # Strict IDOR ownership check
    if milestone.assignment.internship.student_id != student.id:
        abort(403)

    tasks = milestone.tasks.all()
    submissions = milestone.submissions.order_by(WeeklySubmission.id.desc()).all()
    latest_submission = milestone.latest_submission
    meeting = Meeting.query.filter_by(milestone_id=milestone.id).first()
    active_rubric = EvaluationRubric.get_active()

    return render_template(
        'student/week_detail.html',
        student=student,
        internship=milestone.assignment.internship,
        assignment=milestone.assignment,
        project=milestone.assignment.project,
        milestone=milestone,
        tasks=tasks,
        submissions=submissions,
        latest_submission=latest_submission,
        meeting=meeting,
        rubric=active_rubric
    )


@student_bp.route('/week/<int:milestone_id>/task/<int:task_id>')
@login_required
def task_detail(milestone_id, task_id):
    student = get_current_student()
    milestone = WeeklyMilestone.query.get_or_404(milestone_id)

    # Strict IDOR check
    if milestone.assignment.internship.student_id != student.id:
        abort(403)

    task = WeeklyTask.query.filter_by(id=task_id, milestone_id=milestone.id).first_or_404()
    return render_template(
        'student/task_detail.html',
        student=student,
        internship=milestone.assignment.internship,
        assignment=milestone.assignment,
        project=milestone.assignment.project,
        milestone=milestone,
        task=task
    )


@student_bp.route('/week/<int:milestone_id>/task/<int:task_id>/toggle', methods=['POST'])
@login_required
def toggle_task(milestone_id, task_id):
    student = get_current_student()
    milestone = WeeklyMilestone.query.get_or_404(milestone_id)
    if hasattr(milestone, '_preloaded_completed_tasks_count'):
        delattr(milestone, '_preloaded_completed_tasks_count')
    if hasattr(milestone, '_preloaded_total_tasks_count'):
        delattr(milestone, '_preloaded_total_tasks_count')

    if milestone.assignment.internship.student_id != student.id:
        abort(403)

    task = WeeklyTask.query.filter_by(id=task_id, milestone_id=milestone.id).first_or_404()
    task.is_completed = not task.is_completed
    task.status = 'COMPLETED' if task.is_completed else 'IN_PROGRESS'

    # If milestone is AVAILABLE, transition to IN_PROGRESS upon work start
    if milestone.status in ['AVAILABLE', 'ASSIGNED'] and task.is_completed:
        milestone.status = 'IN_PROGRESS'

    db.session.commit()

    if request.is_json or request.headers.get('Accept') == 'application/json' or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'success': True,
            'task_id': task.id,
            'is_completed': task.is_completed,
            'all_completed': milestone.all_tasks_completed,
            'completed_count': milestone.completed_tasks_count,
            'total_count': milestone.total_tasks_count
        })
    return redirect(url_for('student.week_detail', milestone_id=milestone.id))


# ─── Milestone Submission ────────────────────────────────────────────────────

@student_bp.route('/submissions/submit/<int:milestone_id>', methods=['POST'])
@login_required
def submit_milestone(milestone_id):
    student = get_current_student()
    milestone = WeeklyMilestone.query.get_or_404(milestone_id)
    if hasattr(milestone, '_preloaded_completed_tasks_count'):
        delattr(milestone, '_preloaded_completed_tasks_count')
    if hasattr(milestone, '_preloaded_total_tasks_count'):
        delattr(milestone, '_preloaded_total_tasks_count')

    # Strict IDOR check
    if milestone.assignment.internship.student_id != student.id:
        abort(403)

    # Verify milestone is active / unlocked
    if milestone.status in ['LOCKED', 'COMPLETED', 'APPROVED']:
        flash('This week is not in a submittable state.', 'danger')
        return redirect(url_for('student.week_detail', milestone_id=milestone.id))

    # Duplicate submission protection
    if milestone.status in ['UNDER_REVIEW', 'SUBMITTED']:
        flash('A submission is already currently under review for this week. Please await evaluation.', 'warning')
        return redirect(url_for('student.week_detail', milestone_id=milestone.id))

    # Validate state transition using workflow engine
    valid_trans, trans_err = SubmissionWorkflowService.validate_milestone_transition(milestone.status, 'UNDER_REVIEW')
    if not valid_trans:
        flash(trans_err or 'Invalid submission status transition.', 'danger')
        return redirect(url_for('student.week_detail', milestone_id=milestone.id))

    # 1. Require all tasks to be completed
    if not milestone.all_tasks_completed:
        flash('Complete all required tasks before submitting your work.', 'danger')
        return redirect(url_for('student.week_detail', milestone_id=milestone.id))

    # 2. Validate GitHub URL
    github_url = request.form.get('github_url', '').strip() or request.form.get('repo_url', '').strip()
    if not github_url or not is_valid_github_url(github_url):
        flash('Please provide a valid GitHub repository URL (e.g. https://github.com/username/project).', 'danger')
        return redirect(url_for('student.week_detail', milestone_id=milestone.id))

    # 3. Validate Demo Video Google Drive URL
    demo_video_url = request.form.get('demo_video_url', '').strip() or request.form.get('demo_video', '').strip()
    if not demo_video_url:
        flash('Demo video Google Drive link is required.', 'danger')
        return redirect(url_for('student.week_detail', milestone_id=milestone.id))

    if not is_valid_google_drive_url(demo_video_url):
        flash('Please provide a valid Google Drive demo video link.', 'danger')
        return redirect(url_for('student.week_detail', milestone_id=milestone.id))

    submission_notes = request.form.get('submission_notes', '').strip()
    implementation_summary = request.form.get('implementation_summary', '').strip()

    try:
        # Create submission record (preserving full history and reusing existing database fields)
        sub = WeeklySubmission(
            milestone_id=milestone.id,
            student_id=student.id,
            github_url=github_url,
            repo_url=github_url,
            demo_video_url=demo_video_url,
            demo_video_path=demo_video_url,
            file_path=demo_video_url,
            submission_notes=submission_notes,
            implementation_summary=implementation_summary,
            status='UNDER_REVIEW',
            submitted_at=datetime.utcnow()
        )
        db.session.add(sub)
        db.session.flush()

        # Update milestone status
        milestone.status = 'UNDER_REVIEW'
        milestone.assignment.internship.update_progress()

        # Notification for student
        notif = Notification(
            user_id=current_user.id,
            title=f"Week {milestone.week_number} Submitted for Verification",
            message=f"Your deliverables for Week {milestone.week_number} ({milestone.title}) were submitted successfully and are under evaluation.",
            type='SUCCESS',
            link=url_for('student.week_detail', milestone_id=milestone.id)
        )
        db.session.add(notif)

        # Notification for assigned mentor if one exists
        mentor_id = milestone.assignment.internship.mentor_id
        if mentor_id:
            SubmissionWorkflowService.notify_submission_received(
                mentor_user_id=mentor_id,
                student_name=student.user.full_name,
                milestone=milestone,
                submission_id=sub.id
            )

        db.session.commit()

        flash(f'Week {milestone.week_number} submitted for verification successfully! Your submission is now UNDER REVIEW.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'An error occurred while processing your submission: {str(e)}', 'danger')

    return redirect(url_for('student.week_detail', milestone_id=milestone.id))


# ─── Protected Video Stream ───────────────────────────────────────────────────

@student_bp.route('/employee/submission/<int:sub_id>/video')
@login_required
def stream_submission_video(sub_id):
    """Securely stream demo video for the owning employee only."""
    student = get_current_student()
    sub = WeeklySubmission.query.get_or_404(sub_id)

    # Strict ownership check (IDOR protection)
    if sub.student_id != student.id:
        abort(403)

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

    # Determine mimetype
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    mimetype = 'video/mp4'
    if ext == 'webm':
        mimetype = 'video/webm'
    elif ext == 'mov':
        mimetype = 'video/quicktime'

    return send_file(full_path, mimetype=mimetype, conditional=True)


# ─── Other Student Views ──────────────────────────────────────────────────────

@student_bp.route('/submissions')
@login_required
def submissions():
    student = get_current_student()
    submissions_list = WeeklySubmission.query.filter_by(
        student_id=student.id
    ).order_by(WeeklySubmission.id.desc()).all()

    return render_template(
        'student/submissions.html',
        student=student,
        submissions=submissions_list
    )


@student_bp.route('/meetings')
@login_required
def meetings():
    student = get_current_student()
    meetings_list = Meeting.query.filter_by(
        student_id=student.id
    ).order_by(Meeting.id.desc()).all()

    return render_template(
        'student/meetings.html',
        student=student,
        meetings=meetings_list
    )


@student_bp.route('/notifications')
@login_required
def notifications():
    student = get_current_student()
    category = request.args.get('category', 'ALL')
    unread_only = request.args.get('unread', '0') in ['1', 'true', 'yes']

    notifications_list = NotificationService.get_user_notifications(
        user_id=current_user.id,
        category=category,
        unread_only=unread_only,
        limit=100
    )
    announcements = NotificationService.get_user_announcements(current_user.id, limit=5)
    preferences = NotificationService.get_or_create_preferences(current_user.id)

    return render_template(
        'student/notifications.html',
        student=student,
        notifications=notifications_list,
        announcements=announcements,
        preferences=preferences,
        active_category=category.upper(),
        unread_only=unread_only,
        unread_count=current_user.unread_notifications_count
    )


@student_bp.route('/notifications/<int:notification_id>/read', methods=['POST'])
@login_required
def mark_single_notification_read(notification_id):
    success = NotificationService.mark_as_read(notification_id, current_user.id)
    if not success:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': 'Unauthorized or notification not found.'}), 403
        abort(403)

    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'unread_count': current_user.unread_notifications_count})

    return redirect(request.referrer or url_for('student.notifications'))


@student_bp.route('/notifications/mark-all-read', methods=['POST'])
@student_bp.route('/notifications/read-all', methods=['POST'])
@login_required
def mark_all_read():
    category = request.form.get('category') or request.args.get('category')
    NotificationService.mark_all_as_read(current_user.id, category=category)
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'unread_count': 0})
    flash('All notifications marked as read.', 'info')
    return redirect(url_for('student.notifications'))


@student_bp.route('/notifications/preferences', methods=['GET', 'POST'])
@login_required
def notification_preferences():
    student = get_current_student()
    prefs = NotificationService.get_or_create_preferences(current_user.id)

    if request.method == 'POST':
        email_enabled = request.form.get('email_enabled') == 'on'
        in_portal_enabled = request.form.get('in_portal_enabled') == 'on'
        deadline_reminders = request.form.get('deadline_reminders') == 'on'
        announcements = request.form.get('announcements') == 'on'
        evaluations = request.form.get('evaluations') == 'on'

        NotificationService.update_preferences(
            user_id=current_user.id,
            email_enabled=email_enabled,
            in_portal_enabled=in_portal_enabled,
            deadline_reminders=deadline_reminders,
            announcements=announcements,
            evaluations=evaluations
        )
        flash('Notification preferences saved successfully.', 'success')
        return redirect(url_for('student.notification_preferences'))

    return render_template(
        'student/notification_preferences.html',
        student=student,
        preferences=prefs
    )


@student_bp.route('/notifications/api/unread-count')
@login_required
def api_unread_notifications_count():
    return jsonify({
        'success': True,
        'unread_count': current_user.unread_notifications_count
    })


@student_bp.route('/notifications/announcements/<int:announcement_id>/acknowledge', methods=['POST'])
@login_required
def acknowledge_announcement(announcement_id):
    NotificationService.mark_announcement_read(announcement_id, current_user.id)
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True})
    flash('Announcement acknowledged.', 'info')
    return redirect(request.referrer or url_for('student.notifications'))


@student_bp.route('/documents')
@login_required
def documents():
    student = get_current_student()
    internship = student.active_internship
    
    issued_documents = IssuedDocument.query.filter_by(student_id=student.id).order_by(IssuedDocument.id.desc()).all()
    uploaded_documents = Document.query.filter_by(student_id=student.id).order_by(Document.id.desc()).all()
    
    eligibility = DocumentService.validate_certificate_eligibility(internship) if internship else {
        'is_eligible': False,
        'reason': 'No active internship found.',
        'total_milestones': 0,
        'approved_milestones': 0,
        'completion_percentage': 0.0,
        'internship_status': 'NONE'
    }

    # Certificate verification record if already issued
    certificate_ver = CertificateVerification.query.filter_by(internship_id=internship.id).first() if internship else None

    return render_template(
        'student/documents.html',
        student=student,
        internship=internship,
        issued_documents=issued_documents,
        uploaded_documents=uploaded_documents,
        eligibility=eligibility,
        certificate_ver=certificate_ver
    )


@student_bp.route('/documents/upload', methods=['POST'])
@login_required
def upload_document():
    student = get_current_student()
    doc_type = request.form.get('doc_type', 'RESUME').strip().upper()
    file_obj = request.files.get('file') or request.files.get('document')

    if not file_obj or not file_obj.filename:
        flash('Please select a valid document file to upload.', 'danger')
        return redirect(url_for('student.documents'))

    try:
        doc = DocumentService.upload_student_document(
            student_id=student.id,
            doc_type=doc_type,
            file_obj=file_obj,
            uploaded_by_user_id=current_user.id
        )
        flash(f'{doc.doc_type} uploaded successfully. Awaiting administrative verification.', 'success')
    except Exception as e:
        flash(f'Upload failed: {str(e)}', 'danger')

    return redirect(url_for('student.documents'))


@student_bp.route('/documents/download/<doc_no>')
@student_bp.route('/documents/<int:doc_id>/download')
@login_required
def download_document(doc_no=None, doc_id=None):
    identifier = doc_no if doc_no else doc_id
    try:
        file_path, download_name, mime = DocumentService.get_document_for_download(
            doc_identifier=identifier,
            current_user_obj=current_user,
            is_issued=True
        )
        return send_file(file_path, mimetype=mime, as_attachment=True, download_name=download_name)
    except PermissionError as pe:
        abort(403)
    except FileNotFoundError:
        abort(404)
    except Exception as e:
        flash(f'Download error: {str(e)}', 'danger')
        return redirect(url_for('student.documents'))


@student_bp.route('/documents/preview/<doc_no>')
@student_bp.route('/documents/<int:doc_id>/preview')
@login_required
def preview_document(doc_no=None, doc_id=None):
    identifier = doc_no if doc_no else doc_id
    try:
        file_path, download_name, mime = DocumentService.get_document_for_download(
            doc_identifier=identifier,
            current_user_obj=current_user,
            is_issued=True
        )
        return send_file(file_path, mimetype=mime, as_attachment=False)
    except PermissionError:
        abort(403)
    except FileNotFoundError:
        abort(404)
    except Exception as e:
        flash(f'Preview error: {str(e)}', 'danger')
        return redirect(url_for('student.documents'))


@student_bp.route('/documents/uploaded/<int:doc_id>/download')
@login_required
def download_uploaded_document(doc_id):
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
        flash(f'Download error: {str(e)}', 'danger')
        return redirect(url_for('student.documents'))


@student_bp.route('/documents/uploaded/<int:doc_id>/preview')
@login_required
def preview_uploaded_document(doc_id):
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
        flash(f'Preview error: {str(e)}', 'danger')
        return redirect(url_for('student.documents'))


ALLOWED_PROFILE_YEARS = {'1st Year', '2nd Year', '3rd Year', '4th Year', 'Final Year', '4th Year / Final Year'}


@student_bp.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    student = get_current_student()
    internship = student.active_internship
    assignment = internship.active_assignment if internship else None
    colleges = College.query.filter_by(is_active=True).options(joinedload(College.departments)).order_by(College.name).all()

    if request.method == 'POST':
        from sqlalchemy import func

        # 1. College (accepts typed name or selected ID)
        college = None
        college_id_raw = str(request.form.get('college_id', '')).strip()
        college_name = (request.form.get('college') or request.form.get('college_name') or '').strip()

        try:
            college_id = int(college_id_raw) if college_id_raw else 0
        except (ValueError, TypeError):
            college_id = 0

        if college_id > 0:
            college = db.session.get(College, college_id)

        if not college and college_name:
            college = College.query.filter(func.lower(College.name) == college_name.lower()).first()
            if not college:
                college = College.query.filter(College.name.ilike(f"%{college_name}%")).first()
            if not college:
                college = College.query.filter_by(code='OTHER-COLLEGE').first() or \
                          College.query.filter(College.name.ilike('%Other%')).first()

        if not college or not college.is_active:
            flash('Please select a valid College / Institution from the list.', 'danger')
            return render_template(
                'student/profile.html',
                student=student,
                internship=internship,
                assignment=assignment,
                colleges=colleges
            )

        # 2. Department (accepts typed name or selected ID)
        dept = None
        dept_id_raw = str(request.form.get('department_id', '')).strip()
        dept_name = (request.form.get('department') or request.form.get('department_name') or '').strip()

        try:
            department_id = int(dept_id_raw) if dept_id_raw else 0
        except (ValueError, TypeError):
            department_id = 0

        if department_id > 0:
            candidate_dept = db.session.get(Department, department_id)
            if candidate_dept and candidate_dept.college_id == college.id and candidate_dept.is_active:
                dept = candidate_dept

        if not dept and dept_name:
            dept = Department.query.filter_by(college_id=college.id).filter(
                func.lower(Department.name) == dept_name.lower()
            ).first()
            if not dept:
                dept = Department.query.filter(
                    Department.college_id == college.id,
                    Department.name.ilike(f"%{dept_name}%")
                ).first()
            if not dept:
                dept = Department.query.filter_by(college_id=college.id, name='Other').first() or \
                       Department.query.filter_by(college_id=college.id).first()

        if not dept or not dept.is_active or dept.college_id != college.id:
            flash('Please select a valid Academic Department for the selected college.', 'danger')
            return render_template(
                'student/profile.html',
                student=student,
                internship=internship,
                assignment=assignment,
                colleges=colleges
            )

        # 3. Current Year (accepts selection or typed value with normalization)
        current_year = request.form.get('current_year', '').strip()
        year_mapping = {
            '1': '1st Year', '1st': '1st Year', '1st year': '1st Year',
            '2': '2nd Year', '2nd': '2nd Year', '2nd year': '2nd Year',
            '3': '3rd Year', '3rd': '3rd Year', '3rd year': '3rd Year',
            '4': '4th Year', '4th': '4th Year', '4th year': '4th Year',
            'final': 'Final Year', 'final year': 'Final Year'
        }
        if current_year.lower() in year_mapping:
            current_year = year_mapping[current_year.lower()]

        if current_year not in ALLOWED_PROFILE_YEARS:
            flash('Please select a valid Current Year from the options.', 'danger')
            return render_template(
                'student/profile.html',
                student=student,
                internship=internship,
                assignment=assignment,
                colleges=colleges
            )

        # 4. Mobile Number
        raw_phone = request.form.get('mobile_number', '').strip()
        clean_phone = re.sub(r'[\s\-\(\)]', '', raw_phone)
        if not re.match(r'^\+?[0-9]{10,15}$', clean_phone):
            flash('Please enter a valid mobile number (10–15 digits, optional + prefix).', 'danger')
            return render_template(
                'student/profile.html',
                student=student,
                internship=internship,
                assignment=assignment,
                colleges=colleges
            )

        # Update ONLY the candidate's editable profile fields
        student.college_id = college.id
        student.department_id = dept.id
        student.current_year = current_year
        if student.user:
            student.user.phone = raw_phone
        current_user.phone = raw_phone
        student.profile_completed = True

        db.session.commit()
        flash('Profile updated successfully.', 'success')
        return redirect(url_for('student.profile'))

    return render_template(
        'student/profile.html',
        student=student,
        internship=internship,
        assignment=assignment,
        colleges=colleges
    )


@student_bp.route('/profile/skip-onboarding', methods=['POST'])
@login_required
def skip_profile_onboarding():
    """Dismisses the first-time profile prompt without modifying any profile records."""
    student = get_current_student()
    student.profile_completed = True
    db.session.commit()
    if request.is_json:
        return jsonify({'success': True})
    return redirect(url_for('student.dashboard'))


# ─── 9. Intern Performance Analytics & Self-Telemetry (Phase 2 Module 6) ─────

@student_bp.route('/analytics')
@login_required
def analytics():
    """
    Candidate self-service performance scorecard and historical progress metrics.
    Strictly isolated to current authenticated student (IDOR protected).
    """
    student = get_current_student()
    if not student:
        flash('Candidate profile not found.', 'danger')
        return redirect(url_for('student.dashboard'))

    scorecard = AnalyticsReportingService.get_intern_detail_analytics(student.id)
    if not scorecard:
        flash('Analytics telemetry currently unavailable.', 'info')
        return redirect(url_for('student.dashboard'))

    internship = student.active_internship or student.internships.first()
    assignment = internship.assignments.first() if internship else None

    return render_template(
        'student/analytics.html',
        assignment=assignment,
        **scorecard
    )


# ─── 10. Attendance, Leave & Scheduling (Phase 2 Module 9) ───────────────────

@student_bp.route('/attendance', endpoint='attendance')
@student_bp.route('/attendance/dashboard', endpoint='attendance_dashboard')
@login_required
def attendance():
    """
    Candidate self-service attendance dashboard:
    - Real-time check-in and check-out widget
    - Daily status badge & today's log
    - Monthly interactive attendance matrix/calendar
    - Granular KPI metrics (attendance rate, present, late, half-day, leave days)
    - Schedule trajectory & completion pace
    """
    student = get_current_student()
    internship = student.active_internship or student.internships.first()
    today_dt = datetime.now()
    today_date = today_dt.date()

    # Get today's attendance record
    today_record = AttendanceService.get_today_attendance(student.id, today_date)

    # Calendar query month/year
    try:
        year = int(request.args.get('year', today_date.year))
        month = int(request.args.get('month', today_date.month))
    except (ValueError, TypeError):
        year = today_date.year
        month = today_date.month

    # Clamp month/year
    if month < 1 or month > 12:
        month = today_date.month
    if year < 2020 or year > 2035:
        year = today_date.year

    monthly_matrix = AttendanceService.get_monthly_matrix(student.id, year, month)
    summary = AttendanceService.get_student_attendance_summary(student.id, month=month, year=year)
    overall_summary = AttendanceService.get_student_attendance_summary(student.id)
    policy = AttendanceService.get_active_policy()

    schedule_summary = None
    if internship:
        try:
            schedule_summary = AttendanceService.get_internship_schedule_summary(internship.id)
        except Exception as e:
            current_app.logger.warning(f"Error computing internship schedule summary: {e}")

    # Month navigation helpers
    prev_month = 12 if month == 1 else month - 1
    prev_year = year - 1 if month == 1 else year
    next_month = 1 if month == 12 else month + 1
    next_year = year + 1 if month == 12 else year
    month_name = date(year, month, 1).strftime('%B %Y')

    return render_template(
        'student/attendance.html',
        student=student,
        internship=internship,
        today_date=today_date,
        today_record=today_record,
        monthly_matrix=monthly_matrix,
        summary=summary,
        overall_summary=overall_summary,
        policy=policy,
        schedule_summary=schedule_summary,
        selected_year=year,
        selected_month=month,
        month_name=month_name,
        prev_month=prev_month,
        prev_year=prev_year,
        next_month=next_month,
        next_year=next_year
    )


@student_bp.route('/attendance/check-in', methods=['POST'])
@login_required
def attendance_check_in():
    """Records real-time intern arrival timestamp and validates late status."""
    student = get_current_student()
    internship = student.active_internship or student.internships.first()
    internship_id = internship.id if internship else None
    notes = request.form.get('notes', '').strip()
    ip_addr = request.headers.get('X-Forwarded-For', request.remote_addr)

    try:
        record = AttendanceService.record_check_in(
            student_id=student.id,
            internship_id=internship_id,
            ip_address=ip_addr,
            notes=notes if notes else None
        )
        flash(f"Check-in registered successfully at {record.check_in_time.strftime('%H:%M UTC')}. Status: {record.display_status}.", 'success')
    except ValueError as ve:
        flash(str(ve), 'warning')
    except Exception as e:
        flash(f"Failed to record check-in: {str(e)}", 'danger')

    return redirect(url_for('student.attendance_dashboard'))


@student_bp.route('/attendance/check-out', methods=['POST'])
@login_required
def attendance_check_out():
    """Records intern departure timestamp, evaluates working hours and determines half-day status."""
    student = get_current_student()
    notes = request.form.get('notes', '').strip()
    ip_addr = request.headers.get('X-Forwarded-For', request.remote_addr)

    try:
        record = AttendanceService.record_check_out(
            student_id=student.id,
            ip_address=ip_addr,
            notes=notes if notes else None
        )
        flash(f"Check-out registered successfully at {record.check_out_time.strftime('%H:%M UTC')}. Total duration: {record.work_duration_formatted} ({record.display_status}).", 'success')
    except ValueError as ve:
        flash(str(ve), 'warning')
    except Exception as e:
        flash(f"Failed to record check-out: {str(e)}", 'danger')

    return redirect(url_for('student.attendance_dashboard'))


@student_bp.route('/leave', endpoint='leave')
@student_bp.route('/leave/dashboard', endpoint='leave_dashboard')
@login_required
def leave():
    """Leave management portal for candidate: history, status tracking, and leave application."""
    student = get_current_student()
    internship = student.active_internship or student.internships.first()
    
    leave_requests = LeaveRequest.query.filter_by(student_id=student.id).order_by(LeaveRequest.created_at.desc()).all()
    pending_count = sum(1 for l in leave_requests if l.status == 'PENDING')
    approved_count = sum(1 for l in leave_requests if l.status == 'APPROVED')
    rejected_count = sum(1 for l in leave_requests if l.status == 'REJECTED')
    total_approved_days = sum(l.days_count for l in leave_requests if l.status == 'APPROVED')

    return render_template(
        'student/leave.html',
        student=student,
        internship=internship,
        leave_requests=leave_requests,
        pending_count=pending_count,
        approved_count=approved_count,
        rejected_count=rejected_count,
        total_approved_days=total_approved_days,
        today_date=datetime.now().date()
    )


@student_bp.route('/leave/apply', methods=['POST'])
@login_required
def apply_leave():
    """Submits a leave request with overlap validation and optional documentation."""
    student = get_current_student()
    internship = student.active_internship or student.internships.first()
    if not internship:
        flash("You do not have an active internship assignment to request leave against.", "danger")
        return redirect(url_for('student.leave_dashboard'))

    leave_type = request.form.get('leave_type', '').strip()
    start_date_str = request.form.get('start_date', '').strip()
    end_date_str = request.form.get('end_date', '').strip()
    reason = request.form.get('reason', '').strip()
    file_obj = request.files.get('document')

    if not leave_type or not start_date_str or not end_date_str or not reason:
        flash("All fields (Leave Type, Start Date, End Date, Reason) are required.", "danger")
        return redirect(url_for('student.leave_dashboard'))

    try:
        start_d = AttendanceService.parse_date(start_date_str)
        end_d = AttendanceService.parse_date(end_date_str)

        leave_req = AttendanceService.apply_leave(
            student_id=student.id,
            internship_id=internship.id,
            leave_type=leave_type,
            start_date=start_d,
            end_date=end_d,
            reason=reason,
            file_obj=file_obj
        )
        flash(f"Leave application for {leave_req.days_count} working day(s) ({leave_req.display_type}) submitted successfully. Awaiting review.", "success")
    except ValueError as ve:
        flash(str(ve), "danger")
    except Exception as e:
        flash(f"Error submitting leave request: {str(e)}", "danger")

    return redirect(url_for('student.leave_dashboard'))


@student_bp.route('/leave/<int:leave_id>/cancel', methods=['POST'])
@login_required
def cancel_leave(leave_id):
    """Allows an intern to cancel their own pending leave application."""
    student = get_current_student()
    reason = request.form.get('reason', 'Cancelled by student.').strip()

    try:
        AttendanceService.cancel_leave(leave_id=leave_id, student_id=student.id, reason=reason)
        flash("Leave request has been successfully cancelled.", "success")
    except (ValueError, PermissionError) as e:
        flash(str(e), "danger")
    except Exception as e:
        flash(f"Failed to cancel leave: {str(e)}", "danger")

    return redirect(url_for('student.leave_dashboard'))


@student_bp.route('/attendance/export')
@login_required
def export_attendance():
    """Generates official attendance reports in CSV or PDF format for the authenticated student."""
    student = get_current_student()
    export_format = request.args.get('format', 'csv').lower()

    if export_format == 'pdf':
        try:
            pdf_bytes, filename, mime = AttendanceService.generate_attendance_pdf(student_id=student.id)
            return send_file(
                io.BytesIO(pdf_bytes),
                mimetype=mime,
                as_attachment=True,
                download_name=filename
            )
        except Exception as e:
            flash(f"PDF generation failed: {str(e)}", "danger")
            return redirect(url_for('student.attendance_dashboard'))
    else:
        try:
            csv_content, filename, mime = AttendanceService.generate_attendance_csv(student_id=student.id)
            return Response(
                csv_content,
                mimetype=mime,
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )
        except Exception as e:
            flash(f"CSV export failed: {str(e)}", "danger")
            return redirect(url_for('student.attendance_dashboard'))



