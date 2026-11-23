"""ANTI-MATRIX Phase 2 — Module 4 Test Suite
Intelligent Project Allocation Engine & Constraints Verification.
"""

import json
from datetime import datetime, timedelta
import unittest
from app import create_app, db
from app.models import (
    User, Student, Internship, Project, ProjectAssignment,
    WeeklyMilestone, WeeklyTask, AuditLog, InternshipPlan
)
from app.seed import seed_initial_data
from app.services.project_allocation_engine import (
    ProjectAllocationEngine, normalize_skill_token, tokenize_skills
)
from tests.test_config import TestConfig


class TestProjectAllocationEngineModule4(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()

        db.drop_all()
        db.create_all()
        seed_initial_data()

        self.client = self.app.test_client()

        # Seeded admin user
        self.admin = User.query.filter_by(employee_id='admin').first() or User.query.filter_by(role='admin').first()

        # Seeded mentors
        self.mentor1 = User.query.filter_by(email='praveen@antimatrix.co.in').first()
        self.mentor1.department = 'Cloud Architecture & DevOps'
        self.mentor1.skills = 'Python, AWS, Terraform, Docker, Kubernetes'
        self.mentor1.max_capacity = 3
        self.mentor1.availability_status = 'AVAILABLE'

        self.mentor2 = User.query.filter_by(email='satishkumar@antimatrix.co.in').first()
        self.mentor2.department = 'Computer Science & Engineering'
        self.mentor2.skills = 'Python, Machine Learning, Scikit-learn, PyTorch, SQL'
        self.mentor2.max_capacity = 2
        self.mentor2.availability_status = 'AVAILABLE'

        # Seeded students
        self.student1 = Student.query.filter_by(student_uid='AM-INT-2026-001').first()
        self.student1.skills = 'Python, Flask, Docker, PostgreSQL'
        self.internship1 = self.student1.active_internship

        self.student2 = Student.query.filter_by(student_uid='AM-INT-2026-002').first()
        self.student2.skills = 'React, JavaScript, CSS, HTML, Node.js'
        self.internship2 = self.student2.active_internship

        # Dedicated test projects with exact known attributes
        self.cloud_project = Project(
            project_code='AM-1M-TESTCLOUD',
            title='Cloud Infrastructure & Docker Deployment',
            domain='Cloud Architecture & DevOps',
            description='Deploy containerized microservices.',
            duration_months=1,
            duration_weeks=4,
            tech_stack_json=json.dumps(['Python', 'Docker', 'PostgreSQL', 'AWS']),
            max_capacity=2,
            is_active=True,
            is_archived=False
        )
        db.session.add(self.cloud_project)

        self.ai_project = Project(
            project_code='AM-3M-TESTAI',
            title='Enterprise Deep Learning Platform',
            domain='Artificial Intelligence & ML',
            description='Build end-to-end vision pipeline.',
            duration_months=3,
            duration_weeks=12,
            tech_stack_json=json.dumps(['Python', 'PyTorch', 'Computer Vision', 'CUDA']),
            max_capacity=5,
            is_active=True,
            is_archived=False
        )
        db.session.add(self.ai_project)

        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def login_admin(self):
        return self.client.post('/admin/login', data={
            'email_or_id': 'admin',
            'password': 'Admin@12345'
        }, follow_redirects=True)

    # ─── TEST 1: Skill Matching & Transparent Scoring ──────────────────────────

    def test_01_intern_skill_matching_and_transparent_scoring(self):
        """Test intern-project match scoring, matching skills, and missing skills."""
        match_data = ProjectAllocationEngine.calculate_intern_project_match(self.student1, self.cloud_project)

        self.assertIn('match_score', match_data)
        self.assertIn('match_tier', match_data)
        self.assertIn('matching_skills', match_data)
        self.assertIn('missing_skills', match_data)

        # Student 1 has Python, Docker, PostgreSQL (3 of 4 required)
        self.assertIn('Python', match_data['matching_skills'])
        self.assertIn('Docker', match_data['matching_skills'])
        self.assertIn('PostgreSQL', match_data['matching_skills'])
        self.assertIn('AWS', match_data['missing_skills'])

        # Score should be >= 75%
        self.assertGreaterEqual(match_data['match_score'], 75)
        self.assertIn(match_data['match_tier'], ['GOOD', 'EXCELLENT'])
        self.assertTrue(match_data['duration_compatible'])

    # ─── TEST 2: Mentor Expertise & Capacity Matching ──────────────────────────

    def test_02_mentor_expertise_and_capacity_matching(self):
        """Test mentor matching algorithm and ranking by expertise and available capacity."""
        # Mentor 1 (DevOps/Cloud) should score higher for Cloud Project than Mentor 2 (ML)
        m1_data = ProjectAllocationEngine.calculate_mentor_project_match(self.mentor1, self.cloud_project)
        m2_data = ProjectAllocationEngine.calculate_mentor_project_match(self.mentor2, self.cloud_project)

        self.assertGreater(m1_data['suitability_score'], m2_data['suitability_score'])
        self.assertTrue(m1_data['is_available'])
        self.assertFalse(m1_data['is_at_capacity'])
        self.assertIn('AWS', m1_data['matching_skills'])
        self.assertIn('Docker', m1_data['matching_skills'])

    # ─── TEST 3: Project Capacity Constraint Validation ────────────────────────

    def test_03_project_capacity_constraint_validation(self):
        """Ensure allocation fails when project reaches max capacity without override."""
        # Align student2 duration to 1-Month to isolate project capacity check
        self.internship2.plan_id = self.internship1.plan_id
        self.cloud_project.max_capacity = 1
        db.session.commit()

        # Allocate student 1 successfully
        assignment, err = ProjectAllocationEngine.execute_allocation(
            internship=self.internship1,
            project=self.cloud_project,
            mentor=self.mentor1,
            assigned_by_user=self.admin
        )
        self.assertIsNotNone(assignment)
        self.assertIsNone(err)
        self.assertTrue(self.cloud_project.is_at_capacity)

        # Attempt to allocate another student without override
        assignment2, err2 = ProjectAllocationEngine.execute_allocation(
            internship=self.internship2,
            project=self.cloud_project,
            mentor=self.mentor1,
            assigned_by_user=self.admin,
            force_override=False
        )
        self.assertIsNone(assignment2)
        self.assertIn('maximum intern capacity', err2)

        # Attempt with authorized admin override reason
        assignment3, err3 = ProjectAllocationEngine.execute_allocation(
            internship=self.internship2,
            project=self.cloud_project,
            mentor=self.mentor1,
            assigned_by_user=self.admin,
            override_reason='Authorized project cap expansion for capstone cohort',
            force_override=True
        )
        self.assertIsNotNone(assignment3)
        self.assertIsNone(err3)

    # ─── TEST 4: Mentor Capacity Constraint Validation ─────────────────────────

    def test_04_mentor_capacity_constraint_validation(self):
        """Ensure allocation fails when mentor is at max capacity unless overridden."""
        # Mentor 2 starts with 0 active interns. Set max_capacity to 1.
        self.mentor2.max_capacity = 1
        self.internship2.plan_id = self.internship1.plan_id
        db.session.commit()

        # First allocation fills mentor2 capacity
        assignment, err = ProjectAllocationEngine.execute_allocation(
            internship=self.internship1,
            project=self.cloud_project,
            mentor=self.mentor2,
            assigned_by_user=self.admin
        )
        self.assertIsNotNone(assignment)
        self.assertTrue(self.mentor2.is_at_capacity)

        # Second allocation to same mentor without override must fail
        assignment2, err2 = ProjectAllocationEngine.execute_allocation(
            internship=self.internship2,
            project=self.cloud_project,
            mentor=self.mentor2,
            assigned_by_user=self.admin,
            force_override=False
        )
        self.assertIsNone(assignment2)
        self.assertIn('maximum mentee capacity', err2)

    # ─── TEST 5: Duration Compatibility Validation ─────────────────────────────

    def test_05_internship_duration_compatibility_validation(self):
        """Ensure 1-month intern cannot be assigned to 3-month project without override."""
        self.assertEqual(self.internship1.plan.duration_months, 1)
        self.assertEqual(self.ai_project.duration_months, 3)

        assignment, err = ProjectAllocationEngine.execute_allocation(
            internship=self.internship1,
            project=self.ai_project,
            mentor=self.mentor2,
            assigned_by_user=self.admin,
            force_override=False
        )
        self.assertIsNone(assignment)
        self.assertIn('Duration mismatch', err)

    # ─── TEST 6: Inactive or Archived Project Rejection ─────────────────────────

    def test_06_inactive_or_archived_project_prevention(self):
        """Ensure inactive or archived projects cannot be assigned."""
        self.cloud_project.is_archived = True
        self.cloud_project.is_active = False
        db.session.commit()

        assignment, err = ProjectAllocationEngine.execute_allocation(
            internship=self.internship1,
            project=self.cloud_project,
            mentor=self.mentor1,
            assigned_by_user=self.admin
        )
        self.assertIsNone(assignment)
        self.assertIn('Cannot allocate', err)

    # ─── TEST 7: Inactive or On-Leave Mentor Rejection ──────────────────────────

    def test_07_inactive_or_on_leave_mentor_prevention(self):
        """Ensure mentors with availability ON_LEAVE or inactive cannot be assigned."""
        self.mentor1.availability_status = 'ON_LEAVE'
        db.session.commit()

        assignment, err = ProjectAllocationEngine.execute_allocation(
            internship=self.internship1,
            project=self.cloud_project,
            mentor=self.mentor1,
            assigned_by_user=self.admin,
            force_override=False
        )
        self.assertIsNone(assignment)
        self.assertIn('on leave', err)

    # ─── TEST 8: Atomic Allocation & Milestone Roadmap Initialization ──────────

    def test_08_atomic_allocation_and_milestone_generation(self):
        """Ensure successful allocation atomically links mentor, creates assignment, and initializes milestones."""
        assignment, err = ProjectAllocationEngine.execute_allocation(
            internship=self.internship1,
            project=self.cloud_project,
            mentor=self.mentor1,
            assigned_by_user=self.admin
        )
        self.assertIsNotNone(assignment)
        self.assertIsNone(err)

        # Verify mentor relationship
        self.assertEqual(self.internship1.mentor_id, self.mentor1.id)
        self.assertEqual(self.internship1.assigned_mentor_name, 'Praveen')

        # Verify weekly milestones generated
        milestones = assignment.weekly_milestones.order_by(WeeklyMilestone.week_number.asc()).all()
        self.assertEqual(len(milestones), self.cloud_project.total_weeks)

        # Week 1 is AVAILABLE with 7-day initial deadline
        week_1 = milestones[0]
        self.assertEqual(week_1.status, 'AVAILABLE')
        self.assertIsNotNone(week_1.due_at)

        # Subsequent weeks are LOCKED
        for w in milestones[1:]:
            self.assertEqual(w.status, 'LOCKED')

        # Verify AuditLog was recorded
        audit = AuditLog.query.filter_by(action='ALLOCATE_PROJECT_INTELLIGENT').first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor_id, self.admin.id)

    # ─── TEST 9: Reallocation Supersedes Previous Active Assignment ────────────

    def test_09_reallocation_and_superseding_previous_assignment(self):
        """Ensure reallocating an intern safely supersedes previous active assignment."""
        # Initial allocation
        a1, _ = ProjectAllocationEngine.execute_allocation(
            internship=self.internship1,
            project=self.cloud_project,
            mentor=self.mentor1,
            assigned_by_user=self.admin
        )
        self.assertIsNotNone(a1)
        self.assertEqual(a1.status, 'ASSIGNED')

        # Create a second 1-month project
        p2 = Project(
            project_code='AM-1M-ALT',
            title='Alternative Full Stack Project',
            domain='Web Development',
            description='Second project',
            duration_months=1,
            duration_weeks=4,
            tech_stack_json=json.dumps(['Python', 'Flask']),
            max_capacity=5,
            is_active=True
        )
        db.session.add(p2)
        db.session.commit()

        # Reallocate to p2
        a2, err = ProjectAllocationEngine.execute_allocation(
            internship=self.internship1,
            project=p2,
            mentor=self.mentor2,
            assigned_by_user=self.admin,
            override_reason='Intern requested track transfer'
        )
        self.assertIsNotNone(a2)
        self.assertIsNone(err)

        # Refresh previous assignment: must be SUPERSEDED
        prev = ProjectAssignment.query.get(a1.id)
        self.assertEqual(prev.status, 'SUPERSEDED')
        self.assertEqual(a2.status, 'ASSIGNED')
        self.assertEqual(self.internship1.mentor_id, self.mentor2.id)

    # ─── TEST 10: Admin Routes & Endpoints Access ──────────────────────────────

    def test_10_admin_allocation_routes(self):
        """Test allocations hub, wizard, and dynamic mentor recommendation API."""
        self.login_admin()

        # Allocations Hub
        resp = self.client.get('/admin/allocations')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Intelligent Allocation Hub', resp.data)

        # Allocation Wizard
        resp = self.client.get(f'/admin/allocations/wizard?student_id={self.student1.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Recommended Projects', resp.data)

        # API Dynamic Mentor Recommendation
        resp_api = self.client.get(f'/admin/allocations/api/mentors/{self.cloud_project.id}')
        self.assertEqual(resp_api.status_code, 200)
        data = json.loads(resp_api.data)
        self.assertIn('mentors', data)
        self.assertGreaterEqual(len(data['mentors']), 1)

        # Project Archive Toggle
        resp_arch = self.client.post(f'/admin/projects/{self.cloud_project.id}/archive', follow_redirects=True)
        self.assertEqual(resp_arch.status_code, 200)
        updated_p = Project.query.get(self.cloud_project.id)
        self.assertTrue(updated_p.is_archived)
        self.assertFalse(updated_p.is_active)


if __name__ == '__main__':
    unittest.main()
