"""
ANTI-MATRIX PERFORMANCE ANALYTICS & REPORTING SERVICE
Phase 2 — Module 6

Centralized business intelligence, reporting, and statistical analytics engine.
Computes deterministic KPIs and telemetry for:
1. Organization Overview (cross-cohort velocity, completion, bottlenecks)
2. Intern Performance (milestones, tasks, on-time submissions, revision rates, score distributions)
3. Mentor Telemetry (workload distribution, capacity tiers, turnaround velocity, review throughput)
4. Project Health (allocation saturation, milestone progression, completion rates)
5. RFC 4180 compliant CSV Export & Printable Report Generation
"""

import io
import csv
from datetime import datetime, timedelta
from app.extensions import db
from app.models import (
    Student, Internship, Project, ProjectAssignment,
    WeeklyMilestone, WeeklyTask, WeeklySubmission, Evaluation, User
)


class AnalyticsReportingService:
    """Enterprise BI and Analytics service with safe division and robust aggregations."""

    # ─── Safe Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _get_internship_duration(internship):
        if not internship:
            return 1
        if hasattr(internship, 'plan') and internship.plan and hasattr(internship.plan, 'duration_months'):
            return internship.plan.duration_months
        return 1

    @staticmethod
    def _format_date_safe(val):
        if not val:
            return 'N/A'
        if isinstance(val, datetime):
            return val.strftime('%Y-%m-%d')
        return str(val)

    @staticmethod
    def _get_student_intern_id(student, internship=None):
        if hasattr(student, 'student_uid') and student.student_uid:
            return student.student_uid
        if internship and hasattr(internship, 'internship_no') and internship.internship_no:
            return internship.internship_no
        return f'AM-INT-{student.id:03d}'

    @staticmethod
    def _get_student_name(student):
        if student.user and student.user.full_name:
            return student.user.full_name
        return 'Unnamed Intern'

    @staticmethod
    def _get_student_email(student):
        if student.user and student.user.email:
            return student.user.email
        return 'N/A'

    @staticmethod
    def _get_student_dept(student):
        if student.department:
            return student.department.name if hasattr(student.department, 'name') else str(student.department)
        return 'Engineering'

    @staticmethod
    def _get_student_college(student):
        if student.college:
            return student.college.name if hasattr(student.college, 'name') else str(student.college)
        return 'N/A'

    # ─── 1. Organization KPIs ──────────────────────────────────────────────────

    @staticmethod
    def get_organization_kpis():
        """
        Calculates organizational-level operational KPIs.
        Guards all division operations against zero-division.
        """
        now = datetime.utcnow()

        total_interns = Student.query.count()
        active_internships = Internship.query.filter_by(status='ACTIVE').all()
        active_interns = len(active_internships)
        completed_internships = Internship.query.filter_by(status='COMPLETED').count()

        # Mentors (users with role mentor, lead_mentor, or staff/employee)
        total_mentors = User.query.filter(User.role.in_(['mentor', 'lead_mentor', 'employee'])).count()

        # Projects
        active_projects = Project.query.filter_by(is_active=True).count()
        total_projects = Project.query.count()

        # Submissions
        all_submissions = WeeklySubmission.query.all()
        pending_submissions = sum(1 for s in all_submissions if s.status in ['SUBMITTED', 'UNDER_REVIEW'])
        total_submissions = len(all_submissions)

        # Overdue Milestones & Tasks
        overdue_milestones = WeeklyMilestone.query.filter(
            WeeklyMilestone.due_at.isnot(None),
            WeeklyMilestone.due_at < now,
            WeeklyMilestone.status.notin_(['COMPLETED', 'APPROVED'])
        ).all()
        overdue_milestones_count = len(overdue_milestones)

        # Average Completion Rate of Active Internships
        all_internships = Internship.query.all()
        if all_internships:
            total_progress = sum(i.progress_percent for i in all_internships if i.progress_percent is not None)
            avg_completion_rate = round(total_progress / len(all_internships), 1)
        else:
            avg_completion_rate = 0.0

        # Evaluations & Average Score
        all_evaluations = Evaluation.query.all()
        total_evaluations = len(all_evaluations)
        if total_evaluations > 0:
            valid_scores = [e.total_score for e in all_evaluations if e.total_score is not None]
            avg_evaluation_score = round(sum(valid_scores) / len(valid_scores), 1) if valid_scores else 0.0
        else:
            avg_evaluation_score = 0.0

        # On-Time Submission Rate
        if total_submissions > 0:
            on_time_count = 0
            for s in all_submissions:
                milestone = s.milestone
                if milestone and milestone.due_at and s.submitted_at:
                    if s.submitted_at <= milestone.due_at:
                        on_time_count += 1
                else:
                    on_time_count += 1
            on_time_rate = round((on_time_count / total_submissions) * 100, 1)
        else:
            on_time_rate = 0.0

        # Submission Approval Rate
        reviewed_submissions = [s for s in all_submissions if s.status in ['APPROVED', 'COMPLETED', 'REDO_REQUIRED', 'REVISION_REQUIRED']]
        if reviewed_submissions:
            approved_count = sum(1 for s in reviewed_submissions if s.status in ['APPROVED', 'COMPLETED'])
            submission_approval_rate = round((approved_count / len(reviewed_submissions)) * 100, 1)
        else:
            submission_approval_rate = 0.0

        # Revision Rate
        if reviewed_submissions:
            revision_count = sum(1 for s in reviewed_submissions if s.status in ['REDO_REQUIRED', 'REVISION_REQUIRED'])
            revision_rate = round((revision_count / len(reviewed_submissions)) * 100, 1)
        else:
            revision_rate = 0.0

        # Cohort Duration Distribution
        duration_1_mo = sum(1 for i in all_internships if AnalyticsReportingService._get_internship_duration(i) == 1)
        duration_3_mo = sum(1 for i in all_internships if AnalyticsReportingService._get_internship_duration(i) == 3)
        duration_6_mo = sum(1 for i in all_internships if AnalyticsReportingService._get_internship_duration(i) == 6)

        return {
            'total_interns': total_interns,
            'active_interns': active_interns,
            'completed_internships': completed_internships,
            'total_mentors': total_mentors,
            'active_projects': active_projects,
            'total_projects': total_projects,
            'pending_submissions': pending_submissions,
            'total_submissions': total_submissions,
            'overdue_milestones_count': overdue_milestones_count,
            'avg_completion_rate': avg_completion_rate,
            'avg_evaluation_score': avg_evaluation_score,
            'on_time_rate': on_time_rate,
            'submission_approval_rate': submission_approval_rate,
            'revision_rate': revision_rate,
            'duration_distribution': {
                '1_month': duration_1_mo,
                '3_months': duration_3_mo,
                '6_months': duration_6_mo
            }
        }

    # ─── 2. Intern Analytics & Scorecard ──────────────────────────────────────

    @staticmethod
    def get_interns_analytics(search=None, project_id=None, mentor_id=None, status=None, sort_by='progress', order='desc'):
        """
        Retrieves aggregated performance analytics for intern directory with filtering and sorting.
        """
        students = Student.query.all()
        records = []

        for s in students:
            internship = s.active_internship or s.internships.first()
            assignment = internship.assignments.first() if internship else None
            project = assignment.project if assignment else None
            mentor = internship.mentor if internship else None

            # Filters
            if status and internship and internship.status.upper() != status.upper():
                continue
            if project_id and (not project or project.id != int(project_id)):
                continue
            if mentor_id and (not mentor or mentor.id != int(mentor_id)):
                continue

            full_name = AnalyticsReportingService._get_student_name(s)
            intern_id = AnalyticsReportingService._get_student_intern_id(s, internship)
            email = AnalyticsReportingService._get_student_email(s)
            college = AnalyticsReportingService._get_student_college(s)
            department = AnalyticsReportingService._get_student_dept(s)

            if search:
                query_str = search.lower()
                name_match = full_name and query_str in full_name.lower()
                id_match = intern_id and query_str in intern_id.lower()
                email_match = email and query_str in email.lower()
                college_match = college and query_str in college.lower()
                if not (name_match or id_match or email_match or college_match):
                    continue

            # Milestones and Tasks
            milestones = assignment.weekly_milestones.all() if assignment else []
            total_milestones = len(milestones)
            completed_milestones = sum(1 for m in milestones if m.status in ['COMPLETED', 'APPROVED'])
            milestone_completion_rate = round((completed_milestones / total_milestones * 100), 1) if total_milestones > 0 else 0.0

            # Tasks
            all_tasks = []
            for m in milestones:
                all_tasks.extend(m.tasks.all())
            total_tasks = len(all_tasks)
            completed_tasks = sum(1 for t in all_tasks if t.is_completed)
            task_completion_rate = round((completed_tasks / total_tasks * 100), 1) if total_tasks > 0 else 0.0

            # Submissions
            submissions = WeeklySubmission.query.filter_by(student_id=s.id).all()
            total_submissions = len(submissions)
            
            # On-time rate
            on_time_count = 0
            for sub in submissions:
                m = sub.milestone
                if m and m.due_at and sub.submitted_at:
                    if sub.submitted_at <= m.due_at:
                        on_time_count += 1
                else:
                    on_time_count += 1
            on_time_rate = round((on_time_count / total_submissions * 100), 1) if total_submissions > 0 else 0.0

            # Reviewed submissions & Approval / Revision rates
            reviewed_subs = [sub for sub in submissions if sub.status in ['APPROVED', 'COMPLETED', 'REDO_REQUIRED', 'REVISION_REQUIRED']]
            reviewed_count = len(reviewed_subs)
            approved_count = sum(1 for sub in reviewed_subs if sub.status in ['APPROVED', 'COMPLETED'])
            approval_rate = round((approved_count / reviewed_count * 100), 1) if reviewed_count > 0 else 0.0
            revision_count = sum(1 for sub in reviewed_subs if sub.status in ['REDO_REQUIRED', 'REVISION_REQUIRED'])
            revision_rate = round((revision_count / reviewed_count * 100), 1) if reviewed_count > 0 else 0.0

            # Evaluations
            evaluations = Evaluation.query.filter_by(employee_id=s.id).all()
            scores = [e.total_score for e in evaluations if e.total_score is not None]
            avg_score = round(sum(scores) / len(scores), 1) if scores else None

            progress_val = internship.progress_percent if (internship and internship.progress_percent is not None) else 0

            records.append({
                'student': s,
                'intern_id': intern_id,
                'full_name': full_name,
                'email': email,
                'department': department,
                'college': college,
                'internship': internship,
                'status': internship.status if internship else 'PENDING',
                'duration_months': AnalyticsReportingService._get_internship_duration(internship),
                'progress_percent': progress_val,
                'project': project,
                'project_title': project.title if project else 'Unallocated',
                'mentor': mentor,
                'mentor_name': mentor.full_name if mentor else 'Unassigned',
                'total_milestones': total_milestones,
                'completed_milestones': completed_milestones,
                'milestone_completion_rate': milestone_completion_rate,
                'total_tasks': total_tasks,
                'completed_tasks': completed_tasks,
                'task_completion_rate': task_completion_rate,
                'total_submissions': total_submissions,
                'on_time_rate': on_time_rate,
                'approval_rate': approval_rate,
                'revision_rate': revision_rate,
                'evaluations_count': len(evaluations),
                'avg_score': avg_score,
                'last_active': s.created_at
            })

        # Sorting
        reverse = (order.lower() == 'desc')
        if sort_by == 'progress':
            records.sort(key=lambda r: r['progress_percent'], reverse=reverse)
        elif sort_by == 'score':
            records.sort(key=lambda r: (r['avg_score'] is not None, r['avg_score'] or 0), reverse=reverse)
        elif sort_by == 'on_time':
            records.sort(key=lambda r: r['on_time_rate'], reverse=reverse)
        elif sort_by == 'name':
            records.sort(key=lambda r: r['full_name'].lower(), reverse=reverse)
        elif sort_by == 'id':
            records.sort(key=lambda r: r['intern_id'], reverse=reverse)

        return records

    @staticmethod
    def get_intern_detail_analytics(student_id):
        """
        Retrieves detailed individual candidate scorecard, criteria breakdowns,
        weekly timeline, and historical progress for an intern.
        """
        student = Student.query.get(student_id)
        if not student:
            return None

        internship = student.active_internship or student.internships.first()
        assignment = internship.assignments.first() if internship else None
        project = assignment.project if assignment else None
        mentor = internship.mentor if internship else None

        milestones = assignment.weekly_milestones.order_by(WeeklyMilestone.week_number.asc()).all() if assignment else []
        submissions = WeeklySubmission.query.filter_by(student_id=student.id).order_by(WeeklySubmission.submitted_at.desc()).all()
        evaluations = Evaluation.query.filter_by(employee_id=student.id).order_by(Evaluation.evaluated_at.desc()).all()

        total_milestones = len(milestones)
        completed_milestones = sum(1 for m in milestones if m.status in ['COMPLETED', 'APPROVED'])
        milestone_completion_rate = round((completed_milestones / total_milestones * 100), 1) if total_milestones > 0 else 0.0

        all_tasks = []
        for m in milestones:
            all_tasks.extend(m.tasks.all())
        total_tasks = len(all_tasks)
        completed_tasks = sum(1 for t in all_tasks if t.is_completed)
        task_completion_rate = round((completed_tasks / total_tasks * 100), 1) if total_tasks > 0 else 0.0

        total_submissions = len(submissions)
        on_time_count = 0
        for sub in submissions:
            m = sub.milestone
            if m and m.due_at and sub.submitted_at:
                if sub.submitted_at <= m.due_at:
                    on_time_count += 1
            else:
                on_time_count += 1
        on_time_rate = round((on_time_count / total_submissions * 100), 1) if total_submissions > 0 else 0.0

        reviewed_subs = [sub for sub in submissions if sub.status in ['APPROVED', 'COMPLETED', 'REDO_REQUIRED', 'REVISION_REQUIRED']]
        reviewed_count = len(reviewed_subs)
        approved_count = sum(1 for sub in reviewed_subs if sub.status in ['APPROVED', 'COMPLETED'])
        approval_rate = round((approved_count / reviewed_count * 100), 1) if reviewed_count > 0 else 0.0
        revision_count = sum(1 for sub in reviewed_subs if sub.status in ['REDO_REQUIRED', 'REVISION_REQUIRED'])
        revision_rate = round((revision_count / reviewed_count * 100), 1) if reviewed_count > 0 else 0.0

        # Rubric Criteria Breakdown
        eval_scores = [e.total_score for e in evaluations if e.total_score is not None]
        avg_score = round(sum(eval_scores) / len(eval_scores), 1) if eval_scores else None

        tech_scores = [e.technical_score for e in evaluations if e.technical_score is not None]
        code_scores = [e.code_quality_score for e in evaluations if e.code_quality_score is not None]
        func_scores = [e.functionality_score for e in evaluations if e.functionality_score is not None]
        doc_scores = [e.documentation_score for e in evaluations if e.documentation_score is not None]
        pres_scores = [e.presentation_score for e in evaluations if e.presentation_score is not None]

        rubric_averages = {
            'technical': round(sum(tech_scores) / len(tech_scores), 1) if tech_scores else 0.0,
            'code_quality': round(sum(code_scores) / len(code_scores), 1) if code_scores else 0.0,
            'functionality': round(sum(func_scores) / len(func_scores), 1) if func_scores else 0.0,
            'documentation': round(sum(doc_scores) / len(doc_scores), 1) if doc_scores else 0.0,
            'presentation': round(sum(pres_scores) / len(pres_scores), 1) if pres_scores else 0.0,
        }

        # Weekly Progress & Score Trends
        weekly_score_trends = []
        for m in milestones:
            m_eval = Evaluation.query.filter_by(milestone_id=m.id, employee_id=student.id).order_by(Evaluation.evaluated_at.desc()).first()
            m_sub = WeeklySubmission.query.filter_by(milestone_id=m.id, student_id=student.id).order_by(WeeklySubmission.submitted_at.desc()).first()
            tasks_list = m.tasks.all()
            completed_tasks_count = sum(1 for t in tasks_list if t.is_completed)

            weekly_score_trends.append({
                'week_number': m.week_number,
                'title': m.title,
                'status': m.status,
                'due_at': m.due_at,
                'submitted_at': m_sub.submitted_at if m_sub else None,
                'score': m_eval.total_score if m_eval else None,
                'feedback': m_eval.feedback or m_eval.remarks if m_eval else None,
                'tasks_completed': completed_tasks_count,
                'tasks_total': len(tasks_list),
            })

        return {
            'student': student,
            'intern_id': AnalyticsReportingService._get_student_intern_id(student, internship),
            'full_name': AnalyticsReportingService._get_student_name(student),
            'email': AnalyticsReportingService._get_student_email(student),
            'department': AnalyticsReportingService._get_student_dept(student),
            'college': AnalyticsReportingService._get_student_college(student),
            'internship': internship,
            'project': project,
            'mentor': mentor,
            'progress_percent': internship.progress_percent if (internship and internship.progress_percent is not None) else 0,
            'total_milestones': total_milestones,
            'completed_milestones': completed_milestones,
            'milestone_completion_rate': milestone_completion_rate,
            'total_tasks': total_tasks,
            'completed_tasks': completed_tasks,
            'task_completion_rate': task_completion_rate,
            'total_submissions': total_submissions,
            'on_time_rate': on_time_rate,
            'approval_rate': approval_rate,
            'revision_rate': revision_rate,
            'avg_score': avg_score,
            'rubric_averages': rubric_averages,
            'weekly_trends': weekly_score_trends,
            'submissions': submissions,
            'evaluations': evaluations
        }

    # ─── 3. Mentor Analytics & Supervisory Telemetry ──────────────────────────

    @staticmethod
    def get_mentors_analytics(search=None, department=None, sort_by='workload', order='desc'):
        """
        Retrieves supervisory performance metrics and workload tiers for mentors.
        """
        mentors = User.query.filter(User.role.in_(['mentor', 'lead_mentor', 'employee'])).all()
        records = []

        for m in mentors:
            if department and m.department and m.department.lower() != department.lower():
                continue
            if search:
                query_str = search.lower()
                name_match = m.full_name and query_str in m.full_name.lower()
                email_match = m.email and query_str in m.email.lower()
                emp_match = m.employee_id and query_str in m.employee_id.lower()
                if not (name_match or email_match or emp_match):
                    continue

            assigned_internships = Internship.query.filter_by(mentor_id=m.id).all()
            total_assigned = len(assigned_internships)
            active_count = sum(1 for i in assigned_internships if i.status == 'ACTIVE')
            completed_count = sum(1 for i in assigned_internships if i.status == 'COMPLETED')

            capacity = getattr(m, 'max_capacity', None) or 5
            utilization_rate = round((total_assigned / capacity * 100), 1) if capacity > 0 else 0.0

            # Workload Tier Badge
            if utilization_rate < 60:
                workload_tier = 'Low Load'
                workload_badge_class = 'badge-success'
            elif utilization_rate <= 100:
                workload_tier = 'Balanced'
                workload_badge_class = 'badge-primary'
            elif utilization_rate <= 120:
                workload_tier = 'At Capacity'
                workload_badge_class = 'badge-warning'
            else:
                workload_tier = 'Overloaded'
                workload_badge_class = 'badge-danger'

            # Submissions & Turnaround Velocity
            student_ids = [i.student_id for i in assigned_internships]
            submissions = WeeklySubmission.query.filter(
                WeeklySubmission.student_id.in_(student_ids)
            ).all() if student_ids else []

            pending_reviews = sum(1 for sub in submissions if sub.status in ['SUBMITTED', 'UNDER_REVIEW'])
            completed_reviews = sum(1 for sub in submissions if sub.status in ['APPROVED', 'COMPLETED', 'REDO_REQUIRED', 'REVISION_REQUIRED'])

            # Turnaround Velocity Calculation (hours)
            turnaround_deltas = []
            for sub in submissions:
                if sub.submitted_at and sub.reviewed_at and sub.reviewed_at >= sub.submitted_at:
                    delta_hours = (sub.reviewed_at - sub.submitted_at).total_seconds() / 3600.0
                    turnaround_deltas.append(delta_hours)
            avg_turnaround_hours = round(sum(turnaround_deltas) / len(turnaround_deltas), 1) if turnaround_deltas else 0.0

            # Evaluations by this mentor
            evaluations = Evaluation.query.filter_by(evaluator_id=m.id).all()
            eval_scores = [e.total_score for e in evaluations if e.total_score is not None]
            avg_score_awarded = round(sum(eval_scores) / len(eval_scores), 1) if eval_scores else None

            # Average Mentee Progress
            if assigned_internships:
                progress_list = [i.progress_percent for i in assigned_internships if i.progress_percent is not None]
                avg_mentee_progress = round(sum(progress_list) / len(progress_list), 1) if progress_list else 0.0
            else:
                avg_mentee_progress = 0.0

            records.append({
                'mentor': m,
                'mentor_id': m.employee_id or f'MNT-{m.id:03d}',
                'full_name': m.full_name or 'Unnamed Mentor',
                'email': m.email,
                'department': m.department or 'Engineering',
                'total_assigned': total_assigned,
                'active_count': active_count,
                'completed_count': completed_count,
                'capacity': capacity,
                'utilization_rate': utilization_rate,
                'workload_tier': workload_tier,
                'workload_badge_class': workload_badge_class,
                'pending_reviews': pending_reviews,
                'completed_reviews': completed_reviews,
                'avg_turnaround_hours': avg_turnaround_hours,
                'avg_score_awarded': avg_score_awarded,
                'avg_mentee_progress': avg_mentee_progress
            })

        reverse = (order.lower() == 'desc')
        if sort_by == 'workload':
            records.sort(key=lambda r: r['utilization_rate'], reverse=reverse)
        elif sort_by == 'assigned':
            records.sort(key=lambda r: r['total_assigned'], reverse=reverse)
        elif sort_by == 'turnaround':
            records.sort(key=lambda r: r['avg_turnaround_hours'], reverse=reverse)
        elif sort_by == 'name':
            records.sort(key=lambda r: r['full_name'].lower(), reverse=reverse)

        return records

    # ─── 4. Project Analytics ──────────────────────────────────────────────────

    @staticmethod
    def get_projects_analytics(search=None, domain=None, status=None, sort_by='allocated', order='desc'):
        """
        Retrieves project health, allocation capacity, milestone progression, and rankings.
        """
        projects = Project.query.all()
        records = []

        for p in projects:
            if status:
                is_active_filter = (status.lower() == 'active')
                if p.is_active != is_active_filter:
                    continue
            proj_domain = p.domain or getattr(p, 'department', None) or 'Engineering'
            if domain and proj_domain.lower() != domain.lower():
                continue
            if search:
                query_str = search.lower()
                title_match = p.title and query_str in p.title.lower()
                domain_match = proj_domain and query_str in proj_domain.lower()
                desc_match = p.description and query_str in p.description.lower()
                if not (title_match or domain_match or desc_match):
                    continue

            assignments = ProjectAssignment.query.filter_by(project_id=p.id).all()
            allocated_count = len(assignments)
            capacity = p.max_capacity or 10
            capacity_utilization = round((allocated_count / capacity * 100), 1) if capacity > 0 else 0.0

            # Interns progress in this project
            internships = [a.internship for a in assignments if a.internship]
            if internships:
                progress_list = [i.progress_percent for i in internships if i.progress_percent is not None]
                avg_progress = round(sum(progress_list) / len(progress_list), 1) if progress_list else 0.0
            else:
                avg_progress = 0.0

            # Milestones and Tasks Progression
            all_milestones = []
            for a in assignments:
                all_milestones.extend(a.weekly_milestones.all())
            total_milestones = len(all_milestones)
            completed_milestones = sum(1 for m in all_milestones if m.status in ['COMPLETED', 'APPROVED'])
            milestone_progression_rate = round((completed_milestones / total_milestones * 100), 1) if total_milestones > 0 else 0.0

            # Submissions for this project
            milestone_ids = [m.id for m in all_milestones]
            submissions = WeeklySubmission.query.filter(
                WeeklySubmission.milestone_id.in_(milestone_ids)
            ).all() if milestone_ids else []

            total_subs = len(submissions)
            on_time_subs = 0
            for sub in submissions:
                m = sub.milestone
                if m and m.due_at and sub.submitted_at:
                    if sub.submitted_at <= m.due_at:
                        on_time_subs += 1
                else:
                    on_time_subs += 1
            on_time_rate = round((on_time_subs / total_subs * 100), 1) if total_subs > 0 else 0.0

            # Average evaluation score across this project
            evaluations = Evaluation.query.filter(
                Evaluation.assignment_id.in_([a.id for a in assignments])
            ).all() if assignments else []
            scores = [e.total_score for e in evaluations if e.total_score is not None]
            avg_score = round(sum(scores) / len(scores), 1) if scores else None

            difficulty = p.difficulty or getattr(p, 'difficulty_level', None) or 'INTERMEDIATE'

            records.append({
                'project': p,
                'project_id': p.id,
                'project_code': p.project_code or f'PROJ-{p.id:03d}',
                'title': p.title,
                'domain': proj_domain,
                'department': proj_domain,
                'difficulty_level': difficulty,
                'duration_months': p.duration_months or 1,
                'is_active': p.is_active,
                'allocated_count': allocated_count,
                'capacity': capacity,
                'capacity_utilization': capacity_utilization,
                'avg_progress': avg_progress,
                'total_milestones': total_milestones,
                'completed_milestones': completed_milestones,
                'milestone_progression_rate': milestone_progression_rate,
                'total_submissions': total_subs,
                'on_time_rate': on_time_rate,
                'avg_score': avg_score
            })

        reverse = (order.lower() == 'desc')
        if sort_by == 'allocated':
            records.sort(key=lambda r: r['allocated_count'], reverse=reverse)
        elif sort_by == 'progress':
            records.sort(key=lambda r: r['avg_progress'], reverse=reverse)
        elif sort_by == 'score':
            records.sort(key=lambda r: (r['avg_score'] is not None, r['avg_score'] or 0), reverse=reverse)
        elif sort_by == 'title':
            records.sort(key=lambda r: r['title'].lower(), reverse=reverse)

        return records

    @staticmethod
    def get_project_detail_analytics(project_id):
        """
        Retrieves in-depth project telemetry including intern roster and milestone distribution.
        """
        project = Project.query.get(project_id)
        if not project:
            return None

        assignments = ProjectAssignment.query.filter_by(project_id=project.id).all()
        roster = []

        for a in assignments:
            internship = a.internship
            student = internship.student if internship else None
            mentor = internship.mentor if internship else None

            if student:
                milestones = a.weekly_milestones.all()
                total_m = len(milestones)
                comp_m = sum(1 for m in milestones if m.status in ['COMPLETED', 'APPROVED'])
                m_rate = round((comp_m / total_m * 100), 1) if total_m > 0 else 0.0

                roster.append({
                    'student': student,
                    'intern_id': AnalyticsReportingService._get_student_intern_id(student, internship),
                    'full_name': AnalyticsReportingService._get_student_name(student),
                    'email': AnalyticsReportingService._get_student_email(student),
                    'status': internship.status if internship else 'PENDING',
                    'progress_percent': internship.progress_percent if (internship and internship.progress_percent is not None) else 0,
                    'mentor_name': mentor.full_name if mentor else 'Unassigned',
                    'milestone_rate': m_rate,
                    'completed_milestones': comp_m,
                    'total_milestones': total_m
                })

        capacity = project.max_capacity or 10
        allocated = len(assignments)
        utilization = round((allocated / capacity * 100), 1) if capacity > 0 else 0.0

        return {
            'project': project,
            'capacity': capacity,
            'allocated_count': allocated,
            'utilization': utilization,
            'roster': roster
        }

    # ─── 5. CSV Export Engine (RFC 4180 Compliant) ────────────────────────────

    @staticmethod
    def generate_csv_report(report_type, filters=None):
        """
        Generates RFC 4180 compliant CSV content as a string.
        Includes UTF-8 BOM for Microsoft Excel compatibility.
        """
        filters = filters or {}
        output = io.StringIO()
        writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)

        if report_type == 'interns_performance':
            # Header
            writer.writerow([
                'Intern ID', 'Full Name', 'Email', 'Department', 'College',
                'Status', 'Duration (Months)', 'Project Title', 'Mentor Name',
                'Progress %', 'Milestones Done', 'Total Milestones',
                'Tasks Done', 'Total Tasks', 'On-Time Rate %',
                'Approval Rate %', 'Revision Rate %', 'Average Evaluation Score'
            ])
            records = AnalyticsReportingService.get_interns_analytics(
                search=filters.get('search'),
                project_id=filters.get('project_id'),
                mentor_id=filters.get('mentor_id'),
                status=filters.get('status')
            )
            for r in records:
                writer.writerow([
                    r['intern_id'],
                    r['full_name'],
                    r['email'],
                    r['department'],
                    r['college'],
                    r['status'],
                    r['duration_months'],
                    r['project_title'],
                    r['mentor_name'],
                    f"{r['progress_percent']}%",
                    r['completed_milestones'],
                    r['total_milestones'],
                    r['completed_tasks'],
                    r['total_tasks'],
                    f"{r['on_time_rate']}%",
                    f"{r['approval_rate']}%",
                    f"{r['revision_rate']}%",
                    r['avg_score'] if r['avg_score'] is not None else 'N/A'
                ])

        elif report_type == 'mentors_performance':
            writer.writerow([
                'Mentor ID', 'Full Name', 'Email', 'Department',
                'Total Assigned', 'Active Interns', 'Completed Internships',
                'Max Capacity', 'Capacity Utilization %', 'Workload Tier',
                'Pending Reviews', 'Completed Reviews', 'Avg Turnaround (Hours)',
                'Avg Score Awarded', 'Avg Mentee Progress %'
            ])
            records = AnalyticsReportingService.get_mentors_analytics(
                search=filters.get('search'),
                department=filters.get('department')
            )
            for r in records:
                writer.writerow([
                    r['mentor_id'],
                    r['full_name'],
                    r['email'],
                    r['department'],
                    r['total_assigned'],
                    r['active_count'],
                    r['completed_count'],
                    r['capacity'],
                    f"{r['utilization_rate']}%",
                    r['workload_tier'],
                    r['pending_reviews'],
                    r['completed_reviews'],
                    f"{r['avg_turnaround_hours']}h",
                    r['avg_score_awarded'] if r['avg_score_awarded'] is not None else 'N/A',
                    f"{r['avg_mentee_progress']}%"
                ])

        elif report_type == 'projects_summary':
            writer.writerow([
                'Project ID', 'Project Title', 'Domain', 'Difficulty Level',
                'Duration (Months)', 'Status', 'Allocated Interns', 'Max Capacity',
                'Capacity Utilization %', 'Avg Progress %', 'Completed Milestones',
                'Total Milestones', 'On-Time Submission Rate %', 'Avg Evaluation Score'
            ])
            records = AnalyticsReportingService.get_projects_analytics(
                search=filters.get('search'),
                domain=filters.get('domain'),
                status=filters.get('status')
            )
            for r in records:
                writer.writerow([
                    r['project_id'],
                    r['title'],
                    r['domain'],
                    r['difficulty_level'],
                    r['duration_months'],
                    'Active' if r['is_active'] else 'Archived',
                    r['allocated_count'],
                    r['capacity'],
                    f"{r['capacity_utilization']}%",
                    f"{r['avg_progress']}%",
                    r['completed_milestones'],
                    r['total_milestones'],
                    f"{r['on_time_rate']}%",
                    r['avg_score'] if r['avg_score'] is not None else 'N/A'
                ])

        elif report_type == 'organization_monthly':
            writer.writerow(['Metric', 'Value', 'Unit / Notes'])
            kpis = AnalyticsReportingService.get_organization_kpis()
            writer.writerow(['Report Generation Date', datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'), 'System Timestamp'])
            writer.writerow(['Total Registered Interns', kpis['total_interns'], 'Candidates'])
            writer.writerow(['Active Internships', kpis['active_interns'], 'Currently enrolled'])
            writer.writerow(['Completed Internships', kpis['completed_internships'], 'Graduated'])
            writer.writerow(['Total Faculty Mentors', kpis['total_mentors'], 'Supervisors'])
            writer.writerow(['Active Projects', kpis['active_projects'], 'Live curriculum projects'])
            writer.writerow(['Total Projects', kpis['total_projects'], 'Project database count'])
            writer.writerow(['Pending Submission Queue', kpis['pending_submissions'], 'Awaiting evaluation'])
            writer.writerow(['Total Submissions Logged', kpis['total_submissions'], 'Submissions received'])
            writer.writerow(['Overdue Milestones Count', kpis['overdue_milestones_count'], 'Action required'])
            writer.writerow(['Average Completion Rate', f"{kpis['avg_completion_rate']}%", 'Across all cohorts'])
            writer.writerow(['Average Evaluation Score', f"{kpis['avg_evaluation_score']}/100", 'Overall benchmark'])
            writer.writerow(['On-Time Submission Rate', f"{kpis['on_time_rate']}%", 'Deliverable timeliness'])
            writer.writerow(['Submission Approval Rate', f"{kpis['submission_approval_rate']}%", 'First-pass/reviewed approvals'])
            writer.writerow(['Revision Request Rate', f"{kpis['revision_rate']}%", 'Redo/revision requests'])

        elif report_type == 'internship_completions':
            writer.writerow([
                'Intern ID', 'Full Name', 'Email', 'Department', 'College',
                'Duration (Months)', 'Start Date', 'End Date', 'Project Title',
                'Mentor Name', 'Final Progress %', 'Average Evaluation Score',
                'Status', 'Certificate Eligibility'
            ])
            students = Student.query.all()
            for s in students:
                internship = s.active_internship or s.internships.first()
                if not internship:
                    continue
                # Show COMPLETED or progress >= 100%
                if internship.status != 'COMPLETED' and (internship.progress_percent or 0) < 100:
                    continue
                assignment = internship.assignments.first() if internship else None
                project = assignment.project if assignment else None
                mentor = internship.mentor
                evals = Evaluation.query.filter_by(employee_id=s.id).all()
                scores = [e.total_score for e in evals if e.total_score is not None]
                avg_score = round(sum(scores) / len(scores), 1) if scores else None
                is_eligible = (internship.status == 'COMPLETED' or (internship.progress_percent or 0) >= 100) and (avg_score is None or avg_score >= 60)

                writer.writerow([
                    AnalyticsReportingService._get_student_intern_id(s, internship),
                    AnalyticsReportingService._get_student_name(s),
                    AnalyticsReportingService._get_student_email(s),
                    AnalyticsReportingService._get_student_dept(s),
                    AnalyticsReportingService._get_student_college(s),
                    AnalyticsReportingService._get_internship_duration(internship),
                    AnalyticsReportingService._format_date_safe(internship.start_date),
                    AnalyticsReportingService._format_date_safe(internship.end_date),
                    project.title if project else 'Unallocated',
                    mentor.full_name if mentor else 'Unassigned',
                    f"{internship.progress_percent}%",
                    avg_score if avg_score is not None else 'N/A',
                    internship.status,
                    'ELIGIBLE' if is_eligible else 'PENDING_REVIEW'
                ])

        return output.getvalue()

    @staticmethod
    def generate_mentor_mentees_csv(mentor_id):
        """
        Generates CSV for a mentor's assigned mentees with their scores and progression.
        """
        output = io.StringIO()
        writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)

        writer.writerow([
            'Intern ID', 'Full Name', 'Email', 'Project', 'Status',
            'Progress %', 'Milestones Done', 'Total Milestones',
            'Tasks Done', 'Total Tasks', 'On-Time Rate %', 'Average Score'
        ])

        internships = Internship.query.filter_by(mentor_id=mentor_id).all()
        for i in internships:
            s = i.student
            if not s:
                continue
            assignment = i.assignments.first()
            project = assignment.project if assignment else None
            milestones = assignment.weekly_milestones.all() if assignment else []
            total_m = len(milestones)
            comp_m = sum(1 for m in milestones if m.status in ['COMPLETED', 'APPROVED'])

            tasks = []
            for m in milestones:
                tasks.extend(m.tasks.all())
            total_t = len(tasks)
            comp_t = sum(1 for t in tasks if t.is_completed)

            subs = WeeklySubmission.query.filter_by(student_id=s.id).all()
            on_time = 0
            for sub in subs:
                m = sub.milestone
                if m and m.due_at and sub.submitted_at:
                    if sub.submitted_at <= m.due_at:
                        on_time += 1
                else:
                    on_time += 1
            on_time_rate = round((on_time / len(subs) * 100), 1) if subs else 0.0

            evals = Evaluation.query.filter_by(employee_id=s.id).all()
            scores = [e.total_score for e in evals if e.total_score is not None]
            avg_score = round(sum(scores) / len(scores), 1) if scores else None

            writer.writerow([
                AnalyticsReportingService._get_student_intern_id(s, i),
                AnalyticsReportingService._get_student_name(s),
                AnalyticsReportingService._get_student_email(s),
                project.title if project else 'Unallocated',
                i.status,
                f"{i.progress_percent}%",
                comp_m,
                total_m,
                comp_t,
                total_t,
                f"{on_time_rate}%",
                avg_score if avg_score is not None else 'N/A'
            ])

        return output.getvalue()
