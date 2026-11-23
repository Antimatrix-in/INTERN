import io
from datetime import datetime, date, timedelta
from functools import wraps
import json
from flask import (
    Blueprint, render_template, redirect, url_for, flash,
    request, abort, jsonify, current_app, Response, send_file
)
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload
from app.extensions import db
from app.models import (
    User, Student, Internship, Project, ProjectAssignment,
    WeeklyMilestone, WeeklyTask, WeeklySubmission, Evaluation,
    Meeting, Notification, AuditLog, College, Department, EvaluationRubric,
    NotificationPreference, Announcement, AnnouncementRead,
    AttendanceRecord, LeaveRequest, AttendancePolicy, CompanyHoliday
)
from app.services.submission_workflow_service import SubmissionWorkflowService
from app.services.analytics_reporting_service import AnalyticsReportingService
from app.services.notification_service import NotificationService
from app.services.attendance_service import AttendanceService

mentor_bp = Blueprint('mentor', __name__, url_prefix='/mentor')


# ─── Mentor Authorization Decorator & Helpers ─────────────────────────────────

def mentor_required(f):
    """
    Enforces server-side mentor authorization.
    Permits mentors, admins, and super_admins. Active status strictly enforced.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Please sign in to access the Mentor Workspace.', 'warning')
            return redirect(url_for('auth.admin_login', next=request.url))
        if current_user.role not in ['mentor', 'admin', 'super_admin']:
            flash('Access restricted. Technical Mentor credentials required.', 'danger')
            return abort(403)
        if not current_user.is_active:
            flash('Your mentor account has been deactivated. Please contact the administrator.', 'danger')
            return abort(403)
        return f(*args, **kwargs)
    return decorated_function


def check_mentor_intern_authorization(student_id):
    """
    Checks that the requesting user has authority over the student.
    Mentors are strictly scoped to their assigned interns; Admins retain global access.
    Returns: (authorized: bool, student: Student or None, internship: Internship or None)
    """
    student = Student.query.get(student_id)
    if not student:
        return False, None, None
    internship = student.active_internship
    if current_user.role in ['admin', 'super_admin']:
        return True, student, internship
    if current_user.role == 'mentor':
        if internship and internship.mentor_id == current_user.id:
            return True, student, internship
        return False, student, internship
    return False, None, None


def check_mentor_submission_authorization(submission_id):
    """
    Validates that a submission belongs to an intern under the current mentor's supervision.
    Returns: (authorized: bool, submission: WeeklySubmission or None)
    """
    submission = WeeklySubmission.query.get(submission_id)
    if not submission:
        return False, None
    if current_user.role in ['admin', 'super_admin']:
        return True, submission
    if current_user.role == 'mentor':
        milestone = submission.milestone
        if (milestone and milestone.assignment and
            milestone.assignment.internship and
            milestone.assignment.internship.mentor_id == current_user.id):
            return True, submission
        return False, submission
    return False, None


# ─── 1. Mentor Dashboard Overview ─────────────────────────────────────────────

@mentor_bp.route('/')
@mentor_bp.route('/dashboard')
@login_required
@mentor_required
def dashboard():
    mentor = current_user

    # 1. Assigned Internships
    assigned_internships = Internship.query.filter_by(
        mentor_id=mentor.id
    ).options(
        joinedload(Internship.student).joinedload(Student.user),
        joinedload(Internship.student).joinedload(Student.college),
        joinedload(Internship.student).joinedload(Student.department)
    ).order_by(Internship.id.desc()).all()

    active_internships = [i for i in assigned_internships if i.status == 'ACTIVE']
    completed_internships = [i for i in assigned_internships if i.status == 'COMPLETED']
    active_intern_count = len(active_internships)
    total_assigned_count = len(assigned_internships)

    # 2. Student IDs supervised
    student_ids = [i.student_id for i in assigned_internships]
    active_student_ids = [i.student_id for i in active_internships]

    # 3. Pending Submissions Review Queue
    pending_submissions = []
    if active_student_ids:
        pending_submissions = WeeklySubmission.query.filter(
            WeeklySubmission.student_id.in_(active_student_ids),
            WeeklySubmission.status.in_(['SUBMITTED', 'UNDER_REVIEW'])
        ).order_by(WeeklySubmission.submitted_at.desc()).all()
    pending_count = len(pending_submissions)

    # 4. Supervised Active Projects
    project_ids = set()
    for i in active_internships:
        assign = i.active_assignment
        if assign and assign.project_id:
            project_ids.add(assign.project_id)
    active_projects_count = len(project_ids)

    # 5. Upcoming Deadlines (Due within next 7 days or overdue among active mentees)
    upcoming_deadlines = []
    now = datetime.utcnow()
    seven_days_later = now + timedelta(days=7)
    for i in active_internships:
        assign = i.active_assignment
        if assign:
            for m in assign.weekly_milestones.all():
                if m.status in ['AVAILABLE', 'IN_PROGRESS', 'REVISION_REQUIRED', 'REDO_REQUIRED']:
                    is_urgent = (m.due_at and m.due_at <= seven_days_later) or m.is_overdue
                    upcoming_deadlines.append({
                        'internship': i,
                        'student': i.student,
                        'project': assign.project,
                        'milestone': m,
                        'due_at': m.due_at,
                        'is_overdue': m.is_overdue,
                        'is_urgent': is_urgent
                    })
    # Sort deadlines by urgency (overdue first, then soonest due date)
    upcoming_deadlines.sort(key=lambda d: (0 if d['is_overdue'] else 1, d['due_at'] or datetime.max))

    # 6. Performance Telemetry & Completion Rate
    completion_rate = int((len(completed_internships) / total_assigned_count * 100)) if total_assigned_count > 0 else 0

    # 7. Recent Mentor Activity (Evaluations conducted by this mentor)
    recent_evaluations = Evaluation.query.filter_by(
        evaluator_id=mentor.id
    ).order_by(Evaluation.evaluated_at.desc()).limit(8).all()

    # 8. Workload Capacity Calculation
    max_cap = mentor.max_capacity if (mentor.max_capacity and mentor.max_capacity > 0) else 5
    capacity_percent = min(100, int((active_intern_count / max_cap) * 100))

    return render_template(
        'mentor/dashboard.html',
        mentor=mentor,
        active_intern_count=active_intern_count,
        total_assigned_count=total_assigned_count,
        pending_count=pending_count,
        active_projects_count=active_projects_count,
        upcoming_deadlines=upcoming_deadlines[:6],
        active_internships=active_internships,
        completed_internships=completed_internships,
        pending_submissions=pending_submissions[:5],
        recent_evaluations=recent_evaluations,
        completion_rate=completion_rate,
        max_cap=max_cap,
        capacity_percent=capacity_percent
    )


# ─── 2. Assigned Interns Directory ────────────────────────────────────────────

@mentor_bp.route('/interns')
@login_required
@mentor_required
def interns():
    mentor = current_user
    q = request.args.get('q', '').strip()
    status_filter = request.args.get('status', 'ACTIVE').upper()
    duration_filter = request.args.get('duration', '').strip()

    query = Internship.query.filter_by(mentor_id=mentor.id).options(
        joinedload(Internship.student).joinedload(Student.user),
        joinedload(Internship.student).joinedload(Student.college),
        joinedload(Internship.student).joinedload(Student.department),
        joinedload(Internship.plan)
    )

    if status_filter != 'ALL':
        query = query.filter(Internship.status == status_filter)

    internships_list = query.order_by(Internship.id.desc()).all()

    # Post-filtering for search query
    if q:
        q_lower = q.lower()
        internships_list = [
            i for i in internships_list
            if (i.student and i.student.user and (
                q_lower in (i.student.user.full_name or '').lower() or
                q_lower in (i.student.student_uid or '').lower() or
                q_lower in (i.student.user.email or '').lower() or
                (i.active_assignment and q_lower in (i.active_assignment.project.title or '').lower())
            ))
        ]

    # Post-filtering for duration plan
    if duration_filter:
        try:
            d_months = int(duration_filter)
            internships_list = [
                i for i in internships_list
                if (i.plan and i.plan.duration_months == d_months)
            ]
        except ValueError:
            pass

    # Quick metric counts for tabs
    total_assigned = Internship.query.filter_by(mentor_id=mentor.id).count()
    active_count = Internship.query.filter_by(mentor_id=mentor.id, status='ACTIVE').count()
    completed_count = Internship.query.filter_by(mentor_id=mentor.id, status='COMPLETED').count()

    return render_template(
        'mentor/interns.html',
        internships=internships_list,
        total_assigned=total_assigned,
        active_count=active_count,
        completed_count=completed_count,
        q=q,
        status_filter=status_filter,
        duration_filter=duration_filter
    )


# ─── 3. Individual Intern 360° Supervision Profile ───────────────────────────

@mentor_bp.route('/interns/<int:student_id>')
@login_required
@mentor_required
def intern_detail(student_id):
    authorized, student, internship = check_mentor_intern_authorization(student_id)
    if not authorized:
        flash('Access denied. You can only view interns explicitly assigned to your mentorship.', 'danger')
        return abort(403)

    assignment = internship.active_assignment if internship else None
    milestones = assignment.weekly_milestones.all() if assignment else []
    
    # Submissions by this student
    submissions = WeeklySubmission.query.filter_by(
        student_id=student.id
    ).order_by(WeeklySubmission.id.desc()).all()

    # Evaluations conducted for this student
    evaluations = Evaluation.query.filter_by(
        employee_id=student.id
    ).order_by(Evaluation.evaluated_at.desc()).all()

    # Scheduled meetings
    meetings = Meeting.query.filter_by(
        student_id=student.id
    ).order_by(Meeting.id.desc()).all()

    return render_template(
        'mentor/intern_detail.html',
        student=student,
        internship=internship,
        assignment=assignment,
        milestones=milestones,
        submissions=submissions,
        evaluations=evaluations,
        meetings=meetings
    )


# ─── 4. Submission Review Queue ───────────────────────────────────────────────

@mentor_bp.route('/submissions')
@login_required
@mentor_required
def submissions():
    mentor = current_user
    status_filter = request.args.get('status', 'PENDING').upper()

    # Retrieve all interns assigned to this mentor
    assigned_internships = Internship.query.filter_by(mentor_id=mentor.id).all()
    student_ids = [i.student_id for i in assigned_internships]

    submissions_query = WeeklySubmission.query.filter(
        WeeklySubmission.student_id.in_(student_ids) if student_ids else False
    ).options(
        joinedload(WeeklySubmission.student).joinedload(Student.user),
        joinedload(WeeklySubmission.milestone).joinedload(WeeklyMilestone.assignment).joinedload(ProjectAssignment.project)
    )

    if status_filter == 'PENDING':
        submissions_query = submissions_query.filter(WeeklySubmission.status.in_(['SUBMITTED', 'UNDER_REVIEW']))
    elif status_filter == 'APPROVED':
        submissions_query = submissions_query.filter(WeeklySubmission.status.in_(['APPROVED', 'COMPLETED']))
    elif status_filter == 'REVISION':
        submissions_query = submissions_query.filter(WeeklySubmission.status.in_(['REVISION_REQUIRED', 'REDO_REQUIRED']))

    submissions_list = submissions_query.order_by(WeeklySubmission.id.desc()).all() if student_ids else []

    # Counts
    pending_count = WeeklySubmission.query.filter(
        WeeklySubmission.student_id.in_(student_ids),
        WeeklySubmission.status.in_(['SUBMITTED', 'UNDER_REVIEW'])
    ).count() if student_ids else 0

    approved_count = WeeklySubmission.query.filter(
        WeeklySubmission.student_id.in_(student_ids),
        WeeklySubmission.status.in_(['APPROVED', 'COMPLETED'])
    ).count() if student_ids else 0

    revision_count = WeeklySubmission.query.filter(
        WeeklySubmission.student_id.in_(student_ids),
        WeeklySubmission.status.in_(['REVISION_REQUIRED', 'REDO_REQUIRED'])
    ).count() if student_ids else 0

    return render_template(
        'mentor/submissions.html',
        submissions=submissions_list,
        pending_count=pending_count,
        approved_count=approved_count,
        revision_count=revision_count,
        status_filter=status_filter
    )


# ─── 5. Deep Submission Review & Scoring ──────────────────────────────────────

@mentor_bp.route('/submissions/<int:submission_id>/review')
@login_required
@mentor_required
def review_submission(submission_id):
    authorized, submission = check_mentor_submission_authorization(submission_id)
    if not authorized:
        flash('Access denied. You can only review submissions for your assigned mentees.', 'danger')
        return abort(403)

    milestone = submission.milestone
    assignment = milestone.assignment
    student = submission.student
    project = assignment.project
    tasks = milestone.tasks.all()

    # Submission history for this milestone
    submission_history = WeeklySubmission.query.filter_by(
        milestone_id=milestone.id
    ).order_by(WeeklySubmission.id.desc()).all()

    # Past evaluations for this milestone
    past_evaluations = Evaluation.query.filter_by(
        milestone_id=milestone.id
    ).order_by(Evaluation.evaluated_at.desc()).all()

    # Active Configurable Rubric (Phase 2 Module 5)
    active_rubric = EvaluationRubric.get_active()

    return render_template(
        'mentor/review.html',
        submission=submission,
        milestone=milestone,
        assignment=assignment,
        student=student,
        project=project,
        tasks=tasks,
        submission_history=submission_history,
        past_evaluations=past_evaluations,
        rubric=active_rubric
    )


# ─── 6. Action: Approve & Advance Milestone ───────────────────────────────────

@mentor_bp.route('/submissions/<int:submission_id>/approve', methods=['POST'])
@login_required
@mentor_required
def submission_approve(submission_id):
    authorized, submission = check_mentor_submission_authorization(submission_id)
    if not authorized:
        flash('Access denied. You cannot approve submissions for unassigned students.', 'danger')
        return abort(403)

    milestone = submission.milestone
    student = submission.student

    # Duplicate progression guard
    if milestone.status == 'COMPLETED':
        flash(f'Week {milestone.week_number} has already been marked COMPLETED.', 'info')
        return redirect(url_for('mentor.submissions'))

    # Extract 5 criteria scores (0-100 each)
    scores_dict = {
        'technical_score': request.form.get('technical_score') or request.form.get('score_technical') or 85,
        'code_quality_score': request.form.get('code_quality_score') or request.form.get('score_code_quality') or 85,
        'functionality_score': request.form.get('functionality_score') or request.form.get('score_functionality') or 85,
        'documentation_score': request.form.get('documentation_score') or request.form.get('score_documentation') or 80,
        'presentation_score': request.form.get('presentation_score') or request.form.get('score_presentation') or 80
    }
    for legacy_k in ['ui_ux_score', 'demo_score', 'originality_score']:
        if legacy_k in request.form:
            scores_dict[legacy_k] = request.form[legacy_k]

    feedback = request.form.get('feedback', '').strip() or 'Deliverables reviewed and approved by technical mentor.'

    try:
        success, total_score = SubmissionWorkflowService.process_evaluation_approval(
            submission=submission,
            evaluator=current_user,
            scores_dict=scores_dict,
            feedback=feedback,
            ip_address=request.remote_addr
        )
        if success:
            flash(f'Week {milestone.week_number} approved! Total Score: {total_score}/100. Deliverables recorded and progression updated for {student.user.full_name}.', 'success')
        else:
            flash(f'Notice: {total_score}', 'warning')
    except Exception as e:
        db.session.rollback()
        flash(f'Error processing approval: {str(e)}', 'danger')

    return redirect(url_for('mentor.submissions'))


# ─── 7. Action: Request Revision / Redo ────────────────────────────────────────

@mentor_bp.route('/submissions/<int:submission_id>/redo', methods=['POST'])
@login_required
@mentor_required
def submission_redo(submission_id):
    authorized, submission = check_mentor_submission_authorization(submission_id)
    if not authorized:
        flash('Access denied. You cannot request revisions for unassigned students.', 'danger')
        return abort(403)

    remarks = request.form.get('feedback', '').strip() or request.form.get('remarks', '').strip()
    if not remarks:
        flash('Technical feedback and revision notes are required when requesting a rework.', 'danger')
        return redirect(url_for('mentor.review_submission', submission_id=submission.id))

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
            flash(f'Revisions requested for Week {submission.milestone.week_number}. Feedback transmitted to {submission.student.user.full_name}.', 'warning')
        else:
            flash(msg, 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Failed to request revisions: {str(e)}', 'danger')

    return redirect(url_for('mentor.submissions'))


# ─── Mentor Task Management (Phase 2 Module 5) ────────────────────────────────

@mentor_bp.route('/milestones/<int:milestone_id>/tasks/create', methods=['POST'])
@login_required
@mentor_required
def create_milestone_task(milestone_id):
    milestone = WeeklyMilestone.query.get_or_404(milestone_id)
    student = milestone.assignment.internship.student
    authorized, _, _ = check_mentor_intern_authorization(student.id)
    if not authorized:
        flash('Access denied. You cannot manage tasks for unassigned interns.', 'danger')
        return abort(403)

    task_title = request.form.get('title', '').strip() or request.form.get('task_text', '').strip()
    if not task_title:
        flash('Task title is required.', 'danger')
        return redirect(request.referrer or url_for('mentor.intern_detail', student_id=student.id))

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
    flash(f'Task "{task_title}" successfully assigned to Week {milestone.week_number}.', 'success')
    return redirect(request.referrer or url_for('mentor.intern_detail', student_id=student.id))


@mentor_bp.route('/milestones/tasks/<int:task_id>/edit', methods=['POST'])
@login_required
@mentor_required
def edit_milestone_task(task_id):
    task = WeeklyTask.query.get_or_404(task_id)
    milestone = task.milestone
    student = milestone.assignment.internship.student
    authorized, _, _ = check_mentor_intern_authorization(student.id)
    if not authorized:
        return abort(403)

    title = request.form.get('title', '').strip() or request.form.get('task_text', '').strip()
    if title:
        task.task_text = title
    task.description = request.form.get('description', task.description or '').strip()
    task.priority = request.form.get('priority', task.priority)
    task.technical_requirements = request.form.get('technical_requirements', task.technical_requirements or '').strip()

    db.session.commit()
    flash(f'Task "{task.task_text}" updated.', 'success')
    return redirect(request.referrer or url_for('mentor.intern_detail', student_id=student.id))


@mentor_bp.route('/milestones/tasks/<int:task_id>/delete', methods=['POST'])
@login_required
@mentor_required
def delete_milestone_task(task_id):
    task = WeeklyTask.query.get_or_404(task_id)
    milestone = task.milestone
    student = milestone.assignment.internship.student
    authorized, _, _ = check_mentor_intern_authorization(student.id)
    if not authorized:
        return abort(403)

    title = task.task_text
    db.session.delete(task)
    db.session.commit()
    flash(f'Task "{title}" removed from Week {milestone.week_number}.', 'success')
    return redirect(request.referrer or url_for('mentor.intern_detail', student_id=student.id))


@mentor_bp.route('/milestones/<int:milestone_id>/configure', methods=['POST'])
@login_required
@mentor_required
def configure_milestone(milestone_id):
    milestone = WeeklyMilestone.query.get_or_404(milestone_id)
    student = milestone.assignment.internship.student
    authorized, _, _ = check_mentor_intern_authorization(student.id)
    if not authorized:
        return abort(403)

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
    return redirect(request.referrer or url_for('mentor.intern_detail', student_id=student.id))


# ─── 8. Mentor Performance Analytics ──────────────────────────────────────────

@mentor_bp.route('/analytics')
@login_required
@mentor_required
def analytics():
    mentor = current_user

    # Internships supervised
    assigned_internships = Internship.query.filter_by(mentor_id=mentor.id).all()
    total_assigned = len(assigned_internships)
    active_count = sum(1 for i in assigned_internships if i.status == 'ACTIVE')
    completed_count = sum(1 for i in assigned_internships if i.status == 'COMPLETED')
    completion_rate = int((completed_count / total_assigned * 100)) if total_assigned > 0 else 0

    # Student IDs
    student_ids = [i.student_id for i in assigned_internships]

    # Submissions
    all_submissions = WeeklySubmission.query.filter(
        WeeklySubmission.student_id.in_(student_ids) if student_ids else False
    ).all() if student_ids else []

    pending_reviews_count = sum(1 for s in all_submissions if s.status in ['SUBMITTED', 'UNDER_REVIEW'])
    reviewed_submissions = [s for s in all_submissions if s.status in ['APPROVED', 'COMPLETED', 'REDO_REQUIRED', 'REVISION_REQUIRED']]
    reviewed_count = len(reviewed_submissions)

    # On-Time Submissions Rate
    if all_submissions:
        on_time_count = 0
        for s in all_submissions:
            m = s.milestone
            if m and m.due_at and s.submitted_at:
                if s.submitted_at <= m.due_at:
                    on_time_count += 1
            else:
                on_time_count += 1
        on_time_rate = round((on_time_count / len(all_submissions)) * 100, 1)
    else:
        on_time_rate = 0.0

    # Approval & Revision Rates
    if reviewed_count > 0:
        approved_count = sum(1 for s in reviewed_submissions if s.status in ['APPROVED', 'COMPLETED'])
        approval_rate = round((approved_count / reviewed_count) * 100, 1)
        revision_count = sum(1 for s in reviewed_submissions if s.status in ['REDO_REQUIRED', 'REVISION_REQUIRED'])
        revision_rate = round((revision_count / reviewed_count) * 100, 1)
    else:
        approval_rate = 0.0
        revision_rate = 0.0

    # Evaluations by this mentor
    evaluations = Evaluation.query.filter_by(
        evaluator_id=mentor.id
    ).order_by(Evaluation.evaluated_at.desc()).all()

    total_evals = len(evaluations)
    avg_score = int(sum(e.total_score for e in evaluations if e.total_score) / total_evals) if total_evals > 0 else 0

    # Review turnaround time calculation (hours between submitted_at and reviewed_at)
    turnaround_hours = []
    for s in all_submissions:
        if s.submitted_at and s.reviewed_at and s.reviewed_at >= s.submitted_at:
            delta = (s.reviewed_at - s.submitted_at).total_seconds() / 3600.0
            turnaround_hours.append(delta)
    avg_turnaround_hours = round(sum(turnaround_hours) / len(turnaround_hours), 1) if turnaround_hours else 0.0

    # Score tiers
    scores_90_plus = sum(1 for e in evaluations if (e.total_score or 0) >= 90)
    scores_75_89 = sum(1 for e in evaluations if 75 <= (e.total_score or 0) < 90)
    scores_under_75 = sum(1 for e in evaluations if (e.total_score or 0) < 75)

    # Detailed Mentees Performance Roster
    mentees_roster = []
    for i in assigned_internships:
        s = i.student
        if not s:
            continue
        assignment = i.assignments.first()
        proj = assignment.project if assignment else None
        milestones = assignment.weekly_milestones.all() if assignment else []
        tot_m = len(milestones)
        done_m = sum(1 for m in milestones if m.status in ['COMPLETED', 'APPROVED'])
        m_rate = round((done_m / tot_m * 100), 1) if tot_m > 0 else 0.0

        s_evals = Evaluation.query.filter_by(employee_id=s.id).all()
        s_scores = [e.total_score for e in s_evals if e.total_score is not None]
        s_avg_score = round(sum(s_scores) / len(s_scores), 1) if s_scores else None

        mentees_roster.append({
            'student': s,
            'intern_id': AnalyticsReportingService._get_student_intern_id(s, i),
            'full_name': AnalyticsReportingService._get_student_name(s),
            'email': AnalyticsReportingService._get_student_email(s),
            'project_title': proj.title if proj else 'Unallocated',
            'status': i.status,
            'progress_percent': i.progress_percent if i.progress_percent is not None else 0,
            'milestones_completed': done_m,
            'milestones_total': tot_m,
            'milestone_rate': m_rate,
            'avg_score': s_avg_score
        })

    return render_template(
        'mentor/analytics.html',
        mentor=mentor,
        total_assigned=total_assigned,
        active_count=active_count,
        completed_count=completed_count,
        completion_rate=completion_rate,
        pending_reviews_count=pending_reviews_count,
        total_evals=total_evals,
        avg_score=avg_score,
        avg_turnaround_hours=avg_turnaround_hours,
        on_time_rate=on_time_rate,
        approval_rate=approval_rate,
        revision_rate=revision_rate,
        scores_90_plus=scores_90_plus,
        scores_75_89=scores_75_89,
        scores_under_75=scores_under_75,
        mentees_roster=mentees_roster,
        evaluations=evaluations[:15]
    )


@mentor_bp.route('/analytics/export/csv')
@login_required
@mentor_required
def export_mentees_csv():
    """Exports assigned mentees progress and evaluation metrics as CSV."""
    csv_data = AnalyticsReportingService.generate_mentor_mentees_csv(current_user.id)
    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    filename = f"antimatrix_mentees_{current_user.id}_{timestamp}.csv"
    bom_csv = '\ufeff' + csv_data

    return Response(
        bom_csv,
        mimetype='text/csv; charset=utf-8',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Type': 'text/csv; charset=utf-8'
        }
    )


# ─── 9. Mentor Profile & Availability Management ──────────────────────────────

@mentor_bp.route('/profile', methods=['GET', 'POST'])
@login_required
@mentor_required
def profile():
    mentor = current_user

    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        skills = request.form.get('skills', '').strip()
        availability_status = request.form.get('availability_status', 'AVAILABLE').upper()

        if availability_status not in ['AVAILABLE', 'BUSY', 'ON_LEAVE']:
            availability_status = 'AVAILABLE'

        mentor.phone = phone
        mentor.skills = skills
        mentor.availability_status = availability_status
        db.session.commit()

        flash('Your mentor profile and availability status have been updated.', 'success')
        return redirect(url_for('mentor.profile'))

    return render_template(
        'mentor/profile.html',
        mentor=mentor
    )


# ─── 10. Schedule Evaluation Meeting ──────────────────────────────────────────

@mentor_bp.route('/meeting/schedule', methods=['POST'])
@login_required
@mentor_required
def schedule_meeting():
    student_id = request.form.get('student_id', type=int)
    authorized, student, internship = check_mentor_intern_authorization(student_id)
    if not authorized or not internship:
        flash('Access denied. You can only schedule sessions with your assigned mentees.', 'danger')
        return abort(403)

    title = request.form.get('title', '').strip() or 'Weekly Milestone Evaluation Session'
    meeting_date = request.form.get('meeting_date', '').strip()
    meeting_time = request.form.get('meeting_time', '').strip()
    meeting_link = request.form.get('meeting_link', '').strip() or 'https://meet.google.com/anti-matrix-mentorship'
    notes = request.form.get('meeting_notes', '').strip()

    if not meeting_date or not meeting_time:
        flash('Meeting date and time are required.', 'danger')
        return redirect(url_for('mentor.intern_detail', student_id=student.id))

    meeting = Meeting(
        internship_id=internship.id,
        student_id=student.id,
        host_id=current_user.id,
        title=title,
        meeting_date=meeting_date,
        meeting_time=meeting_time,
        meeting_link=meeting_link,
        meeting_notes=notes,
        status='SCHEDULED'
    )
    db.session.add(meeting)

    notif = Notification(
        user_id=student.user.id,
        title=f'Mentorship Sync Scheduled: {title}',
        message=f'Mentor {current_user.full_name} scheduled a meeting on {meeting_date} at {meeting_time}. Link: {meeting_link}',
        type='INFO',
        link=meeting_link
    )
    db.session.add(notif)
    db.session.commit()

    flash(f'Evaluation session scheduled with {student.user.full_name} on {meeting_date} at {meeting_time}.', 'success')
    return redirect(url_for('mentor.intern_detail', student_id=student.id))


# ─── Module 7: Mentor Announcements & Mentee Communication ───────────────────

@mentor_bp.route('/announcements', methods=['GET'])
@login_required
@mentor_required
def mentor_announcements():
    """
    Mentorship communication portal for broadcast updates to assigned mentees.
    """
    announcements_list = Announcement.query.filter(
        (Announcement.author_id == current_user.id) |
        (Announcement.target_role.in_(['ALL', 'MENTOR']))
    ).order_by(Announcement.id.desc()).all()

    assigned_internships = Internship.query.filter_by(
        mentor_id=current_user.id,
        status='ACTIVE'
    ).all()
    mentees = [i.student for i in assigned_internships if i.student]

    return render_template(
        'mentor/announcements.html',
        announcements=announcements_list,
        mentees=mentees,
        unread_count=current_user.unread_notifications_count
    )


@mentor_bp.route('/announcements/create', methods=['POST'])
@login_required
@mentor_required
def create_mentee_broadcast():
    """
    Broadcasts a technical update or announcement specifically to the mentor's assigned mentees.
    """
    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    priority = request.form.get('priority', 'NORMAL').strip().upper()

    if not title or not content:
        flash('Broadcast title and message content are required.', 'danger')
        return redirect(url_for('mentor.mentor_announcements'))

    assigned_internships = Internship.query.filter_by(
        mentor_id=current_user.id,
        status='ACTIVE'
    ).all()
    mentee_users = [i.student.user for i in assigned_internships if i.student and i.student.user]

    try:
        announcement = Announcement(
            author_id=current_user.id,
            title=title,
            content=content,
            target_role='STUDENT',
            priority=priority,
            created_at=datetime.utcnow()
        )
        db.session.add(announcement)
        db.session.commit()

        author_name = f"Mentor {current_user.full_name or current_user.name}"

        # Dispatch notifications to assigned mentees
        for u in mentee_users:
            try:
                NotificationService.notify_announcement(
                    recipient_user_id=u.id,
                    announcement_id=announcement.id,
                    announcement_title=title,
                    content=content,
                    author_name=author_name,
                    priority=priority,
                    target_role='STUDENT'
                )
            except Exception as e:
                logger.warning(f"Could not dispatch broadcast to mentee {u.id}: {e}")

        # Audit Log
        audit = AuditLog(
            actor_id=current_user.id,
            actor_role='mentor',
            action='MENTOR_BROADCAST_MENTEES',
            target_entity='announcements',
            target_id=str(announcement.id),
            details_json=json.dumps({
                'title': title,
                'priority': priority,
                'mentees_count': len(mentee_users)
            })
        )
        db.session.add(audit)
        db.session.commit()

        flash(f"Broadcast '{title}' successfully sent to {len(mentee_users)} assigned mentees.", 'success')
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to publish mentor broadcast: {e}")
        flash(f"Error sending broadcast: {str(e)}", 'danger')

    return redirect(url_for('mentor.mentor_announcements'))


# ─── 8. Mentor Attendance & Leave Supervision (Phase 2 Module 9) ──────────────

@mentor_bp.route('/attendance', endpoint='attendance')
@mentor_bp.route('/attendance/roster', endpoint='mentor_attendance')
@login_required
@mentor_required
def attendance():
    """
    Mentor attendance workspace:
    - Roster of all assigned mentees with real-time status (Checked-in, Late, Absent, On Leave)
    - Attendance completion percentages and KPI overview
    - Deep-dive into a specific mentee's monthly calendar and check-in history
    """
    # 1. Fetch assigned internships
    if current_user.role in ['admin', 'super_admin']:
        internships = Internship.query.filter(
            Internship.status.in_(['ONBOARDING', 'ALLOCATED', 'ACTIVE', 'COMPLETED'])
        ).options(joinedload(Internship.student).joinedload(Student.user)).all()
    else:
        internships = Internship.query.filter(
            Internship.mentor_id == current_user.id,
            Internship.status.in_(['ONBOARDING', 'ALLOCATED', 'ACTIVE', 'COMPLETED'])
        ).options(joinedload(Internship.student).joinedload(Student.user)).all()

    today_date = datetime.now().date()
    mentee_cards = []

    for i in internships:
        s = i.student
        if not s:
            continue
        today_att = AttendanceService.get_today_attendance(s.id, today_date)
        summary = AttendanceService.get_student_attendance_summary(s.id)
        mentee_cards.append({
            'internship': i,
            'student': s,
            'today_record': today_att,
            'summary': summary
        })

    # Optional deep-dive for a selected student
    selected_student_id = request.args.get('student_id', type=int)
    selected_student = None
    selected_matrix = None
    selected_summary = None
    selected_schedule = None
    month_name = None

    if selected_student_id:
        auth_ok, student_obj, internship_obj = check_mentor_intern_authorization(selected_student_id)
        if auth_ok and student_obj:
            selected_student = student_obj
            try:
                year = int(request.args.get('year', today_date.year))
                month = int(request.args.get('month', today_date.month))
            except (ValueError, TypeError):
                year = today_date.year
                month = today_date.month
            selected_matrix = AttendanceService.get_monthly_matrix(selected_student.id, year, month)
            selected_summary = AttendanceService.get_student_attendance_summary(selected_student.id, month=month, year=year)
            if internship_obj:
                try:
                    selected_schedule = AttendanceService.get_internship_schedule_summary(internship_obj.id)
                except Exception:
                    pass
            month_name = date(year, month, 1).strftime('%B %Y')

    return render_template(
        'mentor/attendance.html',
        mentee_cards=mentee_cards,
        today_date=today_date,
        selected_student=selected_student,
        selected_matrix=selected_matrix,
        selected_summary=selected_summary,
        selected_schedule=selected_schedule,
        month_name=month_name
    )


@mentor_bp.route('/leave', endpoint='leave_queue')
@mentor_bp.route('/leave/dashboard', endpoint='mentor_leave_dashboard')
@login_required
@mentor_required
def leave_queue():
    """
    Mentor leave management queue:
    - View all leave requests submitted by assigned mentees
    - Filter by status (PENDING, APPROVED, REJECTED, ALL)
    - Review applications and verify supporting documentation
    """
    if current_user.role in ['admin', 'super_admin']:
        student_ids = [s.id for s in Student.query.all()]
    else:
        assigned_internships = Internship.query.filter_by(mentor_id=current_user.id).all()
        student_ids = [i.student_id for i in assigned_internships if i.student_id]

    status_filter = request.args.get('status', 'all').upper().strip()

    q = LeaveRequest.query.filter(LeaveRequest.student_id.in_(student_ids if student_ids else [-1]))
    if status_filter in ['PENDING', 'APPROVED', 'REJECTED', 'CANCELLED']:
        q = q.filter(LeaveRequest.status == status_filter)

    leave_requests = q.order_by(
        LeaveRequest.status == 'PENDING',
        LeaveRequest.created_at.desc()
    ).all()

    # Pre-count KPIs
    all_requests = LeaveRequest.query.filter(LeaveRequest.student_id.in_(student_ids if student_ids else [-1])).all()
    pending_count = sum(1 for l in all_requests if l.status == 'PENDING')
    approved_count = sum(1 for l in all_requests if l.status == 'APPROVED')
    rejected_count = sum(1 for l in all_requests if l.status == 'REJECTED')

    return render_template(
        'mentor/leave.html',
        leave_requests=leave_requests,
        status_filter=status_filter,
        pending_count=pending_count,
        approved_count=approved_count,
        rejected_count=rejected_count,
        today_date=datetime.now().date()
    )


@mentor_bp.route('/leave/<int:leave_id>/review', methods=['POST'])
@login_required
@mentor_required
def review_leave(leave_id):
    """
    Mentor decision workflow for an intern leave application:
    - Enforces mentee ownership authorization
    - Supports APPROVE or REJECT with feedback
    - Automatically updates attendance records upon approval
    """
    leave = db.session.get(LeaveRequest, leave_id)
    if not leave:
        flash("Leave request not found.", "danger")
        return redirect(url_for('mentor.mentor_leave_dashboard'))

    # Verify mentor authority over this intern
    auth_ok, _, _ = check_mentor_intern_authorization(leave.student_id)
    if not auth_ok:
        flash("Unauthorized: You can only review leave applications for your assigned mentees.", "danger")
        return redirect(url_for('mentor.mentor_leave_dashboard'))

    action = request.form.get('action', '').upper().strip()
    remarks = request.form.get('remarks', '').strip()

    if action not in ['APPROVE', 'REJECT']:
        flash("Invalid review action specified.", "danger")
        return redirect(url_for('mentor.mentor_leave_dashboard'))

    try:
        updated_leave = AttendanceService.review_leave(
            leave_id=leave.id,
            reviewer_id=current_user.id,
            action=action,
            remarks=remarks if remarks else None
        )
        flash(f"Leave request for {updated_leave.student.full_name} has been {updated_leave.status.lower()} successfully.", "success")
    except ValueError as ve:
        flash(str(ve), "danger")
    except Exception as e:
        flash(f"Error reviewing leave: {str(e)}", "danger")

    return redirect(url_for('mentor.mentor_leave_dashboard'))

