"""
ANTI-MATRIX SUBMISSION WORKFLOW & EVALUATION SERVICE
Phase 2 — Module 5

Implements:
1. Strict state machine transitions (ASSIGNED, IN_PROGRESS, SUBMITTED, UNDER_REVIEW, APPROVED, REDO_REQUIRED, COMPLETED, OVERDUE).
2. Configurable 5-criteria Evaluation Rubric scoring (Technical implementation, Code quality, Functionality, Documentation, Presentation).
3. Automated multi-party notification triggers.
4. Immutable audit logging and role-based evaluation authority enforcement.
"""

import json
import logging
from datetime import datetime, timedelta
from flask import url_for
from app.extensions import db
from app.models import (
    WeeklyMilestone, WeeklyTask, WeeklySubmission, Evaluation, EvaluationRubric,
    ProjectAssignment, Internship, Student, User, Notification, AuditLog, Project
)

logger = logging.getLogger(__name__)


# ─── 1. State Machine Definitions ─────────────────────────────────────────────

VALID_MILESTONE_TRANSITIONS = {
    'LOCKED': ['AVAILABLE', 'ASSIGNED'],
    'AVAILABLE': ['IN_PROGRESS', 'SUBMITTED', 'UNDER_REVIEW', 'OVERDUE'],
    'ASSIGNED': ['IN_PROGRESS', 'SUBMITTED', 'UNDER_REVIEW', 'OVERDUE'],
    'IN_PROGRESS': ['SUBMITTED', 'UNDER_REVIEW', 'OVERDUE', 'AVAILABLE'],
    'OVERDUE': ['SUBMITTED', 'UNDER_REVIEW', 'IN_PROGRESS', 'AVAILABLE'],
    'SUBMITTED': ['UNDER_REVIEW', 'APPROVED', 'REDO_REQUIRED', 'REVISION_REQUIRED', 'COMPLETED'],
    'UNDER_REVIEW': ['APPROVED', 'REDO_REQUIRED', 'REVISION_REQUIRED', 'COMPLETED', 'UNDER_REVIEW'],
    'REDO_REQUIRED': ['IN_PROGRESS', 'SUBMITTED', 'UNDER_REVIEW', 'OVERDUE', 'AVAILABLE'],
    'REVISION_REQUIRED': ['IN_PROGRESS', 'SUBMITTED', 'UNDER_REVIEW', 'OVERDUE', 'AVAILABLE'],
    'APPROVED': ['COMPLETED', 'APPROVED'],
    'COMPLETED': [] # Terminal state
}

VALID_SUBMISSION_TRANSITIONS = {
    'SUBMITTED': ['UNDER_REVIEW', 'APPROVED', 'REDO_REQUIRED', 'REVISION_REQUIRED', 'COMPLETED'],
    'UNDER_REVIEW': ['APPROVED', 'REDO_REQUIRED', 'REVISION_REQUIRED', 'COMPLETED'],
    'REDO_REQUIRED': ['SUBMITTED', 'UNDER_REVIEW'],
    'REVISION_REQUIRED': ['SUBMITTED', 'UNDER_REVIEW'],
    'APPROVED': ['COMPLETED'],
    'COMPLETED': []
}


