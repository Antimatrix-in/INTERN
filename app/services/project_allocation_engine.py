"""ANTI-MATRIX Intelligent Project Allocation Engine (Phase 2, Module 4)

Provides:
1. Intern-to-project skill matching with transparent scoring, matching & missing skill telemetry.
2. Mentor-to-project expertise matching with availability, workload & capacity tracking.
3. Multi-dimensional ranking and recommendation algorithms.
4. Transactional, atomic project allocation with rigorous constraint validation and audit logging.
"""

import json
import logging
from datetime import datetime
from app.extensions import db
from app.models import (
    Project, ProjectAssignment, Student, Internship, User,
    WeeklyMilestone, AuditLog, College
)
from app.services.mentor_service import get_all_active_mentors, is_approved_mentor
from app.services.problem_allocation_service import ProblemAllocationService

logger = logging.getLogger(__name__)

# Common tech synonym normalization dictionary
TECH_SYNONYMS = {
    'js': 'javascript',
    'ts': 'typescript',
    'py': 'python',
    'reactjs': 'react',
    'react.js': 'react',
    'nodejs': 'node.js',
    'node': 'node.js',
    'postgres': 'postgresql',
    'k8s': 'kubernetes',
    'ml': 'machine learning',
    'ai': 'artificial intelligence',
    'tf': 'tensorflow',
    'torch': 'pytorch',
    'aws': 'amazon web services',
    'gcp': 'google cloud',
    'azure': 'microsoft azure',
    'django': 'python django',
    'flask': 'python flask',
    'fastapi': 'python fastapi',
    'html/css': 'html',
    'tailwind': 'tailwindcss',
}


def normalize_skill_token(token):
    """Normalize a skill token for accurate matching."""
    if not token:
        return ''
    cleaned = token.strip().lower()
    # Replace punctuation characters except dots/hyphens
    cleaned = cleaned.replace('/', ' ').replace(';', ' ')
    cleaned = cleaned.strip()
    return TECH_SYNONYMS.get(cleaned, cleaned)


def tokenize_skills(skill_input):
    """Extract and normalize a list/string of skills into a set of tokens."""
    if not skill_input:
        return set()

    if isinstance(skill_input, list):
        tokens = set()
        for s in skill_input:
            if isinstance(s, str):
                for part in s.split(','):
                    norm = normalize_skill_token(part)
                    if norm:
                        tokens.add(norm)
        return tokens

    if isinstance(skill_input, str):
        # Handle comma, newline, or slash separated strings
        tokens = set()
        for part in skill_input.replace('\n', ',').split(','):
            norm = normalize_skill_token(part)
            if norm:
                tokens.add(norm)
        return tokens

    return set()


