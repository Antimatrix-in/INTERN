import os
import re
import json
import smtplib
import logging
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import render_template, current_app, url_for
from app.extensions import db
from app.models import (
    User, Student, Internship, Project, WeeklyMilestone, WeeklyTask,
    WeeklySubmission, Notification, NotificationPreference, Announcement,
    AnnouncementRead, AuditLog
)

logger = logging.getLogger('anti_matrix.notifications')


class NotificationService:
    """
    Centralized Notification and Communication Service for Anti-Matrix Portal (Module 7).
    Handles in-portal alerts, branded responsive email dispatch, automated deadline reminders,
    announcements broadcasting, user preferences, and audit logging.
    """

    # In-memory test outbox for verifying email generation without live SMTP
    email_outbox = []

    # ─── User Preferences Management ──────────────────────────────────────────

    @classmethod
    def get_or_create_preferences(cls, user_id: int) -> NotificationPreference:
        """
        Retrieves notification preferences for user, creating defaults if not yet present.
        """
        pref = NotificationPreference.query.filter_by(user_id=user_id).first()
        if not pref:
            pref = NotificationPreference(
                user_id=user_id,
                email_enabled=True,
                in_portal_enabled=True,
                deadline_reminders=True,
                announcements=True,
                evaluations=True
            )
            try:
                db.session.add(pref)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                logger.warning(f"Failed to create default preferences for user {user_id}: {e}")
                # Re-query in case of race condition
                pref = NotificationPreference.query.filter_by(user_id=user_id).first()
        return pref

    @classmethod
    def update_preferences(cls, user_id: int, **kwargs) -> NotificationPreference:
        """
        Updates notification preference flags for a user.
        """
        pref = cls.get_or_create_preferences(user_id)
        for key in ['email_enabled', 'in_portal_enabled', 'deadline_reminders', 'announcements', 'evaluations']:
            if key in kwargs:
                setattr(pref, key, bool(kwargs[key]))
        pref.updated_at = datetime.utcnow()
        db.session.commit()
        return pref

    # ─── Core Notification Dispatcher ─────────────────────────────────────────

    @classmethod
    def send_notification(cls, user_id: int, title: str, message: str,
                          notif_type: str = 'INFO', link: str = None,
                          category: str = 'GENERAL', email_template: str = None,
                          email_context: dict = None, force_portal: bool = False,
                          force_email: bool = False) -> Notification or None:
        """
        Dispatches in-portal notification and optional branded email while enforcing
        user communication preferences, error isolation, and audit logging.
        """
        user = User.query.get(user_id)
        if not user:
            logger.warning(f"Notification recipient user {user_id} not found.")
            return None

        pref = cls.get_or_create_preferences(user_id)
        notif = None

        # 1. In-Portal Notification
        in_portal_allowed = force_portal or category == 'SECURITY' or (pref and pref.in_portal_enabled)
        if in_portal_allowed:
            try:
                notif = Notification(
                    user_id=user_id,
                    title=title,
                    message=message,
                    type=notif_type,
                    link=link,
                    category=category,
                    is_read=False,
                    created_at=datetime.utcnow()
                )
                db.session.add(notif)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                logger.error(f"Failed to save in-portal notification for user {user_id}: {e}")

        # 2. Email Notification
        email_allowed = force_email or category == 'SECURITY' or (pref and pref.email_enabled)
        # Check specific category preference
        if pref and not force_email and category != 'SECURITY':
            if category == 'DEADLINE' and not pref.deadline_reminders:
                email_allowed = False
            elif category == 'ANNOUNCEMENT' and not pref.announcements:
                email_allowed = False
            elif category in ['EVALUATION', 'SUBMISSION'] and not pref.evaluations:
                email_allowed = False

        if email_allowed and user.email:
            try:
                ctx = email_context.copy() if email_context else {}
                if 'recipient_name' not in ctx:
                    ctx['recipient_name'] = user.full_name or user.name or 'Team Member'
                if 'recipient_email' not in ctx:
                    ctx['recipient_email'] = user.email
                if 'action_url' not in ctx and link:
                    base_url = current_app.config.get('BASE_URL', 'http://localhost:5050').rstrip('/')
                    clean_link = link if link.startswith('http') else f"{base_url}{link}"
                    ctx['action_url'] = clean_link
                if 'year' not in ctx:
                    ctx['year'] = datetime.utcnow().year

                cls.send_email(
                    recipient_email=user.email,
                    subject=title,
                    template_name=email_template,
                    context=ctx,
                    plain_text_message=message
                )
            except Exception as e:
                logger.warning(f"Email dispatch failed for {user.email}: {e}")

        return notif

    @classmethod
    def create_in_portal_notification(cls, user_id: int, title: str, message: str,
                                      notif_type: str = 'INFO', link: str = None,
                                      category: str = 'GENERAL', **kwargs):
        """Convenience method for creating in-portal notifications."""
        return cls.send_notification(
            user_id=user_id,
            title=title,
            message=message,
            notif_type=notif_type,
            link=link,
            category=category,
            force_portal=True,
            **kwargs
        )

    # ─── Email Engine & Safe SMTP Sender ──────────────────────────────────────

    @classmethod
    def send_email(cls, recipient_email: str, subject: str, template_name: str = None,
                   context: dict = None, plain_text_message: str = None) -> bool:
        """
        Sends branded HTML email with plain-text fallback using standard library smtplib.
        Gracefully handles network drops, mock outbox recording, and configuration gaps.
        """
        if not recipient_email:
            return False

        context = context or {}
        context.setdefault('base_url', current_app.config.get('BASE_URL', 'http://localhost:5050').rstrip('/'))
        context.setdefault('subject', subject)

        html_body = None
        if template_name:
            template_path = f"emails/{template_name}.html" if not template_name.endswith('.html') else f"emails/{template_name}"
            try:
                html_body = render_template(template_path, **context)
            except Exception as e:
                logger.warning(f"Could not render email template {template_path}: {e}")

        if not html_body:
            # Fallback inline HTML
            html_body = f"""
            <div style="font-family:sans-serif; background:#F8FAFC; padding:24px;">
                <div style="max-width:560px; margin:auto; background:#FFF; border:1px solid #E2E8F0; border-radius:8px; padding:24px;">
                    <h2 style="color:#030A07; margin-top:0;">{subject}</h2>
                    <p style="color:#334155; line-height:1.6;">{plain_text_message or subject}</p>
                </div>
            </div>
            """

        text_body = plain_text_message or re.sub(r'<[^>]+>', '', html_body)

        # Record in test outbox
        cls.email_outbox.append({
            'to': recipient_email,
            'subject': subject,
            'html': html_body,
            'text': text_body,
            'template': template_name,
            'sent_at': datetime.utcnow().isoformat()
        })

        # Check whether live SMTP sending should occur
        is_testing = current_app.config.get('TESTING', False)
        suppress_send = current_app.config.get('MAIL_SUPPRESS_SEND', False)
        mail_server = current_app.config.get('MAIL_SERVER')
        mail_username = current_app.config.get('MAIL_USERNAME')
        mail_password = current_app.config.get('MAIL_PASSWORD')

        if is_testing or suppress_send or not mail_server or not mail_username:
            logger.debug(f"[OUTBOX MOCK] Email '{subject}' captured for {recipient_email}")
            return True

        # Live SMTP delivery
        try:
            sender = current_app.config.get('MAIL_DEFAULT_SENDER', 'notifications@antimatrix.tech')
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = sender
            msg['To'] = recipient_email

            part1 = MIMEText(text_body, 'plain', 'utf-8')
            part2 = MIMEText(html_body, 'html', 'utf-8')
            msg.attach(part1)
            msg.attach(part2)

            mail_port = current_app.config.get('MAIL_PORT', 587)
            use_ssl = current_app.config.get('MAIL_USE_SSL', False)
            use_tls = current_app.config.get('MAIL_USE_TLS', True)

            if use_ssl:
                server = smtplib.SMTP_SSL(mail_server, mail_port, timeout=10)
            else:
                server = smtplib.SMTP(mail_server, mail_port, timeout=10)
                if use_tls:
                    server.starttls()

            if mail_username and mail_password:
                server.login(mail_username, mail_password)

            server.sendmail(sender, [recipient_email], msg.as_string())
            server.quit()
            logger.info(f"Successfully sent live email '{subject}' to {recipient_email}")
            return True
        except Exception as e:
            logger.warning(f"SMTP error sending email to {recipient_email}: {e}")
            return False

    # ─── 10 Distinct Event Notification Generators ────────────────────────────

    @classmethod
    def notify_task_assigned(cls, student_user_id: int, task_title: str, milestone_id: int,
                             week_number: int, milestone_title: str = '', actor_name: str = 'Supervisor',
                             due_date_str: str = None) -> Notification:
        """1. New task assigned notification."""
        title = f"New Task Assigned — Week {week_number}"
        message = f"{actor_name} assigned a new task: '{task_title}' for Week {week_number} ({milestone_title})."
        link = f"/week/{milestone_id}"
        return cls.send_notification(
            user_id=student_user_id,
            title=title,
            message=message,
            notif_type='INFO',
            link=link,
            category='TASK',
            email_template='task_assigned',
            email_context={
                'week_number': week_number,
                'milestone_title': milestone_title,
                'task_title': task_title,
                'assigned_by': actor_name,
                'due_date_str': due_date_str
            }
        )

    @classmethod
    def notify_project_allocated(cls, student_user_id: int, project_title: str, project_id: int,
                                 tech_stack: str = None, mentor_name: str = None) -> Notification:
        """2. Project allocation notification."""
        title = f"Project Allocated — {project_title}"
        message = f"You have been allocated to the engineering project '{project_title}'."
        link = "/project"
        return cls.send_notification(
            user_id=student_user_id,
            title=title,
            message=message,
            notif_type='SUCCESS',
            link=link,
            category='ALLOCATION',
            email_template='project_allocation',
            email_context={
                'project_title': project_title,
                'project_id': project_id,
                'tech_stack': tech_stack,
                'mentor_name': mentor_name
            }
        )

    @classmethod
    def notify_mentor_assigned(cls, student_user_id: int, mentor_name: str, mentor_email: str = None,
                               mentor_dept: str = None) -> Notification:
        """3. Mentor assignment notification."""
        title = f"Technical Mentor Assigned — {mentor_name}"
        message = f"Technical mentor {mentor_name} has been assigned to guide your internship deliverables."
        link = "/profile"
        return cls.send_notification(
            user_id=student_user_id,
            title=title,
            message=message,
            notif_type='INFO',
            link=link,
            category='ALLOCATION',
            email_template='mentor_assignment',
            email_context={
                'mentor_name': mentor_name,
                'mentor_email': mentor_email,
                'mentor_dept': mentor_dept
            }
        )

    @classmethod
    def notify_deadline_reminder(cls, student_user_id: int, milestone_title: str, milestone_id: int,
                                 week_number: int, due_date_str: str, days_remaining: int,
                                 time_remaining_str: str = None) -> Notification:
        """4. Deadline approaching reminder (T-3d, T-1d, T-0d)."""
        if days_remaining <= 0:
            title = f"Deadline Today: Week {week_number} Deliverables"
            urgency = 'WARNING'
        elif days_remaining == 1:
            title = f"Deadline Tomorrow: Week {week_number} Deliverables"
            urgency = 'WARNING'
        else:
            title = f"Upcoming Deadline: Week {week_number} ({days_remaining} Days Left)"
            urgency = 'INFO'

        time_str = time_remaining_str or (f"{days_remaining} days remaining" if days_remaining > 0 else "Due today!")
        message = f"Reminder: Deliverables for Week {week_number} ({milestone_title}) are due on {due_date_str} ({time_str})."
        link = f"/week/{milestone_id}"

        return cls.send_notification(
            user_id=student_user_id,
            title=title,
            message=message,
            notif_type=urgency,
            link=link,
            category='DEADLINE',
            email_template='deadline_reminder',
            email_context={
                'week_number': week_number,
                'milestone_title': milestone_title,
                'due_date_str': due_date_str,
                'days_remaining': days_remaining,
                'time_remaining_str': time_str,
                'is_overdue': False
            }
        )

    @classmethod
    def notify_overdue_task(cls, student_user_id: int, milestone_title: str, milestone_id: int,
                            week_number: int, due_date_str: str) -> Notification:
        """5. Overdue task alert notification."""
        title = f"Overdue Alert: Week {week_number} Deliverables"
        message = f"The deadline for Week {week_number} ({milestone_title}) was {due_date_str}. Deliverables are now OVERDUE."
        link = f"/week/{milestone_id}"

        return cls.send_notification(
            user_id=student_user_id,
            title=title,
            message=message,
            notif_type='ACTION_REQUIRED',
            link=link,
            category='DEADLINE',
            email_template='deadline_reminder',
            email_context={
                'week_number': week_number,
                'milestone_title': milestone_title,
                'due_date_str': due_date_str,
                'is_overdue': True
            }
        )

    @classmethod
    def notify_submission_received(cls, mentor_user_id: int, student_name: str, milestone_title: str,
                                   milestone_id: int, submission_id: int, week_number: int = 1,
                                   submitted_at_str: str = None) -> Notification or None:
        """6. Submission received notification sent to assigned mentor."""
        if not mentor_user_id:
            return None

        title = f"Deliverables Submitted — {student_name} (Week {week_number})"
        message = f"Intern {student_name} submitted deliverables for Week {week_number} ({milestone_title}). Codebase and video are ready for evaluation."
        link = f"/mentor/review/{submission_id}"

        return cls.send_notification(
            user_id=mentor_user_id,
            title=title,
            message=message,
            notif_type='ACTION_REQUIRED',
            link=link,
            category='SUBMISSION',
            email_template='submission_received',
            email_context={
                'student_name': student_name,
                'week_number': week_number,
                'milestone_title': milestone_title,
                'submitted_at_str': submitted_at_str or datetime.utcnow().strftime('%B %d, %Y %H:%M UTC')
            }
        )

    @classmethod
    def notify_submission_approved(cls, student_user_id: int, milestone_title: str, milestone_id: int,
                                   week_number: int, total_score: float = None, mentor_name: str = 'Mentor',
                                   mentor_feedback: str = None, next_milestone_id: int = None,
                                   next_week_number: int = None) -> Notification:
        """7. Submission approved notification sent to intern."""
        score_text = f" ({total_score}/100)" if total_score is not None else ""
        if next_week_number:
            title = f"Week {week_number} Approved{score_text} — Week {next_week_number} Unlocked!"
            message = f"Supervisor {mentor_name} approved your Week {week_number} deliverables{score_text}. Week {next_week_number} is now active."
            link = f"/week/{next_milestone_id}"
        else:
            title = f"Week {week_number} Approved{score_text} — All Milestones Complete!"
            message = f"Congratulations! Supervisor {mentor_name} approved your deliverables{score_text}. All required milestones are complete."
            link = "/project"

        return cls.send_notification(
            user_id=student_user_id,
            title=title,
            message=message,
            notif_type='SUCCESS',
            link=link,
            category='SUBMISSION',
            email_template='submission_approved',
            email_context={
                'week_number': week_number,
                'milestone_title': milestone_title,
                'total_score': total_score,
                'mentor_name': mentor_name,
                'mentor_feedback': mentor_feedback
            }
        )

    @classmethod
    def notify_revision_requested(cls, student_user_id: int, milestone_title: str, milestone_id: int,
                                  week_number: int, feedback_notes: str, mentor_name: str = 'Mentor') -> Notification:
        """8. Revisions requested notification sent to intern."""
        title = f"Revisions Requested: Week {week_number}"
        message = f"Supervisor {mentor_name} reviewed Week {week_number} and requested revisions: '{feedback_notes}'"
        link = f"/week/{milestone_id}"

        return cls.send_notification(
            user_id=student_user_id,
            title=title,
            message=message,
            notif_type='WARNING',
            link=link,
            category='SUBMISSION',
            email_template='revision_requested',
            email_context={
                'week_number': week_number,
                'milestone_title': milestone_title,
                'feedback_notes': feedback_notes,
                'mentor_name': mentor_name
            }
        )

    @classmethod
    def notify_mentor_feedback(cls, student_user_id: int, milestone_title: str, milestone_id: int,
                               week_number: int, mentor_name: str = 'Mentor', feedback_summary: str = None,
                               score: float = None) -> Notification:
        """9. Mentor feedback and rubric evaluation recorded."""
        title = f"Mentor Feedback Recorded — Week {week_number}"
        message = f"Mentor {mentor_name} submitted rubric evaluation feedback for Week {week_number} ({milestone_title})."
        link = f"/week/{milestone_id}"

        return cls.send_notification(
            user_id=student_user_id,
            title=title,
            message=message,
            notif_type='INFO',
            link=link,
            category='EVALUATION',
            email_template='mentor_feedback',
            email_context={
                'week_number': week_number,
                'milestone_title': milestone_title,
                'mentor_name': mentor_name,
                'feedback_summary': feedback_summary,
                'score': score
            }
        )

    @classmethod
    def notify_internship_completed(cls, student_user_id: int, student_name: str,
                                    program_title: str = None, final_score: float = None,
                                    cert_link: str = None) -> Notification:
        """10. Final internship completion and certificate notification."""
        title = "Internship Successfully Completed — Certificate Issued!"
        message = f"Congratulations {student_name}! You have achieved 100% completion of your technical internship."
        link = cert_link or "/documents"

        return cls.send_notification(
            user_id=student_user_id,
            title=title,
            message=message,
            notif_type='SUCCESS',
            link=link,
            category='COMPLETION',
            email_template='internship_completed',
            email_context={
                'student_name': student_name,
                'program_title': program_title,
                'final_score': final_score
            }
        )

    @classmethod
    def notify_announcement(cls, recipient_user_id: int, announcement_id: int,
                            announcement_title: str, content: str, author_name: str = 'Admin',
                            priority: str = 'NORMAL', target_role: str = 'ALL') -> Notification:
        """Broadcast announcement notification."""
        urgency = 'WARNING' if priority == 'URGENT' else 'INFO'
        title = f"{'🚨 [URGENT] ' if priority == 'URGENT' else ''}Announcement: {announcement_title}"
        link = "/notifications"

        return cls.send_notification(
            user_id=recipient_user_id,
            title=title,
            message=f"{author_name} posted: {content[:180]}{'...' if len(content) > 180 else ''}",
            notif_type=urgency,
            link=link,
            category='ANNOUNCEMENT',
            email_template='announcement',
            email_context={
                'announcement_title': announcement_title,
                'announcement_content': content,
                'author_name': author_name,
                'priority': priority,
                'target_role': target_role
            }
        )

    @classmethod
    def notify_registration_welcome(cls, user_id: int, full_name: str, email: str,
                                    login_id: str, temp_password: str = None) -> Notification:
        """Onboarding welcome notification upon account registration."""
        title = "Welcome to Anti-Matrix Intern Portal"
        message = f"Welcome {full_name}! Your login credentials have been generated. Login ID: {login_id}"
        link = "/dashboard"

        return cls.send_notification(
            user_id=user_id,
            title=title,
            message=message,
            notif_type='SUCCESS',
            link=link,
            category='GENERAL',
            email_template='registration_welcome',
            email_context={
                'login_id': login_id,
                'recipient_email': email,
                'temp_password': temp_password
            },
            force_email=True
        )

    # ─── Automated Deadline Reminder Engine ───────────────────────────────────

    @classmethod
    def check_and_send_deadline_reminders(cls) -> dict:
        """
        Automated background scheduler engine for deadline reminders and overdue alerts.
        Scans all active milestones and triggers T-3d, T-1d, T-0d, and OVERDUE reminders.
        Strict deduplication via AuditLog prevents duplicate notifications.
        Skips milestones that are completed, approved, or already submitted.
        """
        now = datetime.utcnow()
        summary = {
            'checked_milestones': 0,
            'reminders_sent': 0,
            'overdue_sent': 0,
            'skipped_submitted_or_done': 0,
            'timestamp': now.isoformat()
        }

        # Query all active milestones with a deadline
        active_milestones = WeeklyMilestone.query.filter(
            WeeklyMilestone.due_at.isnot(None),
            ~WeeklyMilestone.status.in_(['COMPLETED', 'APPROVED'])
        ).all()

        summary['checked_milestones'] = len(active_milestones)

        for m in active_milestones:
            try:
                # 1. Resolve student and user
                student = m.assignment.internship.student if (m.assignment and m.assignment.internship) else None
                if not student or not student.user or not student.user.is_active:
                    continue

                # 2. Check if student already submitted deliverables for this milestone
                has_active_submission = WeeklySubmission.query.filter(
                    WeeklySubmission.student_id == student.id,
                    WeeklySubmission.milestone_id == m.id,
                    WeeklySubmission.status.in_(['SUBMITTED', 'UNDER_REVIEW', 'APPROVED', 'COMPLETED'])
                ).first() is not None

                if has_active_submission:
                    summary['skipped_submitted_or_done'] += 1
                    continue

                due = m.due_at
                delta = due - now
                seconds_remaining = delta.total_seconds()
                due_date_str = due.strftime('%B %d, %Y (%H:%M UTC)')

                stage = None
                days_left = 0

                # Determine stage
                if seconds_remaining < 0:
                    stage = 'OVERDUE'
                elif 0 <= seconds_remaining <= 86400: # <= 24 hours
                    stage = 'DUE_TODAY'
                    days_left = 0
                elif 86400 < seconds_remaining <= 2 * 86400: # 24h to 48h
                    stage = '1_DAY'
                    days_left = 1
                elif 2 * 86400 < seconds_remaining <= 3.5 * 86400: # 48h to ~84h
                    stage = '3_DAYS'
                    days_left = 3

                if not stage:
                    continue

                # 3. Deduplication Check via AuditLog
                action_key = f"DEADLINE_REMINDER_{stage}_{m.id}"
                prior_audit = AuditLog.query.filter_by(
                    action=action_key,
                    target_entity='weekly_milestones',
                    target_id=str(m.id)
                ).first()

                if prior_audit:
                    # Already sent this stage reminder
                    continue

                # 4. Dispatch Reminder
                if stage == 'OVERDUE':
                    cls.notify_overdue_task(
                        student_user_id=student.user.id,
                        milestone_title=m.title or f"Week {m.week_number}",
                        milestone_id=m.id,
                        week_number=m.week_number,
                        due_date_str=due_date_str
                    )
                    summary['overdue_sent'] += 1
                else:
                    cls.notify_deadline_reminder(
                        student_user_id=student.user.id,
                        milestone_title=m.title or f"Week {m.week_number}",
                        milestone_id=m.id,
                        week_number=m.week_number,
                        due_date_str=due_date_str,
                        days_remaining=days_left,
                        time_remaining_str="Due today!" if days_left == 0 else f"{days_left} day{'s' if days_left != 1 else ''} left"
                    )
                    summary['reminders_sent'] += 1

                # 5. Record AuditLog for Deduplication
                audit = AuditLog(
                    actor_role='SYSTEM',
                    action=action_key,
                    target_entity='weekly_milestones',
                    target_id=str(m.id),
                    details_json=json.dumps({
                        'stage': stage,
                        'student_id': student.id,
                        'student_name': student.user.full_name or student.user.name,
                        'milestone_id': m.id,
                        'week_number': m.week_number,
                        'due_at': due.isoformat(),
                        'sent_at': now.isoformat()
                    })
                )
                db.session.add(audit)
                db.session.commit()

            except Exception as e:
                db.session.rollback()
                logger.error(f"Error evaluating deadline reminder for milestone {m.id}: {e}")

        logger.info(f"Deadline reminder scan complete: {summary}")
        return summary

    # ─── Announcements System ─────────────────────────────────────────────────

    @classmethod
    def create_announcement(cls, author_id: int, title: str, content: str,
                            target_role: str = 'ALL', department: str = None,
                            project_id: int = None, priority: str = 'NORMAL') -> Announcement:
        """
        Creates a system announcement and broadcasts alerts to all matching recipients.
        """
        author = User.query.get(author_id)
        if not author:
            raise ValueError(f"Author user {author_id} not found.")

        announcement = Announcement(
            author_id=author_id,
            title=title,
            content=content,
            target_role=target_role,
            department=department,
            project_id=project_id,
            priority=priority,
            created_at=datetime.utcnow()
        )
        db.session.add(announcement)
        db.session.commit()

        # Resolve target recipients
        recipient_query = User.query.filter_by(is_active=True)

        if target_role == 'STUDENT':
            recipient_query = recipient_query.filter_by(role='student')
        elif target_role == 'MENTOR':
            recipient_query = recipient_query.filter_by(role='mentor')

        recipients = recipient_query.all()

        # Filter by department or project if provided
        filtered_users = []
        for u in recipients:
            if department and u.role == 'student':
                # Check student department
                std = getattr(u, 'student_profile', None)
                if std and std.department and std.department.lower() != department.lower():
                    continue
            if project_id and u.role == 'student':
                std = getattr(u, 'student_profile', None)
                internship = std.active_internship if std else None
                if not internship or internship.project_id != project_id:
                    continue
            filtered_users.append(u)

        author_name = author.full_name or author.name or 'Portal Administrator'

        # Broadcast notifications
        for u in filtered_users:
            try:
                cls.notify_announcement(
                    recipient_user_id=u.id,
                    announcement_id=announcement.id,
                    announcement_title=title,
                    content=content,
                    author_name=author_name,
                    priority=priority,
                    target_role=target_role
                )
            except Exception as e:
                logger.warning(f"Could not broadcast announcement to user {u.id}: {e}")

        # Audit Log
        audit = AuditLog(
            actor_id=author_id,
            actor_role=author.role,
            action='ANNOUNCEMENT_BROADCAST',
            target_entity='announcements',
            target_id=str(announcement.id),
            details_json=json.dumps({
                'title': title,
                'target_role': target_role,
                'priority': priority,
                'recipients_count': len(filtered_users)
            })
        )
        db.session.add(audit)
        db.session.commit()

        return announcement

    @classmethod
    def get_user_announcements(cls, user_id: int, limit: int = 20) -> list:
        """
        Retrieves announcements visible to the specified user.
        """
        user = User.query.get(user_id)
        if not user:
            return []

        query = Announcement.query
        if user.role == 'student':
            query = query.filter(Announcement.target_role.in_(['ALL', 'STUDENT']))
        elif user.role == 'mentor':
            query = query.filter(Announcement.target_role.in_(['ALL', 'MENTOR']))

        announcements = query.order_by(Announcement.id.desc()).limit(limit).all()
        return announcements

    @classmethod
    def mark_announcement_read(cls, announcement_id: int, user_id: int) -> bool:
        """
        Marks an announcement as read/acknowledged for the given user.
        """
        existing = AnnouncementRead.query.filter_by(
            announcement_id=announcement_id,
            user_id=user_id
        ).first()

        if existing:
            return True

        read_record = AnnouncementRead(
            announcement_id=announcement_id,
            user_id=user_id,
            read_at=datetime.utcnow()
        )
        db.session.add(read_record)
        db.session.commit()
        return True

    # ─── Querying & IDOR-Protected Mutation Helpers ───────────────────────────

    @classmethod
    def get_user_notifications(cls, user_id: int, category: str = None,
                               unread_only: bool = False, limit: int = 50) -> list:
        """
        Retrieves notifications for user with optional category and unread state filtering.
        """
        query = Notification.query.filter_by(user_id=user_id)
        if category and category.upper() != 'ALL':
            query = query.filter_by(category=category.upper())
        if unread_only:
            query = query.filter_by(is_read=False)

        return query.order_by(Notification.id.desc()).limit(limit).all()

    @classmethod
    def mark_as_read(cls, notification_id: int, user_id: int) -> bool:
        """
        Marks a specific notification as read, enforcing strict IDOR protection.
        Returns False if notification does not belong to user_id.
        """
        notif = Notification.query.get(notification_id)
        if not notif or notif.user_id != user_id:
            logger.warning(f"IDOR violation attempt: user {user_id} tried to mark notification {notification_id}")
            return False

        notif.is_read = True
        db.session.commit()
        return True

    @classmethod
    def mark_all_as_read(cls, user_id: int, category: str = None) -> int:
        """
        Marks all notifications for user as read with optional category filter.
        """
        query = Notification.query.filter_by(user_id=user_id, is_read=False)
        if category and category.upper() != 'ALL':
            query = query.filter_by(category=category.upper())

        updated_count = query.update({'is_read': True})
        db.session.commit()
        return updated_count