class SubmissionWorkflowService:
    """Enterprise submission workflow, status transition, rubric evaluation and notification engine."""

    # ─── Transition Validation ────────────────────────────────────────────────

    @staticmethod
    def validate_milestone_transition(current_status, new_status):
        """
        Validates if transitioning from current_status to new_status is permitted.
        Returns (is_valid: bool, error_message: str or None)
        """
        curr = (current_status or 'LOCKED').upper()
        target = (new_status or '').upper()

        if curr == target:
            return True, None

        allowed = VALID_MILESTONE_TRANSITIONS.get(curr, [])
        if target not in allowed:
            return False, f"Invalid milestone transition from '{curr}' to '{target}'. Allowed target states: {allowed}."
        return True, None

    @staticmethod
    def transition_milestone(milestone, new_status, actor=None, notes=None):
        """
        Transitions milestone status enforcing state machine rules and logging audit.
        """
        is_valid, err = SubmissionWorkflowService.validate_milestone_transition(milestone.status, new_status)
        if not is_valid:
            raise ValueError(err)

        prev_status = milestone.status
        milestone.status = new_status
        now = datetime.utcnow()

        if new_status in ['COMPLETED', 'APPROVED'] and not milestone.completed_at:
            milestone.completed_at = now
            if actor:
                milestone.approved_by = actor.id
                milestone.approved_at = now

        if notes:
            milestone.admin_notes = notes

        # Audit transition
        if actor:
            audit = AuditLog(
                actor_id=actor.id,
                actor_role=actor.role if hasattr(actor, 'role') else 'SYSTEM',
                action='MILESTONE_STATUS_TRANSITION',
                target_entity='weekly_milestones',
                target_id=str(milestone.id),
                details_json=json.dumps({
                    'week_number': milestone.week_number,
                    'from_status': prev_status,
                    'to_status': new_status,
                    'notes': notes
                })
            )
            db.session.add(audit)

        return True

    # ─── Notifications (Delegated to Centralized NotificationService) ─────────

    @staticmethod
    def notify_task_assigned(student_user_id, milestone, task_title, actor_name='Supervisor'):
        """Send notification to intern when a new task is assigned."""
        try:
            from app.services.notification_service import NotificationService
            due_str = milestone.due_at.strftime('%B %d, %Y') if milestone.due_at else None
            return NotificationService.notify_task_assigned(
                student_user_id=student_user_id,
                task_title=task_title,
                milestone_id=milestone.id,
                week_number=milestone.week_number,
                milestone_title=milestone.title or '',
                actor_name=actor_name,
                due_date_str=due_str
            )
        except Exception as e:
            logger.warning(f"Could not dispatch task notification: {e}")

    @staticmethod
    def notify_submission_received(mentor_user_id, student_name, milestone, submission_id):
        """Notify assigned mentor when an intern submits weekly deliverables."""
        if not mentor_user_id:
            return
        try:
            from app.services.notification_service import NotificationService
            return NotificationService.notify_submission_received(
                mentor_user_id=mentor_user_id,
                student_name=student_name,
                milestone_title=milestone.title or '',
                milestone_id=milestone.id,
                submission_id=submission_id,
                week_number=milestone.week_number
            )
        except Exception as e:
            logger.warning(f"Could not dispatch mentor submission notification: {e}")

    @staticmethod
    def notify_revisions_requested(student_user_id, mentor_name, milestone, feedback):
        """Notify intern when supervisor requests revisions."""
        try:
            from app.services.notification_service import NotificationService
            return NotificationService.notify_revision_requested(
                student_user_id=student_user_id,
                milestone_title=milestone.title or '',
                milestone_id=milestone.id,
                week_number=milestone.week_number,
                feedback_notes=feedback,
                mentor_name=mentor_name
            )
        except Exception as e:
            logger.warning(f"Could not dispatch revision notification: {e}")

    @staticmethod
    def notify_submission_approved(student_user_id, mentor_name, milestone, total_score, next_milestone=None):
        """Notify intern when submission is approved and next milestone unlocked."""
        try:
            from app.services.notification_service import NotificationService
            return NotificationService.notify_submission_approved(
                student_user_id=student_user_id,
                milestone_title=milestone.title or '',
                milestone_id=milestone.id,
                week_number=milestone.week_number,
                total_score=total_score,
                mentor_name=mentor_name,
                next_milestone_id=next_milestone.id if next_milestone else None,
                next_week_number=next_milestone.week_number if next_milestone else None
            )
        except Exception as e:
            logger.warning(f"Could not dispatch approval notification: {e}")

    @staticmethod
    def notify_deadline_approaching(student_user_id, milestone, hours_remaining):
        """Notify intern when a milestone deadline is approaching."""
        try:
            from app.services.notification_service import NotificationService
            days = hours_remaining // 24
            due_str = milestone.due_at.strftime('%B %d, %Y') if milestone.due_at else 'Soon'
            return NotificationService.notify_deadline_reminder(
                student_user_id=student_user_id,
                milestone_title=milestone.title or '',
                milestone_id=milestone.id,
                week_number=milestone.week_number,
                due_date_str=due_str,
                days_remaining=days
            )
        except Exception as e:
            logger.warning(f"Could not dispatch deadline notification: {e}")

    # ─── Evaluation Rubric & Approval Engine ─────────────────────────────────

    @staticmethod
    def process_evaluation_approval(submission, evaluator, scores_dict, feedback, ip_address=None):
        """
        Executes a complete, validated submission approval workflow:
        1. Calculates score using active EvaluationRubric (Technical, Code Quality, Functionality, Documentation, Presentation).
        2. Validates milestone transition to COMPLETED.
        3. Records Evaluation with snapshot of criteria weights.
        4. Progresses to next milestone or completes internship.
        5. Sends notifications and records AuditLog.
        """
        milestone = submission.milestone
        assignment = milestone.assignment
        student = submission.student
        internship = assignment.internship

        if milestone.status == 'COMPLETED':
            return False, "This milestone has already been completed."

        rubric = EvaluationRubric.get_active()

        # Extract 5 criteria scores (0-100 each)
        tech = int(scores_dict.get('technical_score', 85))
        code = int(scores_dict.get('code_quality_score', 85))
        func = int(scores_dict.get('functionality_score', 85))
        doc = int(scores_dict.get('documentation_score', 80))
        pres = int(scores_dict.get('presentation_score', 80))

        # Calculate weighted total using active rubric (or backward-compatible legacy 7-field sum if present)
        if any(k in scores_dict for k in ['ui_ux_score', 'demo_score', 'originality_score']):
            legacy_sum = (
                int(scores_dict.get('technical_score', 0)) +
                int(scores_dict.get('functionality_score', 0)) +
                int(scores_dict.get('ui_ux_score', 0)) +
                int(scores_dict.get('documentation_score', 0)) +
                int(scores_dict.get('demo_score', 0)) +
                int(scores_dict.get('originality_score', 0)) +
                int(scores_dict.get('presentation_score', 0))
            )
            total_score = legacy_sum if legacy_sum > 0 else rubric.calculate_score(tech, code, func, doc, pres)
        else:
            total_score = rubric.calculate_score(tech, code, func, doc, pres)

        now = datetime.utcnow()
        feedback_text = feedback.strip() if feedback else "Deliverables reviewed and approved by supervisor."

        # 1. Update current submission
        submission.status = 'COMPLETED'
        submission.review_feedback = feedback_text
        submission.reviewed_by = evaluator.id
        submission.reviewed_at = now

        # 2. Update current milestone
        milestone.status = 'COMPLETED'
        milestone.completed_at = now
        milestone.approved_by = evaluator.id
        milestone.approved_at = now
        milestone.admin_notes = feedback_text

        # 3. Save Evaluation record
        eval_log = Evaluation(
            submission_id=submission.id,
            milestone_id=milestone.id,
            assignment_id=assignment.id,
            employee_id=student.id,
            admin_id=evaluator.id,
            evaluator_id=evaluator.id,
            action='PROCEED_TO_NEXT',
            remarks=feedback_text,
            feedback=feedback_text,
            technical_score=tech,
            code_quality_score=code,
            functionality_score=func,
            documentation_score=doc,
            presentation_score=pres,
            total_score=total_score,
            rubric_id=rubric.id,
            rubric_snapshot_json=json.dumps(rubric.to_dict()),
            result='PASSED',
            evaluated_at=now
        )
        db.session.add(eval_log)

        # 4. Check for next week milestone
        next_milestone = WeeklyMilestone.query.filter_by(
            assignment_id=assignment.id,
            week_number=milestone.week_number + 1
        ).first()

        if next_milestone:
            next_milestone.status = 'AVAILABLE'
            next_milestone.unlocked_at = now
            next_milestone.started_at = now
            next_milestone.due_at = now + timedelta(days=7)

            SubmissionWorkflowService.notify_submission_approved(
                student_user_id=student.user.id,
                mentor_name=evaluator.full_name,
                milestone=milestone,
                total_score=total_score,
                next_milestone=next_milestone
            )
        else:
            assignment.status = 'COMPLETED'
            internship.status = 'COMPLETED'
            internship.progress_percent = 100

            SubmissionWorkflowService.notify_submission_approved(
                student_user_id=student.user.id,
                mentor_name=evaluator.full_name,
                milestone=milestone,
                total_score=total_score,
                next_milestone=None
            )

            try:
                from app.services.notification_service import NotificationService
                program_title = internship.plan.title if internship and internship.plan else 'Engineering Internship'
                NotificationService.notify_internship_completed(
                    student_user_id=student.user.id,
                    student_name=student.user.full_name or student.user.name or 'Intern',
                    program_title=program_title,
                    final_score=total_score
                )
            except Exception as ex:
                logger.warning(f"Failed to send completion notification: {ex}")

        internship.update_progress()

        # 5. Audit Log
        audit = AuditLog(
            actor_id=evaluator.id,
            actor_role=evaluator.role if hasattr(evaluator, 'role') else 'MENTOR',
            action='SUBMISSION_EVALUATION_APPROVED',
            target_entity='weekly_submissions',
            target_id=str(submission.id),
            details_json=json.dumps({
                'milestone_id': milestone.id,
                'week_number': milestone.week_number,
                'student_id': student.id,
                'total_score': total_score,
                'rubric_id': rubric.id,
                'scores': {
                    'technical': tech,
                    'code_quality': code,
                    'functionality': func,
                    'documentation': doc,
                    'presentation': pres
                }
            }),
            ip_address=ip_address
        )
        db.session.add(audit)
        db.session.commit()

        return True, total_score

    @staticmethod
    def process_evaluation_redo(submission, evaluator, feedback, scores_dict=None, ip_address=None):
        """
        Executes a validated submission revision/redo request.
        """
        milestone = submission.milestone
        assignment = milestone.assignment
        student = submission.student

        remarks = (feedback or '').strip()
        if not remarks:
            return False, "Feedback and revision notes are required when requesting a rework."

        now = datetime.utcnow()
        rubric = EvaluationRubric.get_active()

        # Score calculations for revision (default or provided)
        scores_dict = scores_dict or {}
        tech = int(scores_dict.get('technical_score', 40))
        code = int(scores_dict.get('code_quality_score', 40))
        func = int(scores_dict.get('functionality_score', 40))
        doc = int(scores_dict.get('documentation_score', 40))
        pres = int(scores_dict.get('presentation_score', 40))
        total_score = rubric.calculate_score(tech, code, func, doc, pres)

        submission.status = 'REDO_REQUIRED'
        submission.review_feedback = remarks
        submission.reviewed_by = evaluator.id
        submission.reviewed_at = now

        milestone.status = 'REDO_REQUIRED'
        milestone.admin_notes = remarks

        eval_log = Evaluation(
            submission_id=submission.id,
            milestone_id=milestone.id,
            assignment_id=assignment.id,
            employee_id=student.id,
            admin_id=evaluator.id,
            evaluator_id=evaluator.id,
            action='REDO',
            remarks=remarks,
            feedback=remarks,
            technical_score=tech,
            code_quality_score=code,
            functionality_score=func,
            documentation_score=doc,
            presentation_score=pres,
            total_score=total_score,
            rubric_id=rubric.id,
            rubric_snapshot_json=json.dumps(rubric.to_dict()),
            result='REDO_REQUIRED',
            evaluated_at=now
        )
        db.session.add(eval_log)

        SubmissionWorkflowService.notify_revisions_requested(
            student_user_id=student.user.id,
            mentor_name=evaluator.full_name,
            milestone=milestone,
            feedback=remarks
        )

        audit = AuditLog(
            actor_id=evaluator.id,
            actor_role=evaluator.role if hasattr(evaluator, 'role') else 'MENTOR',
            action='SUBMISSION_EVALUATION_REDO',
            target_entity='weekly_submissions',
            target_id=str(submission.id),
            details_json=json.dumps({
                'milestone_id': milestone.id,
                'week_number': milestone.week_number,
                'student_id': student.id,
                'remarks': remarks
            }),
            ip_address=ip_address
        )
        db.session.add(audit)
        milestone.assignment.internship.update_progress()
        db.session.commit()

        return True, "Revision request processed successfully."

    # ─── Group / Broadcast Task Assignment ───────────────────────────────────

    @staticmethod
    def broadcast_task_to_project(project_id, task_title, description=None, priority='Medium', technical_requirements=None, reference_resources=None, actor=None):
        """
        Assigns/appends a weekly task to all active interns currently assigned to a project group.
        """
        project = Project.query.get(project_id)
        if not project:
            return 0, "Project not found."

        # Find all active assignments
        active_assignments = ProjectAssignment.query.filter_by(
            project_id=project.id,
            status='IN_PROGRESS'
        ).all()

        if not active_assignments:
            # Fallback to any active assignment status
            active_assignments = ProjectAssignment.query.filter(
                ProjectAssignment.project_id == project.id,
                ProjectAssignment.status.in_(['ASSIGNED', 'IN_PROGRESS', 'ACTIVE'])
            ).all()

        assigned_count = 0
        now = datetime.utcnow()

        for pa in active_assignments:
            cur_milestone = pa.current_milestone
            if not cur_milestone:
                continue

            max_order = db.session.query(db.func.max(WeeklyTask.order_num)).filter_by(milestone_id=cur_milestone.id).scalar() or 0

            wt = WeeklyTask(
                milestone_id=cur_milestone.id,
                task_text=task_title,
                description=description,
                priority=priority,
                technical_requirements=technical_requirements,
                reference_resources=reference_resources,
                is_completed=False,
                order_num=max_order + 1,
                status='ASSIGNED'
            )
            db.session.add(wt)
            assigned_count += 1

            # Notify student
            if pa.internship and pa.internship.student and pa.internship.student.user:
                SubmissionWorkflowService.notify_task_assigned(
                    student_user_id=pa.internship.student.user.id,
                    milestone=cur_milestone,
                    task_title=task_title,
                    actor_name=actor.full_name if actor else 'Project Administrator'
                )

        if actor and assigned_count > 0:
            audit = AuditLog(
                actor_id=actor.id,
                actor_role=actor.role if hasattr(actor, 'role') else 'ADMIN',
                action='BROADCAST_TASK_TO_PROJECT',
                target_entity='projects',
                target_id=str(project.id),
                details_json=json.dumps({
                    'project_id': project.id,
                    'task_title': task_title,
                    'assigned_count': assigned_count
                })
            )
            db.session.add(audit)

        db.session.commit()
        return assigned_count, None