class ProjectAllocationEngine:
    """Core intelligent matching and allocation engine."""

    @staticmethod
    def calculate_intern_project_match(student, project):
        """
        Compare intern skills and background with project tech stack and domain.
        Returns a transparent match breakdown dictionary:
        - match_score: integer 0-100
        - match_tier: 'EXCELLENT' | 'GOOD' | 'MODERATE' | 'LOW'
        - matching_skills: list of project skills matched by intern
        - missing_skills: list of project skills not matched
        - extra_skills: list of student skills outside project requirements
        - duration_compatible: bool
        - is_eligible: bool
        - requires_override: bool (if score < 50)
        """
        intern_skills = tokenize_skills(student.skills if hasattr(student, 'skills') else '')
        # Fallback to student department or degree if skills are empty
        if not intern_skills and student.user and student.user.skills:
            intern_skills = tokenize_skills(student.user.skills)

        project_skills_raw = project.tech_stack_list
        project_skills = tokenize_skills(project_skills_raw)

        # Calculate exact and partial skill matches
        matching_skills = []
        missing_skills = []

        if project_skills:
            for req in project_skills_raw:
                req_norm = normalize_skill_token(req)
                # Check direct containment or token match
                matched = False
                for s in intern_skills:
                    if req_norm == s or req_norm in s or s in req_norm:
                        matched = True
                        break
                if matched:
                    matching_skills.append(req)
                else:
                    missing_skills.append(req)

            # Base score calculation from required skills
            match_ratio = len(matching_skills) / max(len(project_skills_raw), 1)
            raw_score = match_ratio * 100
        else:
            # When project does not define explicit skills, default base score
            raw_score = 65.0
            missing_skills = []

        # Domain synergy bonus (+10% if student department aligns with project domain)
        student_dept = (student.department.name if hasattr(student, 'department') and student.department else '') or (student.user.department if student.user else '')
        student_dept_clean = student_dept.lower()
        project_domain_clean = (project.domain or '').lower()

        domain_synergy = False
        common_domain_keywords = ['computer', 'software', 'cloud', 'data', 'ai', 'machine learning', 'cyber', 'network', 'web', 'full stack']
        for kw in common_domain_keywords:
            if kw in student_dept_clean and kw in project_domain_clean:
                domain_synergy = True
                break

        if domain_synergy:
            raw_score = min(100.0, raw_score + 10.0)

        match_score = int(round(raw_score))

        # Determine Match Tier
        if match_score >= 80:
            match_tier = 'EXCELLENT'
        elif match_score >= 60:
            match_tier = 'GOOD'
        elif match_score >= 40:
            match_tier = 'MODERATE'
        else:
            match_tier = 'LOW'

        # Check duration compatibility
        student_duration = 1
        if student.active_internship and student.active_internship.plan:
            student_duration = student.active_internship.plan.duration_months
        duration_compatible = (project.duration_months == student_duration)

        is_eligible = (
            project.is_active and
            not project.is_archived and
            duration_compatible and
            not project.is_at_capacity
        )

        requires_override = (match_score < 50 or project.is_at_capacity or not duration_compatible)

        return {
            'project': project,
            'match_score': match_score,
            'match_tier': match_tier,
            'matching_skills': matching_skills,
            'missing_skills': missing_skills,
            'duration_compatible': duration_compatible,
            'domain_synergy': domain_synergy,
            'is_eligible': is_eligible,
            'is_at_capacity': project.is_at_capacity,
            'active_interns_count': project.active_interns_count,
            'max_capacity': project.max_capacity,
            'remaining_capacity': project.remaining_capacity,
            'requires_override': requires_override
        }

    @staticmethod
    def calculate_mentor_project_match(mentor, project):
        """
        Compare mentor skills and capacity with project tech stack and domain.
        Returns mentor suitability telemetry:
        - match_score: integer 0-100
        - suitability_score: float 0-100 (weighted by match + available capacity margin)
        - matching_skills: list of matching skills
        - availability_status: AVAILABLE | BUSY | ON_LEAVE
        - is_available: bool
        - is_recommended: bool
        """
        mentor_skills = tokenize_skills(mentor.skills or '')
        project_skills_raw = project.tech_stack_list
        project_skills = tokenize_skills(project_skills_raw)

        matching_skills = []
        if project_skills:
            for req in project_skills_raw:
                req_norm = normalize_skill_token(req)
                matched = False
                for s in mentor_skills:
                    if req_norm == s or req_norm in s or s in req_norm:
                        matched = True
                        break
                if matched:
                    matching_skills.append(req)
            match_ratio = len(matching_skills) / max(len(project_skills_raw), 1)
            raw_score = match_ratio * 100
        else:
            raw_score = 70.0

        # Department / domain synergy bonus
        mentor_dept = (mentor.department or '').lower()
        project_domain = (project.domain or '').lower()
        if any(term in mentor_dept for term in project_domain.split() if len(term) > 3):
            raw_score = min(100.0, raw_score + 15.0)

        match_score = int(round(raw_score))

        # Capacity metrics
        active_interns = mentor.active_interns_count
        max_cap = mentor.max_capacity if (mentor.max_capacity is not None and mentor.max_capacity > 0) else 5
        remaining_cap = max(0, max_cap - active_interns)
        capacity_ratio = remaining_cap / max_cap

        is_available = (mentor.is_active and mentor.availability_status != 'ON_LEAVE')
        is_at_cap = (active_interns >= max_cap)

        if not is_available:
            suitability_score = 0.0
        elif is_at_cap:
            suitability_score = match_score * 0.3
        else:
            # Weighted suitability: 75% skill & domain alignment + 25% capacity margin
            suitability_score = (match_score * 0.75) + (capacity_ratio * 25.0)

        is_recommended = (is_available and not is_at_cap and match_score >= 30)

        return {
            'mentor': mentor,
            'match_score': match_score,
            'suitability_score': round(suitability_score, 1),
            'matching_skills': matching_skills,
            'availability_status': mentor.availability_status or 'AVAILABLE',
            'is_available': is_available,
            'active_interns_count': active_interns,
            'max_capacity': max_cap,
            'remaining_capacity': remaining_cap,
            'is_at_capacity': is_at_cap,
            'capacity_percent': mentor.capacity_percent,
            'is_recommended': is_recommended
        }

    @staticmethod
    def get_recommended_projects_for_intern(student, filter_duration=True, include_full=True):
        """
        Rank all projects for an intern sorted by match score and eligibility.
        """
        query = Project.query.filter(Project.is_archived == False)

        if filter_duration and student.active_internship and student.active_internship.plan:
            dur = student.active_internship.plan.duration_months
            query = query.filter(Project.duration_months == dur)

        projects = query.all()

        scored_projects = []
        for p in projects:
            match_data = ProjectAllocationEngine.calculate_intern_project_match(student, p)
            if not include_full and match_data['is_at_capacity']:
                continue
            scored_projects.append(match_data)

        # Sort: eligible first, then match_score desc, then remaining_capacity desc
        scored_projects.sort(
            key=lambda x: (
                x['is_eligible'],
                x['match_score'],
                x['remaining_capacity']
            ),
            reverse=True
        )

        return scored_projects

    @staticmethod
    def get_recommended_mentors_for_project(project):
        """
        Rank all active mentors for a specific project.
        """
        mentors = get_all_active_mentors()
        scored_mentors = []

        for m in mentors:
            data = ProjectAllocationEngine.calculate_mentor_project_match(m, project)
            scored_mentors.append(data)

        # Sort: recommended first, then suitability_score desc, then remaining_capacity desc
        scored_mentors.sort(
            key=lambda x: (
                x['is_recommended'],
                x['suitability_score'],
                x['remaining_capacity']
            ),
            reverse=True
        )

        return scored_mentors

    @staticmethod
    def execute_allocation(
        internship,
        project,
        mentor,
        assigned_by_user,
        override_reason=None,
        force_override=False,
        deadline=None
    ):
        """
        Execute an atomic project and mentor allocation with strict constraint enforcement.
        Validates:
        1. Internship exists and is ACTIVE.
        2. Project is ACTIVE and NOT ARCHIVED.
        3. Duration compatibility between internship plan and project.
        4. Project capacity limit.
        5. Mentor is ACTIVE and not ON_LEAVE.
        6. Mentor capacity limit.
        7. Audit trail logging.
        """
        student = internship.student
        if not student:
            return None, "No student record linked to this internship."

        # 1. Project validity checks
        if not project:
            return None, "Project not found."

        if not project.is_active or project.is_archived:
            return None, f"Cannot allocate: Project '{project.project_code}' is {project.status.lower()}."

        # 2. Duration compatibility
        plan_duration = internship.plan.duration_months if internship.plan else 1
        if project.duration_months != plan_duration and not force_override:
            return None, (
                f"Duration mismatch: Project duration ({project.duration_months} Month) "
                f"does not match intern's registered duration ({plan_duration} Month)."
            )

        # 3. Project capacity check
        if project.is_at_capacity and not force_override:
            return None, (
                f"Project '{project.project_code}' has reached maximum intern capacity "
                f"({project.active_interns_count}/{project.max_capacity}). Admin override required."
            )

        # 4. Mentor validity checks
        if not mentor:
            return None, "Mentor not found."

        if not mentor.is_active:
            return None, f"Mentor '{mentor.full_name}' is inactive."

        if mentor.availability_status == 'ON_LEAVE' and not force_override:
            return None, f"Mentor '{mentor.full_name}' is currently on leave. Please select an available mentor."

        # 5. Mentor capacity check
        if mentor.is_at_capacity and not force_override:
            return None, (
                f"Mentor '{mentor.full_name}' is at maximum mentee capacity "
                f"({mentor.active_interns_count}/{mentor.max_capacity}). Admin override required."
            )

        # 6. Override validation: If capacity is exceeded or score is very low (< 25%), reason is mandatory
        match_data = ProjectAllocationEngine.calculate_intern_project_match(student, project)
        if (project.is_at_capacity or mentor.is_at_capacity or match_data['match_score'] < 25) and not override_reason and not force_override:
            return None, "This allocation requires an administrative override reason (capacity constraint or low match score)."

        # Atomic database transaction
        try:
            # Handle any prior active assignment: supersede safely
            existing_active_assignment = internship.active_assignment
            if existing_active_assignment and existing_active_assignment.status in ['ASSIGNED', 'IN_PROGRESS', 'SUBMITTED', 'REVISION_REQUIRED', 'ACTIVE']:
                existing_active_assignment.status = 'SUPERSEDED'
                existing_active_assignment.remarks = f"Reallocated to project {project.project_code} by {assigned_by_user.full_name if assigned_by_user else 'Admin'}"

            # Create new project assignment and initialize weekly milestones
            assignment = ProblemAllocationService.assign_problem_to_internship(
                internship=internship,
                project=project,
                assigned_by_user=assigned_by_user,
                deadline=deadline
            )

            # Store override reason
            if override_reason:
                assignment.override_reason = override_reason
                assignment.remarks = f"Admin Override: {override_reason}"

            # Link mentor to internship
            internship.mentor_id = mentor.id

            # Log Audit Trail
            audit = AuditLog(
                actor_id=assigned_by_user.id if assigned_by_user else 1,
                actor_role=getattr(assigned_by_user, 'role', 'admin'),
                action='ALLOCATE_PROJECT_INTELLIGENT',
                target_entity='internships',
                target_id=str(internship.id),
                details_json=json.dumps({
                    'student_id': student.id,
                    'student_uid': student.student_uid,
                    'project_id': project.id,
                    'project_code': project.project_code,
                    'mentor_id': mentor.id,
                    'mentor_name': mentor.full_name,
                    'match_score': match_data['match_score'],
                    'match_tier': match_data['match_tier'],
                    'override_reason': override_reason or 'Direct algorithmic match'
                })
            )
            db.session.add(audit)
            db.session.commit()

            # Dispatch Project & Mentor Allocation Notifications (Module 7)
            try:
                from app.services.notification_service import NotificationService
                if student.user:
                    NotificationService.notify_project_allocated(
                        student_user_id=student.user.id,
                        project_title=project.title or project.project_code,
                        project_id=project.id,
                        tech_stack=project.tech_stack,
                        mentor_name=mentor.full_name
                    )
                    NotificationService.notify_mentor_assigned(
                        student_user_id=student.user.id,
                        mentor_name=mentor.full_name,
                        mentor_email=mentor.email,
                        mentor_dept=mentor.department
                    )
            except Exception as notif_err:
                logger.warning(f"Could not dispatch allocation notification: {notif_err}")

            return assignment, None

        except Exception as e:
            db.session.rollback()
            logger.exception(f"Error during project allocation: {e}")
            return None, f"Allocation transaction failed: {str(e)}"
