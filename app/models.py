import json
from datetime import datetime, timezone, date
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import event
from sqlalchemy.orm import foreign, joinedload
from app.extensions import db, login_manager

@login_manager.user_loader
def load_user(user_id):
    try:
        return User.query.options(joinedload(User.student_profile)).filter_by(id=int(user_id)).first()
    except (ValueError, TypeError):
        return None


class User(db.Model, UserMixin):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    employee_id = db.Column(db.String(50), unique=True, nullable=True, index=True) # AM-ADM-..., AM-MTR-...
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='student') # super_admin, admin, hr, mentor, evaluator, student
    name = db.Column(db.String(100), nullable=True) # Portfolio compatibility
    full_name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), nullable=True)
    avatar_url = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    must_change_password = db.Column(db.Boolean, default=False)
    password_changed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Mentor Profile & Operational Attributes (Module 3)
    department = db.Column(db.String(100), nullable=True, default='Engineering')
    skills = db.Column(db.String(255), nullable=True, default='Full-Stack Development')
    designation = db.Column(db.String(100), nullable=True, default='Technical Mentor')
    max_capacity = db.Column(db.Integer, nullable=True, default=5)
    availability_status = db.Column(db.String(30), nullable=True, default='AVAILABLE') # AVAILABLE, BUSY, ON_LEAVE

    def __init__(self, **kwargs):
        if 'full_name' in kwargs and 'name' not in kwargs:
            kwargs['name'] = kwargs['full_name']
        elif 'name' in kwargs and 'full_name' not in kwargs:
            kwargs['full_name'] = kwargs['name']
        super().__init__(**kwargs)

    # Relationships
    student_profile = db.relationship('Student', backref='user', uselist=False, cascade='all, delete-orphan')
    notifications = db.relationship('Notification', backref='user', lazy='dynamic', cascade='all, delete-orphan', order_by='Notification.id.desc()')
    notification_preferences = db.relationship('NotificationPreference', backref='user', uselist=False, cascade='all, delete-orphan')
    audit_logs = db.relationship('AuditLog', backref='actor', lazy='dynamic')

    @property
    def unread_notifications_count(self):
        try:
            return self.notifications.filter_by(is_read=False).count()
        except Exception:
            return 0

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash or not password:
            return False

        # 1. Standard Werkzeug hash check (scrypt, pbkdf2:sha256, sha256, etc.)
        try:
            if check_password_hash(self.password_hash, password):
                return True
        except Exception:
            pass

        # 2. Check for bcrypt hash ($2b$, $2a$, $2y$, etc.)
        if isinstance(self.password_hash, str) and self.password_hash.startswith(('$2b$', '$2a$', '$2y$', '$2x$')):
            try:
                import bcrypt
                if bcrypt.checkpw(password.encode('utf-8'), self.password_hash.encode('utf-8')):
                    return True
            except Exception:
                pass

        # 3. Check for raw SHA256 hex digest
        try:
            import hashlib
            if hashlib.sha256(password.encode('utf-8')).hexdigest() == self.password_hash:
                return True
        except Exception:
            pass

        # 4. Fallback if stored as plaintext during temporary credential migration
        if self.password_hash == password:
            return True

        return False

    @property
    def is_admin_or_staff(self):
        try:
            from flask import session, has_request_context
            if has_request_context() and session.get('auth_realm') == 'employee':
                return False
        except Exception:
            pass
        return self.role in ['super_admin', 'admin', 'hr', 'mentor', 'evaluator']

    # Mentor Operations & Telemetry Properties (Module 3)
    @property
    def active_interns_count(self):
        from app.models import Internship
        return Internship.query.filter_by(mentor_id=self.id, status='ACTIVE').count()

    @property
    def assigned_internships_list(self):
        from app.models import Internship
        return Internship.query.filter_by(mentor_id=self.id).order_by(Internship.id.desc()).all()

    @property
    def is_at_capacity(self):
        cap = self.max_capacity if (self.max_capacity and self.max_capacity > 0) else 5
        return self.active_interns_count >= cap

    @property
    def capacity_percent(self):
        cap = self.max_capacity if (self.max_capacity and self.max_capacity > 0) else 5
        return min(100, int((self.active_interns_count / cap) * 100))

    @property
    def pending_reviews_count(self):
        from app.models import Internship, WeeklySubmission
        student_ids = [i.student_id for i in Internship.query.filter_by(mentor_id=self.id, status='ACTIVE').all()]
        if not student_ids:
            return 0
        return WeeklySubmission.query.filter(
            WeeklySubmission.student_id.in_(student_ids),
            WeeklySubmission.status.in_(['SUBMITTED', 'UNDER_REVIEW'])
        ).count()

    def __repr__(self):
        return f'<User {self.email} ({self.role})>'


@event.listens_for(User, 'before_insert')
@event.listens_for(User, 'before_update')
def sync_user_names(mapper, connection, target):
    """Ensure both name and full_name are populated for cross-system Supabase compatibility."""
    if target.full_name and not target.name:
        target.name = target.full_name
    elif target.name and not target.full_name:
        target.full_name = target.name


class College(db.Model):
    __tablename__ = 'colleges'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    state = db.Column(db.String(100), nullable=False)
    city = db.Column(db.String(100), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    departments = db.relationship('Department', backref='college', cascade='all, delete-orphan')
    students = db.relationship('Student', backref='college', lazy='dynamic')

    def __repr__(self):
        return f'<College {self.code} - {self.name}>'


class Department(db.Model):
    __tablename__ = 'departments'

    id = db.Column(db.Integer, primary_key=True)
    college_id = db.Column(db.Integer, db.ForeignKey('colleges.id', ondelete='CASCADE'), nullable=False)
    code = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    students = db.relationship('Student', backref='department', lazy='dynamic')

    def __repr__(self):
        return f'<Department {self.name}>'


class Student(db.Model):
    __tablename__ = 'students'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), unique=True, nullable=False)
    student_uid = db.Column(db.String(50), unique=True, nullable=False, index=True) # AM-INT-2026-001 / AM-STU-...
    dob = db.Column(db.String(20), nullable=True)
    gender = db.Column(db.String(30), nullable=True)
    college_id = db.Column(db.Integer, db.ForeignKey('colleges.id'), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'), nullable=False)
    roll_number = db.Column(db.String(50), nullable=False)
    degree = db.Column(db.String(150), nullable=False)
    current_year = db.Column(db.String(50), nullable=False)
    graduation_year = db.Column(db.String(10), nullable=False)
    aadhaar_masked = db.Column(db.String(30), nullable=True) # XXXX XXXX 4821
    is_verified = db.Column(db.Boolean, default=False)
    profile_completed = db.Column(db.Boolean, default=False, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    applications = db.relationship('Application', backref='student', lazy='dynamic', cascade='all, delete-orphan')
    internships = db.relationship('Internship', backref='student', lazy='dynamic')
    documents = db.relationship('Document', backref='student', lazy='dynamic', cascade='all, delete-orphan')
    payments = db.relationship('Payment', primaryjoin="foreign(Payment.student_id) == Student.id", lazy='dynamic')
    submissions = db.relationship('WeeklySubmission', back_populates='student', lazy='dynamic', cascade='all, delete-orphan')
    meetings = db.relationship('Meeting', back_populates='student', lazy='dynamic')
    attendance_records = db.relationship('AttendanceRecord', back_populates='student', lazy='dynamic', cascade='all, delete-orphan')
    leave_requests = db.relationship('LeaveRequest', back_populates='student', lazy='dynamic', cascade='all, delete-orphan')

    @property
    def active_internship(self):
        if hasattr(self, '_preloaded_active_internship'):
            return self._preloaded_active_internship
        return self.internships.order_by(Internship.id.desc()).first()

    @property
    def first_application(self):
        if hasattr(self, '_preloaded_application'):
            return self._preloaded_application
        return self.applications.first()

    @property
    def skills(self):
        return self.user.skills if self.user and self.user.skills else ''

    @skills.setter
    def skills(self, val):
        if self.user:
            self.user.skills = val

    @property
    def skills_list(self):
        s = self.skills
        if not s:
            return []
        return [x.strip() for x in s.split(',') if x.strip()]

    @property
    def full_name(self):
        return self.user.full_name if self.user and self.user.full_name else 'Intern'

    @property
    def email(self):
        return self.user.email if self.user else ''

    def __repr__(self):
        return f'<Student {self.student_uid} - {self.user.full_name if self.user else ""}>'


class InternshipPlan(db.Model):
    __tablename__ = 'internship_plans'

    id = db.Column(db.Integer, primary_key=True)
    plan_code = db.Column(db.String(50), unique=True, nullable=False) # 1_MONTH_PROJECT, 3_MONTH_PROFESSIONAL
    title = db.Column(db.String(100), nullable=False)
    duration_months = db.Column(db.Integer, nullable=False)
    fee = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), default='INR')
    description = db.Column(db.Text, nullable=False)
    features_json = db.Column(db.Text, nullable=False)
    badge = db.Column(db.String(50), nullable=True)
    is_active = db.Column(db.Boolean, default=True)

    applications = db.relationship('Application', backref='plan', lazy='dynamic')
    internships = db.relationship('Internship', backref='plan', lazy='dynamic')

    @property
    def features(self):
        try:
            return json.loads(self.features_json)
        except:
            return []

    def __repr__(self):
        return f'<InternshipPlan {self.title}>'


class Application(db.Model):
    __tablename__ = 'applications'

    id = db.Column(db.Integer, primary_key=True)
    application_no = db.Column(db.String(30), unique=True, nullable=False, index=True) # AM-APP-2026-0001
    # student_id is nullable: career applications exist BEFORE employee conversion
    student_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='CASCADE'), nullable=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('internship_plans.id'), nullable=True)
    status = db.Column(db.String(30), default='APPROVED') # DRAFT, SUBMITTED, PAYMENT_PENDING, PAID, DOCUMENT_PENDING, UNDER_REVIEW, VERIFIED, REJECTED, APPROVED
    consent_agreed = db.Column(db.Boolean, default=True)
    consent_version = db.Column(db.String(20), default='v1.0-2026')
    consent_timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    ip_address = db.Column(db.String(50), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Career application candidate fields (populated before student/employee onboarding)
    candidate_name = db.Column(db.String(150), nullable=True)
    candidate_email = db.Column(db.String(150), nullable=True)
    candidate_phone = db.Column(db.String(30), nullable=True)
    candidate_dob = db.Column(db.String(20), nullable=True)
    candidate_gender = db.Column(db.String(30), nullable=True)
    college_name = db.Column(db.String(200), nullable=True)
    department_name = db.Column(db.String(150), nullable=True)
    course = db.Column(db.String(150), nullable=True)
    year_of_study = db.Column(db.String(50), nullable=True)
    roll_number = db.Column(db.String(50), nullable=True)
    applied_role = db.Column(db.String(100), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    state = db.Column(db.String(100), nullable=True)
    aadhaar_masked = db.Column(db.String(30), nullable=True)
    # Employee conversion tracking
    is_converted_to_employee = db.Column(db.Boolean, default=False)
    converted_employee_id = db.Column(db.String(50), nullable=True)  # AM-INT-XXXX

    # Relationships
    payments = db.relationship('Payment', primaryjoin="foreign(Payment.application_id) == Application.id", lazy='dynamic')
    internship = db.relationship('Internship', backref='application', uselist=False)

    def __repr__(self):
        return f'<Application {self.application_no} - {self.status}>'


class Payment(db.Model):
    __tablename__ = 'payments'

    id = db.Column(db.Integer, primary_key=True)
    # Canonical Anti-Matrix Corporate fields:
    application_id = db.Column(db.Integer, nullable=False) # Foreign key to job_applications.id in production
    cashfree_order_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    cashfree_payment_session_id = db.Column(db.String(255), nullable=True)
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), default='INR', nullable=False)
    payment_status = db.Column(db.String(50), default='paid', nullable=False) # Canonical: 'paid', 'pending', 'failed'
    gateway = db.Column(db.String(50), default='cashfree', nullable=False)
    cf_payment_id = db.Column(db.String(100), nullable=True)
    gateway_response = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Additional backward-compatible columns used by Internship Portal:
    student_id = db.Column(db.Integer, nullable=True)
    transaction_id = db.Column(db.String(100), nullable=True, index=True)
    order_id = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(20), nullable=True) # Secondary alias for payment_status
    payment_method = db.Column(db.String(50), default='ONLINE', nullable=True)
    gateway_response_json = db.Column(db.Text, nullable=True)
    paid_at = db.Column(db.DateTime, nullable=True)

    # Relationships
    student = db.relationship('Student', primaryjoin="foreign(Payment.student_id) == Student.id", uselist=False, overlaps="payments")

    def __init__(self, **kwargs):
        # 1. Synchronize order_id and cashfree_order_id
        if 'order_id' in kwargs and 'cashfree_order_id' not in kwargs:
            kwargs['cashfree_order_id'] = kwargs['order_id']
        elif 'cashfree_order_id' in kwargs and 'order_id' not in kwargs:
            kwargs['order_id'] = kwargs['cashfree_order_id']

        # 2. Normalize and synchronize status and payment_status
        if 'status' in kwargs and 'payment_status' not in kwargs:
            st = str(kwargs['status']).lower()
            if st in ('successful', 'success', 'paid', 'completed'):
                kwargs['payment_status'] = 'paid'
            elif st in ('failed', 'rejected', 'cancelled'):
                kwargs['payment_status'] = 'failed'
            else:
                kwargs['payment_status'] = 'pending'
        elif 'payment_status' in kwargs and 'status' not in kwargs:
            ps = str(kwargs['payment_status']).lower()
            if ps in ('paid', 'success', 'successful'):
                kwargs['status'] = 'SUCCESSFUL'
            elif ps in ('failed', 'rejected'):
                kwargs['status'] = 'FAILED'
            else:
                kwargs['status'] = 'PENDING'

        # 3. Ensure mandatory defaults
        if 'gateway' not in kwargs:
            kwargs['gateway'] = 'cashfree'
        if 'currency' not in kwargs:
            kwargs['currency'] = 'INR'
        if 'updated_at' not in kwargs:
            kwargs['updated_at'] = datetime.utcnow()

        super().__init__(**kwargs)

    def __repr__(self):
        return f'<Payment {self.cashfree_order_id or self.transaction_id} - {self.payment_status}>'


@event.listens_for(Payment, 'before_insert')
@event.listens_for(Payment, 'before_update')
def sync_payment_order_ids(mapper, connection, target):
    """Ensure both order_id and cashfree_order_id are populated for cross-system Supabase compatibility."""
    if target.order_id and not target.cashfree_order_id:
        target.cashfree_order_id = target.order_id
    elif target.cashfree_order_id and not target.order_id:
        target.order_id = target.cashfree_order_id


@event.listens_for(Payment, 'before_insert')
@event.listens_for(Payment, 'before_update')
def sync_payment_status(mapper, connection, target):
    """Ensure canonical payment_status ('paid', 'pending', 'failed') and secondary status are synchronized."""
    if not target.payment_status:
        if target.status:
            st = str(target.status).lower()
            if st in ('successful', 'success', 'paid', 'completed'):
                target.payment_status = 'paid'
            elif st in ('failed', 'rejected', 'cancelled'):
                target.payment_status = 'failed'
            else:
                target.payment_status = 'pending'
        else:
            target.payment_status = 'paid'
    if not target.status:
        target.status = 'SUCCESSFUL' if str(target.payment_status).lower() in ('paid', 'success', 'successful') else ('FAILED' if str(target.payment_status).lower() in ('failed', 'rejected') else 'PENDING')
    if not target.gateway:
        target.gateway = 'cashfree'
    if not target.currency:
        target.currency = 'INR'
    if not target.updated_at:
        target.updated_at = datetime.utcnow()


class Internship(db.Model):
    __tablename__ = 'internships'

    id = db.Column(db.Integer, primary_key=True)
    internship_no = db.Column(db.String(30), unique=True, nullable=False, index=True) # AM-INT-2026-0001
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    application_id = db.Column(db.Integer, db.ForeignKey('applications.id'), nullable=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('internship_plans.id'), nullable=False)
    status = db.Column(db.String(20), default='ACTIVE') # NOT_STARTED, ACTIVE, ON_HOLD, COMPLETED, TERMINATED
    start_date = db.Column(db.String(20), nullable=False)
    end_date = db.Column(db.String(20), nullable=False)
    progress_percent = db.Column(db.Integer, default=0)
    current_stage = db.Column(db.String(50), default='Week 1 Understanding')
    mentor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    mentor = db.relationship('User', foreign_keys=[mentor_id])
    assignments = db.relationship('ProjectAssignment', backref='internship', lazy='dynamic', cascade='all, delete-orphan')
    issued_documents = db.relationship('IssuedDocument', backref='internship', lazy='dynamic')
    verification = db.relationship('CertificateVerification', backref='internship', uselist=False)
    meetings = db.relationship('Meeting', back_populates='internship', lazy='dynamic')
    attendance_records = db.relationship('AttendanceRecord', back_populates='internship', lazy='dynamic')
    leave_requests = db.relationship('LeaveRequest', back_populates='internship', lazy='dynamic')

    @property
    def assigned_mentor(self):
        """
        Returns the assigned mentor only if they are one of the approved mentors or a valid active mentor.
        Invalid or legacy mentors (e.g. Dr. Rajesh Sharma at mentor@antimatrix.com) will return None.
        """
        if not self.mentor:
            return None
        if self.mentor.email in (
            'praveen@antimatrix.co.in',
            'satishkumar@antimatrix.co.in',
            'rohit@antimatrix.co.in',
            'bharatbabu@antimatrix.co.in'
        ):
            return self.mentor
        if self.mentor.email == 'mentor@antimatrix.com' or 'Rajesh Sharma' in (self.mentor.full_name or ''):
            return None
        if self.mentor.role == 'mentor' and self.mentor.is_active:
            return self.mentor
        return None

    @property
    def assigned_mentor_name(self):
        """Returns the exact canonical name of the assigned mentor, or None."""
        m = self.assigned_mentor
        if not m:
            return None
        names = {
            'praveen@antimatrix.co.in': 'Praveen',
            'satishkumar@antimatrix.co.in': 'Satish Kumar',
            'rohit@antimatrix.co.in': 'Rohit',
            'bharatbabu@antimatrix.co.in': 'Bharath',
        }
        return names.get(m.email, m.full_name)

    @property
    def assigned_mentor_email(self):
        """
        Returns the official Anti-Matrix email of the assigned mentor, or None.
        Display-only resolution using canonical mentor mappings without modifying database.
        """
        name = self.assigned_mentor_name
        if not name:
            return None
        from app.services.mentor_service import get_canonical_mentor_email
        canon = get_canonical_mentor_email(name)
        if canon:
            return canon
        if self.mentor:
            return self.mentor.email
        return None

    @property
    def active_assignment(self):
        if hasattr(self, '_preloaded_active_assignment'):
            return self._preloaded_active_assignment
        return self.assignments.order_by(ProjectAssignment.id.desc()).first()

    def update_progress(self):
        assignment = self.active_assignment
        if assignment:
            milestones = assignment.weekly_milestones.all()
            if milestones:
                completed = sum(1 for m in milestones if m.status in ['COMPLETED', 'APPROVED'])
                self.progress_percent = int((completed / len(milestones)) * 100)
                active_m = next((m for m in milestones if m.status in ['AVAILABLE', 'IN_PROGRESS', 'SUBMITTED', 'UNDER_REVIEW', 'REDO_REQUIRED', 'REVISION_REQUIRED']), None)
                if active_m:
                    self.current_stage = f"Week {active_m.week_number} ({active_m.status.replace('_', ' ').title()})"
                elif completed == len(milestones):
                    self.current_stage = "All Milestones Completed"
                db.session.commit()

    def __repr__(self):
        return f'<Internship {self.internship_no} - {self.status}>'


class Document(db.Model):
    __tablename__ = 'documents'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    application_id = db.Column(db.Integer, db.ForeignKey('applications.id'), nullable=True)
    doc_type = db.Column(db.String(30), nullable=False) # AADHAAR, COLLEGE_ID, RESUME, PHOTO
    file_name = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(255), nullable=False)
    file_size = db.Column(db.Integer, nullable=False)
    mime_type = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(30), default='UPLOADED') # NOT_UPLOADED, UPLOADED, UNDER_REVIEW, APPROVED, REJECTED, RE_UPLOAD_REQUIRED
    rejection_reason = db.Column(db.Text, nullable=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    verified_at = db.Column(db.DateTime, nullable=True)
    verified_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    verifier = db.relationship('User', foreign_keys=[verified_by])

    def __repr__(self):
        return f'<Document {self.doc_type} - {self.status}>'


class Project(db.Model):
    __tablename__ = 'projects'

    id = db.Column(db.Integer, primary_key=True)
    project_code = db.Column(db.String(30), unique=True, nullable=False)
    title = db.Column(db.String(200), nullable=False)
    domain = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True, default='')
    problem_statement = db.Column(db.Text, nullable=True)
    expected_outcome = db.Column(db.Text, nullable=True)
    objectives_json = db.Column(db.Text, nullable=True, default='[]')
    tech_stack_json = db.Column(db.Text, nullable=True, default='[]')
    requirements_json = db.Column(db.Text, nullable=True, default='[]')
    instructions_md = db.Column(db.Text, nullable=True, default='')
    reference_links_json = db.Column(db.Text, nullable=True, default='[]')
    project_type = db.Column(db.String(100), nullable=True) # Real-Time AI/ML Web Application, etc.
    deployment = db.Column(db.String(100), nullable=True) # Localhost, Cloud, etc.
    cloud_required = db.Column(db.Boolean, default=False)
    paid_api_required = db.Column(db.Boolean, default=False)
    github_required = db.Column(db.Boolean, default=True)
    modules_json = db.Column(db.Text, nullable=True)
    restrictions_json = db.Column(db.Text, nullable=True)
    final_deliverable_json = db.Column(db.Text, nullable=True)
    evaluation_json = db.Column(db.Text, nullable=True)
    source_json = db.Column(db.Text, nullable=True)
    source_json_name = db.Column(db.String(255), nullable=True)
    duration_weeks = db.Column(db.Integer, default=4) # 4 or 12
    duration_months = db.Column(db.Integer, default=1) # 1 or 3
    difficulty = db.Column(db.String(50), default='Intermediate') # Beginner, Intermediate, Advanced, Easy to Medium, etc.
    max_capacity = db.Column(db.Integer, default=10, nullable=True) # Maximum intern allocation capacity
    is_archived = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    assignments = db.relationship('ProjectAssignment', backref='project', lazy='dynamic')
    project_weeks = db.relationship('ProjectWeek', backref='project', lazy='dynamic', cascade='all, delete-orphan', order_by='ProjectWeek.week_number.asc()')

    @property
    def problem_id(self):
        """Alias for project_code ensuring seamless Problem ID compatibility."""
        return self.project_code

    @problem_id.setter
    def problem_id(self, val):
        self.project_code = val

    @property
    def project_title(self):
        return self.title

    @project_title.setter
    def project_title(self, val):
        self.title = val

    @property
    def level(self):
        return self.difficulty

    @level.setter
    def level(self, val):
        self.difficulty = val

    @property
    def duration_display(self):
        return f"{self.duration_months} Month{'s' if self.duration_months > 1 else ''}"

    @property
    def status(self):
        if self.is_archived:
            return 'ARCHIVED'
        return 'ACTIVE' if self.is_active else 'INACTIVE'

    @status.setter
    def status(self, val):
        val_str = str(val).upper()
        if val_str == 'ARCHIVED':
            self.is_archived = True
            self.is_active = False
        elif val_str == 'ACTIVE':
            self.is_archived = False
            self.is_active = True
        elif val_str == 'INACTIVE':
            self.is_archived = False
            self.is_active = False
        elif isinstance(val, bool):
            self.is_active = val
            self.is_archived = False

    @property
    def active_interns_count(self):
        if hasattr(self, '_preloaded_active_interns_count'):
            return self._preloaded_active_interns_count
        try:
            from app.models import Internship, ProjectAssignment
            return self.assignments.filter(
                ProjectAssignment.status.in_(['ASSIGNED', 'IN_PROGRESS', 'SUBMITTED', 'REVISION_REQUIRED', 'UNDER_EVALUATION', 'ACTIVE'])
            ).join(Internship).filter(Internship.status == 'ACTIVE').count()
        except Exception:
            return 0

    @property
    def is_at_capacity(self):
        limit = self.max_capacity if (self.max_capacity is not None and self.max_capacity > 0) else 10
        return self.active_interns_count >= limit

    @property
    def remaining_capacity(self):
        limit = self.max_capacity if (self.max_capacity is not None and self.max_capacity > 0) else 10
        return max(0, limit - self.active_interns_count)

    @property
    def capacity_percent(self):
        limit = self.max_capacity if (self.max_capacity is not None and self.max_capacity > 0) else 10
        return min(100, int((self.active_interns_count / limit) * 100))

    @property
    def tech_stack_list(self):
        ts = self.tech_stack
        if isinstance(ts, list):
            return [str(x).strip() for x in ts if str(x).strip()]
        if isinstance(ts, str):
            return [x.strip() for x in ts.split(',') if x.strip()]
        return []

    @property
    def total_weeks(self):
        return self.duration_weeks or (4 if self.duration_months == 1 else 12)

    @property
    def total_tasks_count(self):
        try:
            return sum(w.tasks.count() for w in self.project_weeks.all())
        except Exception:
            return 0

    @property
    def active_colleges_count(self):
        """Count of distinct colleges with active assignments for this project."""
        if hasattr(self, '_preloaded_active_colleges_count'):
            return self._preloaded_active_colleges_count
        try:
            from app.models import Student, Internship, ProjectAssignment
            count = db.session.query(Student.college_id).join(
                Internship, Internship.student_id == Student.id
            ).join(
                ProjectAssignment, ProjectAssignment.internship_id == Internship.id
            ).filter(
                ProjectAssignment.project_id == self.id,
                ProjectAssignment.status.in_(['ASSIGNED', 'IN_PROGRESS', 'SUBMITTED', 'REVISION_REQUIRED', 'UNDER_EVALUATION', 'ACTIVE']),
                Internship.status == 'ACTIVE'
            ).distinct().count()
            return count
        except Exception:
            return 0

    @property
    def total_colleges_count(self):
        """Count of distinct colleges with any assignments for this project."""
        try:
            from app.models import Student, Internship, ProjectAssignment
            count = db.session.query(Student.college_id).join(
                Internship, Internship.student_id == Student.id
            ).join(
                ProjectAssignment, ProjectAssignment.internship_id == Internship.id
            ).filter(
                ProjectAssignment.project_id == self.id
            ).distinct().count()
            return count
        except Exception:
            return 0

    @property
    def objectives(self):
        try: return json.loads(self.objectives_json) if self.objectives_json else []
        except: return []

    @property
    def tech_stack(self):
        try: return json.loads(self.tech_stack_json) if self.tech_stack_json else []
        except: return []

    @property
    def requirements(self):
        try: return json.loads(self.requirements_json) if self.requirements_json else []
        except: return []

    @property
    def reference_links(self):
        try: return json.loads(self.reference_links_json) if self.reference_links_json else []
        except: return []

    @property
    def modules(self):
        try: return json.loads(self.modules_json) if self.modules_json else []
        except: return []

    @property
    def restrictions(self):
        try: return json.loads(self.restrictions_json) if self.restrictions_json else []
        except: return []

    @property
    def final_deliverable(self):
        try: return json.loads(self.final_deliverable_json) if self.final_deliverable_json else {}
        except: return {}

    @property
    def evaluation(self):
        try: return json.loads(self.evaluation_json) if self.evaluation_json else {}
        except: return {}


class ProjectWeek(db.Model):
    """Reusable project template week structure."""
    __tablename__ = 'project_weeks'
    __table_args__ = (db.UniqueConstraint('project_id', 'week_number', name='uq_project_week'),)

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False)
    week_number = db.Column(db.Integer, nullable=False)  # 1..4 or 1..12
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    objective = db.Column(db.Text, nullable=True)
    instructions = db.Column(db.Text, nullable=True)
    deliverables_json = db.Column(db.Text, nullable=True)

    # Extended metadata from task plan JSON
    phase_number = db.Column(db.Integer, nullable=True) # 1..4 for 3-Month projects
    phase_title = db.Column(db.String(200), nullable=True)
    weeks_label = db.Column(db.String(50), nullable=True) # "1-3", "4-6", etc.
    completion_percentage = db.Column(db.String(20), nullable=True) # "25%", "50%", etc.
    goal = db.Column(db.Text, nullable=True)
    expected_features_json = db.Column(db.Text, nullable=True)
    demo_output_json = db.Column(db.Text, nullable=True)
    github_requirement = db.Column(db.Text, nullable=True)
    completion_condition = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    tasks = db.relationship('ProjectTask', backref='week', lazy='dynamic', cascade='all, delete-orphan', order_by='ProjectTask.order_num.asc(), ProjectTask.id.asc()')

    @property
    def deliverables(self):
        try:
            return json.loads(self.deliverables_json) if self.deliverables_json else []
        except:
            return []

    @property
    def reference_resources(self):
        try:
            return json.loads(self.reference_resources_json) if self.reference_resources_json else []
        except:
            return []

    @property
    def expected_features(self):
        try:
            return json.loads(self.expected_features_json) if self.expected_features_json else []
        except:
            return []

    @property
    def demo_output(self):
        try:
            return json.loads(self.demo_output_json) if self.demo_output_json else {}
        except:
            return {}

    def __repr__(self):
        return f'<ProjectWeek Week {self.week_number}: {self.title}>'


class ProjectTask(db.Model):
    """Reusable task template belonging to a ProjectWeek."""
    __tablename__ = 'project_tasks'

    id = db.Column(db.Integer, primary_key=True)
    week_id = db.Column(db.Integer, db.ForeignKey('project_weeks.id', ondelete='CASCADE'), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    instructions = db.Column(db.Text, nullable=True)
    expected_output = db.Column(db.Text, nullable=True)
    priority = db.Column(db.String(20), default='Medium')  # High, Medium, Low
    estimated_hours = db.Column(db.Float, nullable=True)
    order_num = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<ProjectTask {self.title} ({self.priority})>'


class TaskImportHistory(db.Model):
    """Audit log of all uploaded JSON task plans."""
    __tablename__ = 'task_import_history'

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id', ondelete='SET NULL'), nullable=True)
    project_code = db.Column(db.String(50), nullable=True) # Problem ID
    project_title = db.Column(db.String(200), nullable=False)
    domain = db.Column(db.String(100), nullable=False)
    duration_months = db.Column(db.Integer, nullable=False)
    total_weeks = db.Column(db.Integer, default=4)
    total_tasks = db.Column(db.Integer, default=0)
    imported_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    status = db.Column(db.String(30), default='IMPORTED') # IMPORTED, UPDATED, FAILED
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    importer = db.relationship('User', foreign_keys=[imported_by])
    project = db.relationship('Project', foreign_keys=[project_id])

    @property
    def duration_display(self):
        return f"{self.duration_months} Month{'s' if self.duration_months > 1 else ''}"

    def __repr__(self):
        return f'<TaskImportHistory {self.filename} -> {self.project_code} ({self.status})>'


class ProjectAssignment(db.Model):
    __tablename__ = 'project_assignments'

    id = db.Column(db.Integer, primary_key=True)
    internship_id = db.Column(db.Integer, db.ForeignKey('internships.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    assigned_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    assigned_at = db.Column(db.DateTime, default=datetime.utcnow)
    deadline = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(30), default='IN_PROGRESS') # ASSIGNED, IN_PROGRESS, SUBMITTED, REVISION_REQUIRED, UNDER_EVALUATION, PASSED, FAILED, COMPLETED
    repo_url = db.Column(db.String(255), nullable=True)
    live_demo_url = db.Column(db.String(255), nullable=True)
    zip_path = db.Column(db.String(255), nullable=True)
    docs_path = db.Column(db.String(255), nullable=True)
    demo_video_url = db.Column(db.String(255), nullable=True)
    description = db.Column(db.Text, nullable=True)
    tech_used = db.Column(db.String(255), nullable=True)
    challenges = db.Column(db.Text, nullable=True)
    remarks = db.Column(db.Text, nullable=True)
    override_reason = db.Column(db.Text, nullable=True) # Admin override rationale for low match or capacity
    submitted_at = db.Column(db.DateTime, nullable=True)

    assigner = db.relationship('User', foreign_keys=[assigned_by])
    evaluation = db.relationship('Evaluation', backref='assignment', uselist=False)
    weekly_milestones = db.relationship('WeeklyMilestone', backref='assignment', lazy='dynamic', cascade='all, delete-orphan', order_by='WeeklyMilestone.week_number.asc()')

    @property
    def progress_percent(self):
        milestones = self.weekly_milestones.all()
        if not milestones:
            return 0
        completed = sum(1 for m in milestones if m.status in ['COMPLETED', 'APPROVED'])
        return int((completed / len(milestones)) * 100)

    @property
    def current_milestone(self):
        milestones = self.weekly_milestones.all()
        for m in milestones:
            if m.status in ['AVAILABLE', 'IN_PROGRESS', 'SUBMITTED', 'UNDER_REVIEW', 'REVISION_REQUIRED']:
                return m
        return milestones[-1] if milestones else None


class WeeklyMilestone(db.Model):
    __tablename__ = 'weekly_milestones'

    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey('project_assignments.id', ondelete='CASCADE'), nullable=False)
    week_number = db.Column(db.Integer, nullable=False) # 1, 2, 3, 4, ...
    title = db.Column(db.String(200), nullable=False)
    objective = db.Column(db.Text, nullable=False)
    instructions = db.Column(db.Text, nullable=True)
    deliverables_json = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(30), default='LOCKED') # LOCKED, AVAILABLE, IN_PROGRESS, SUBMITTED, UNDER_REVIEW, APPROVED, REDO_REQUIRED, REVISION_REQUIRED, COMPLETED
    started_at = db.Column(db.DateTime, nullable=True)
    due_at = db.Column(db.DateTime, nullable=True)
    unlocked_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    admin_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Extended Task Management attributes (Phase 2 Module 5)
    technical_requirements = db.Column(db.Text, nullable=True)
    reference_resources_json = db.Column(db.Text, nullable=True)
    rubric_id = db.Column(db.Integer, db.ForeignKey('evaluation_rubrics.id', ondelete='SET NULL'), nullable=True)

    # Relationships
    tasks = db.relationship('WeeklyTask', backref='milestone', lazy='dynamic', cascade='all, delete-orphan', order_by='WeeklyTask.order_num.asc()')
    submissions = db.relationship('WeeklySubmission', backref='milestone', lazy='dynamic', cascade='all, delete-orphan', order_by='WeeklySubmission.id.desc()')
    evaluations = db.relationship('Evaluation', backref='milestone', lazy='dynamic')
    meetings = db.relationship('Meeting', backref='milestone', lazy='dynamic')
    approver = db.relationship('User', foreign_keys=[approved_by])
    rubric = db.relationship('EvaluationRubric', foreign_keys=[rubric_id])

    @property
    def deliverables(self):
        try:
            return json.loads(self.deliverables_json) if self.deliverables_json else []
        except:
            return []

    @property
    def reference_resources(self):
        try:
            return json.loads(self.reference_resources_json) if self.reference_resources_json else []
        except:
            return []

    @property
    def latest_submission(self):
        return self.submissions.first()

    @property
    def is_locked(self):
        return self.status == 'LOCKED'

    @property
    def is_overdue(self):
        if self.due_at and self.status not in ['COMPLETED', 'APPROVED', 'SUBMITTED', 'UNDER_REVIEW']:
            return datetime.utcnow() > self.due_at
        return False

    @property
    def display_status(self):
        norm = (self.status or '').upper()
        if norm in ['COMPLETED', 'APPROVED']:
            return 'COMPLETED'
        if norm in ['SUBMITTED', 'UNDER_REVIEW']:
            return 'UNDER_REVIEW'
        if norm in ['REDO_REQUIRED', 'REVISION_REQUIRED']:
            return 'REDO_REQUIRED'
        if norm == 'LOCKED':
            return 'LOCKED'
        if norm in ['AVAILABLE', 'IN_PROGRESS']:
            if self.is_overdue:
                return 'OVERDUE'
            return norm
        return norm

    @property
    def all_tasks_completed(self):
        if hasattr(self, '_preloaded_total_tasks_count') and hasattr(self, '_preloaded_completed_tasks_count'):
            if self._preloaded_total_tasks_count == 0:
                return True
            return self._preloaded_completed_tasks_count == self._preloaded_total_tasks_count
        task_list = self.tasks.all()
        if not task_list:
            return True
        return all(t.is_completed for t in task_list)

    @property
    def completed_tasks_count(self):
        if hasattr(self, '_preloaded_completed_tasks_count'):
            return self._preloaded_completed_tasks_count
        return sum(1 for t in self.tasks.all() if t.is_completed)

    @property
    def total_tasks_count(self):
        if hasattr(self, '_preloaded_total_tasks_count'):
            return self._preloaded_total_tasks_count
        return self.tasks.count()

    @property
    def is_available(self):
        return self.status in ['AVAILABLE', 'IN_PROGRESS', 'REVISION_REQUIRED', 'REDO_REQUIRED', 'SUBMITTED', 'UNDER_REVIEW']

    @property
    def is_completed(self):
        return self.status in ['COMPLETED', 'APPROVED']

    def __repr__(self):
        return f'<WeeklyMilestone Week {self.week_number}: {self.title} ({self.status})>'


class WeeklyTask(db.Model):
    __tablename__ = 'weekly_tasks'

    id = db.Column(db.Integer, primary_key=True)
    milestone_id = db.Column(db.Integer, db.ForeignKey('weekly_milestones.id', ondelete='CASCADE'), nullable=False)
    task_text = db.Column(db.String(255), nullable=False)
    is_completed = db.Column(db.Boolean, default=False)
    order_num = db.Column(db.Integer, default=1)

    # Extended Task Management attributes (Phase 2 Module 5)
    description = db.Column(db.Text, nullable=True)
    technical_requirements = db.Column(db.Text, nullable=True)
    reference_resources = db.Column(db.Text, nullable=True)
    priority = db.Column(db.String(20), default='Medium') # High, Medium, Low
    status = db.Column(db.String(30), default='ASSIGNED') # ASSIGNED, IN_PROGRESS, COMPLETED
    due_date = db.Column(db.DateTime, nullable=True)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='SET NULL'), nullable=True)

    assigned_to = db.relationship('Student', foreign_keys=[assigned_to_id])

    @property
    def title(self):
        return self.task_text

    @title.setter
    def title(self, value):
        self.task_text = value

    def __repr__(self):
        return f'<WeeklyTask {self.task_text} ({self.status})>'


class WeeklySubmission(db.Model):
    __tablename__ = 'weekly_submissions'

    id = db.Column(db.Integer, primary_key=True)
    milestone_id = db.Column(db.Integer, db.ForeignKey('weekly_milestones.id', ondelete='CASCADE'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    repo_url = db.Column(db.String(255), nullable=True)
    github_url = db.Column(db.String(255), nullable=True)
    live_demo_url = db.Column(db.String(255), nullable=True)
    demo_video_url = db.Column(db.String(255), nullable=True)
    demo_video_path = db.Column(db.String(255), nullable=True)
    file_path = db.Column(db.String(255), nullable=True)
    submission_notes = db.Column(db.Text, nullable=True)
    implementation_summary = db.Column(db.Text, nullable=True) # Phase 2 Module 5
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    status = db.Column(db.String(30), default='SUBMITTED') # SUBMITTED, UNDER_REVIEW, APPROVED, REDO_REQUIRED, REVISION_REQUIRED, COMPLETED
    review_feedback = db.Column(db.Text, nullable=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    student = db.relationship('Student', foreign_keys=[student_id], back_populates='submissions')
    reviewer = db.relationship('User', foreign_keys=[reviewed_by])
    evaluations = db.relationship('Evaluation', backref='submission', lazy='dynamic', cascade='all, delete-orphan', order_by='Evaluation.id.desc()')

    @property
    def effective_repo_url(self):
        return self.github_url or self.repo_url or ''

    @property
    def effective_video_path(self):
        return self.demo_video_path or self.file_path or self.demo_video_url or ''

    @property
    def admin_remarks(self):
        return self.review_feedback or (self.milestone.admin_notes if self.milestone else '')

    def __repr__(self):
        return f'<WeeklySubmission Milestone={self.milestone_id} Status={self.status}>'


class Meeting(db.Model):
    __tablename__ = 'meetings'

    id = db.Column(db.Integer, primary_key=True)
    milestone_id = db.Column(db.Integer, db.ForeignKey('weekly_milestones.id'), nullable=True)
    internship_id = db.Column(db.Integer, db.ForeignKey('internships.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    host_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    meeting_date = db.Column(db.String(30), nullable=False)
    meeting_time = db.Column(db.String(30), nullable=False)
    meeting_link = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(30), default='SCHEDULED') # SCHEDULED, COMPLETED, RESCHEDULED, CANCELLED
    meeting_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship('Student', foreign_keys=[student_id], back_populates='meetings')
    internship = db.relationship('Internship', foreign_keys=[internship_id], back_populates='meetings')
    host = db.relationship('User', foreign_keys=[host_id])

    def __repr__(self):
        return f'<Meeting {self.title} ({self.status})>'


class EvaluationRubric(db.Model):
    """
    Enterprise Evaluation Rubric with 5 configurable criteria:
    - Technical implementation
    - Code quality
    - Functionality
    - Documentation
    - Presentation
    Enforces that the sum of all weights equals exactly 100%.
    """
    __tablename__ = 'evaluation_rubrics'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), default='Standard Evaluation Rubric')
    description = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    is_default = db.Column(db.Boolean, default=True)

    # 5 standard criteria weights (must total 100%)
    weight_technical = db.Column(db.Integer, default=25)      # Technical Implementation
    weight_code_quality = db.Column(db.Integer, default=25)   # Code Quality
    weight_functionality = db.Column(db.Integer, default=25)  # Functionality
    weight_documentation = db.Column(db.Integer, default=15)  # Documentation
    weight_presentation = db.Column(db.Integer, default=10)   # Presentation

    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    creator = db.relationship('User', foreign_keys=[created_by])

    @property
    def total_weight(self):
        return (
            (self.weight_technical or 0) +
            (self.weight_code_quality or 0) +
            (self.weight_functionality or 0) +
            (self.weight_documentation or 0) +
            (self.weight_presentation or 0)
        )

    @property
    def is_valid_100(self):
        return self.total_weight == 100

    @classmethod
    def get_active(cls):
        """Retrieve active default rubric, or create standard 100% rubric if missing."""
        rubric = cls.query.filter_by(is_active=True, is_default=True).first()
        if not rubric:
            rubric = cls.query.filter_by(is_active=True).first()
        if not rubric:
            rubric = cls(
                name='Standard Evaluation Rubric',
                description='5-criteria weighted rubric (100% total)',
                weight_technical=25,
                weight_code_quality=25,
                weight_functionality=25,
                weight_documentation=15,
                weight_presentation=10,
                is_active=True,
                is_default=True
            )
            db.session.add(rubric)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        return rubric

    def calculate_score(self, technical, code_quality, functionality, documentation, presentation):
        """Calculates consistent weighted score (0-100) based on criterion inputs (0-100 each)."""
        tech = max(0, min(100, float(technical if technical is not None else 0)))
        code = max(0, min(100, float(code_quality if code_quality is not None else 0)))
        func = max(0, min(100, float(functionality if functionality is not None else 0)))
        doc = max(0, min(100, float(documentation if documentation is not None else 0)))
        pres = max(0, min(100, float(presentation if presentation is not None else 0)))

        w_tech = float(self.weight_technical or 0)
        w_code = float(self.weight_code_quality or 0)
        w_func = float(self.weight_functionality or 0)
        w_doc = float(self.weight_documentation or 0)
        w_pres = float(self.weight_presentation or 0)

        total_weight = w_tech + w_code + w_func + w_doc + w_pres
        if total_weight <= 0:
            total_weight = 100.0

        weighted_sum = (
            (tech * w_tech) +
            (code * w_code) +
            (func * w_func) +
            (doc * w_doc) +
            (pres * w_pres)
        )
        return int(round(weighted_sum / (total_weight / 100.0) / 100.0))

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'weight_technical': self.weight_technical,
            'weight_code_quality': self.weight_code_quality,
            'weight_functionality': self.weight_functionality,
            'weight_documentation': self.weight_documentation,
            'weight_presentation': self.weight_presentation,
            'total_weight': self.total_weight,
            'is_valid_100': self.is_valid_100
        }

    def __repr__(self):
        return f'<EvaluationRubric {self.name} Total={self.total_weight}%>'


class Evaluation(db.Model):
    __tablename__ = 'evaluations'

    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey('project_assignments.id', ondelete='CASCADE'), nullable=True)
    submission_id = db.Column(db.Integer, db.ForeignKey('weekly_submissions.id', ondelete='CASCADE'), nullable=True)
    milestone_id = db.Column(db.Integer, db.ForeignKey('weekly_milestones.id', ondelete='CASCADE'), nullable=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='CASCADE'), nullable=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    evaluator_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    action = db.Column(db.String(50), nullable=True) # REDO, VERIFY, PROCEED_TO_NEXT
    remarks = db.Column(db.Text, nullable=True)

    # 5 standard criteria scores (Phase 2 Module 5)
    technical_score = db.Column(db.Integer, default=0)
    code_quality_score = db.Column(db.Integer, default=0)
    functionality_score = db.Column(db.Integer, default=0)
    documentation_score = db.Column(db.Integer, default=0)
    presentation_score = db.Column(db.Integer, default=0)

    # Legacy scores for backward compatibility
    ui_ux_score = db.Column(db.Integer, default=0)
    demo_score = db.Column(db.Integer, default=0)
    originality_score = db.Column(db.Integer, default=0)

    total_score = db.Column(db.Integer, default=0) # max 100
    rubric_id = db.Column(db.Integer, db.ForeignKey('evaluation_rubrics.id', ondelete='SET NULL'), nullable=True)
    rubric_snapshot_json = db.Column(db.Text, nullable=True)

    result = db.Column(db.String(30), nullable=True) # PASSED, NEEDS_IMPROVEMENT, FAILED, REDO_REQUIRED, VERIFIED
    feedback = db.Column(db.Text, nullable=True)
    revision_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    evaluated_at = db.Column(db.DateTime, default=datetime.utcnow)

    admin = db.relationship('User', foreign_keys=[admin_id])
    evaluator = db.relationship('User', foreign_keys=[evaluator_id])
    employee = db.relationship('Student', foreign_keys=[employee_id])
    rubric = db.relationship('EvaluationRubric', foreign_keys=[rubric_id])


class IssuedDocument(db.Model):
    __tablename__ = 'issued_documents'

    id = db.Column(db.Integer, primary_key=True)
    document_no = db.Column(db.String(50), unique=True, nullable=False, index=True) # AM-OFFER-..., AM-CERT-...
    internship_id = db.Column(db.Integer, db.ForeignKey('internships.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    doc_type = db.Column(db.String(30), nullable=False) # OFFER_LETTER, JOINING_LETTER, CERTIFICATE, EXPERIENCE_LETTER
    template_version = db.Column(db.String(20), default='v1.0')
    file_path = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), default='DRAFT') # DRAFT, GENERATED, ISSUED, REVOKED
    issued_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    issued_at = db.Column(db.DateTime, default=datetime.utcnow)
    revocation_reason = db.Column(db.Text, nullable=True)

    issuer = db.relationship('User', foreign_keys=[issued_by])
    student = db.relationship('Student', foreign_keys=[student_id])

    @property
    def display_type(self):
        mapping = {
            'OFFER_LETTER': 'Official Offer Letter',
            'JOINING_LETTER': 'Official Joining Letter',
            'CERTIFICATE': 'Completion Certificate',
            'EXPERIENCE_LETTER': 'Experience Letter',
            'INTERNSHIP_AGREEMENT': 'Internship Agreement'
        }
        return mapping.get(self.doc_type, self.doc_type.replace('_', ' ').title())

    @property
    def is_active(self):
        return self.status in ['ISSUED', 'GENERATED']

    @property
    def is_revoked(self):
        return self.status == 'REVOKED'

    def __repr__(self):
        return f'<IssuedDocument {self.document_no} - {self.doc_type} - {self.status}>'


class CertificateVerification(db.Model):
    __tablename__ = 'certificate_verifications'

    id = db.Column(db.Integer, primary_key=True)
    certificate_no = db.Column(db.String(50), unique=True, nullable=False, index=True)
    internship_id = db.Column(db.Integer, db.ForeignKey('internships.id'), nullable=False)
    student_name = db.Column(db.String(100), nullable=False)
    program_title = db.Column(db.String(150), nullable=False)
    duration = db.Column(db.String(50), nullable=False)
    start_date = db.Column(db.String(20), nullable=False)
    end_date = db.Column(db.String(20), nullable=False)
    issue_date = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(50), default='Successfully Completed') # Successfully Completed, Revoked
    qr_code_data = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def is_valid(self):
        return self.status in ['Successfully Completed', 'VALID', 'VERIFIED']

    @property
    def is_revoked(self):
        return self.status in ['Revoked', 'REVOKED']

    def __repr__(self):
        return f'<CertificateVerification {self.certificate_no} - {self.student_name} - {self.status}>'


class Notification(db.Model):
    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    title = db.Column(db.String(150), nullable=False)
    message = db.Column(db.Text, nullable=False)
    type = db.Column(db.String(30), default='INFO') # INFO, SUCCESS, WARNING, ACTION_REQUIRED
    link = db.Column(db.String(255), nullable=True)
    category = db.Column(db.String(50), default='GENERAL', nullable=True) # GENERAL, TASK, DEADLINE, SUBMISSION, ALLOCATION, ANNOUNCEMENT, EVALUATION, COMPLETION
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class NotificationPreference(db.Model):
    """
    User notification preferences model (Module 7).
    Allows granular control over email and in-portal alerts.
    """
    __tablename__ = 'notification_preferences'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), unique=True, nullable=False, index=True)
    email_enabled = db.Column(db.Boolean, default=True, nullable=False)
    in_portal_enabled = db.Column(db.Boolean, default=True, nullable=False)
    deadline_reminders = db.Column(db.Boolean, default=True, nullable=False)
    announcements = db.Column(db.Boolean, default=True, nullable=False)
    evaluations = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def to_dict(self):
        return {
            'email_enabled': self.email_enabled,
            'in_portal_enabled': self.in_portal_enabled,
            'deadline_reminders': self.deadline_reminders,
            'announcements': self.announcements,
            'evaluations': self.evaluations,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class Announcement(db.Model):
    """
    Central broadcast announcements model (Module 7).
    Published by administrators or mentors to target roles, tracks, or projects.
    """
    __tablename__ = 'announcements'

    id = db.Column(db.Integer, primary_key=True)
    author_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    target_role = db.Column(db.String(30), default='ALL', nullable=False) # ALL, STUDENT, MENTOR
    department = db.Column(db.String(100), nullable=True) # Optional department filter
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True) # Optional project filter
    priority = db.Column(db.String(20), default='NORMAL', nullable=False) # NORMAL, URGENT
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    author = db.relationship('User', foreign_keys=[author_id], backref=db.backref('authored_announcements', lazy='dynamic'))
    project = db.relationship('Project', backref=db.backref('announcements', lazy='dynamic'))
    reads = db.relationship('AnnouncementRead', backref='announcement', cascade='all, delete-orphan', lazy='dynamic')

    @property
    def read_count(self):
        return self.reads.count()

    def is_read_by(self, user_id):
        if not user_id:
            return False
        return self.reads.filter_by(user_id=user_id).first() is not None


class AnnouncementRead(db.Model):
    """
    Tracks which users have acknowledged/read each announcement (Module 7).
    """
    __tablename__ = 'announcement_reads'

    id = db.Column(db.Integer, primary_key=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey('announcements.id', ondelete='CASCADE'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    read_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('announcement_id', 'user_id', name='uq_announcement_user_read'),
    )

    user = db.relationship('User', backref=db.backref('announcement_reads', lazy='dynamic'))


class AuditLog(db.Model):
    __tablename__ = 'audit_logs'

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    actor_role = db.Column(db.String(30), default='SYSTEM')
    action = db.Column(db.String(100), nullable=False)
    target_entity = db.Column(db.String(50), nullable=False)
    target_id = db.Column(db.String(50), nullable=True)
    details_json = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class JobApplication(db.Model):
    """
    Anti-Matrix Corporate Job Application model (shared PostgreSQL database).
    Read-only mapping used by Internship Portal to resolve employee details and duration.
    """
    __tablename__ = 'job_applications'

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, nullable=True)
    user_id = db.Column(db.Integer, nullable=True)
    application_code = db.Column(db.String(50), nullable=True)
    first_name = db.Column(db.String(100), nullable=True)
    last_name = db.Column(db.String(100), nullable=True)
    full_name = db.Column(db.String(150), nullable=True)
    email = db.Column(db.String(150), nullable=True)
    phone = db.Column(db.String(30), nullable=True)
    duration = db.Column(db.String(50), nullable=True)  # '1_month', '3_months'
    college = db.Column(db.String(200), nullable=True)
    department = db.Column(db.String(150), nullable=True)
    degree = db.Column(db.String(150), nullable=True)
    graduation_year = db.Column(db.String(10), nullable=True)
    payment_status = db.Column(db.String(50), nullable=True)
    status = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<JobApplication id={self.id} code='{self.application_code}' email='{self.email}'>"


class Employee(db.Model):
    """
    Anti-Matrix Corporate Employee model (shared PostgreSQL database).
    Authoritative source for Employee ID login credentials and temporary password lifecycle.
    """
    __tablename__ = 'employees'

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.String(20), unique=True, nullable=False, index=True)
    application_id = db.Column(db.Integer, nullable=True, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    temp_password_encrypted = db.Column(db.String(500), nullable=True)
    temporary_password_active = db.Column(db.Boolean, default=True, nullable=False)
    account_status = db.Column(db.String(30), default='active', nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    application = db.relationship(
        'JobApplication',
        primaryjoin="foreign(Employee.application_id) == JobApplication.id",
        uselist=False
    )

    def check_password(self, password: str) -> bool:
        """Verifies candidate password against stored secure Werkzeug hash."""
        if not self.password_hash or not password:
            return False
        try:
            return check_password_hash(self.password_hash, password)
        except Exception:
            return False

    def set_password(self, password: str):
        """Hashes plaintext password using Werkzeug secure password hashing."""
        self.password_hash = generate_password_hash(password)

    def reset_password(self, new_password: str):
        """
        Updates password hash upon password change/reset.
        Marks temporary_password_active = False and purges temp_password_encrypted.
        Also marks onboarding_credential as RESET if present.
        """
        self.set_password(new_password)
        self.temporary_password_active = False
        self.temp_password_encrypted = None
        self.updated_at = datetime.utcnow()
        if hasattr(self, 'onboarding_credential') and self.onboarding_credential:
            self.onboarding_credential.mark_reset()

    def __repr__(self):
        return f"<Employee id={self.id} employee_id='{self.employee_id}' status='{self.account_status}'>"


class EmployeeOnboardingCredential(db.Model):
    """
    Dedicated table for the temporary employee onboarding credential lifecycle.
    Stores temporary password encrypted at rest and one-way password hash for verification.
    Shared with the Anti-Matrix platform via Supabase.
    """
    __tablename__ = 'employee_onboarding_credentials'

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(
        db.String(20),
        db.ForeignKey('employees.employee_id', ondelete='CASCADE'),
        unique=True,
        nullable=False,
        index=True
    )
    temporary_password_encrypted = db.Column(db.Text, nullable=True)
    temporary_password_hash = db.Column(db.String(256), nullable=False)
    status = db.Column(db.String(20), default='ACTIVE', nullable=False, index=True)  # 'ACTIVE', 'RESET', 'EXPIRED'
    password_reset_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    # Relationships
    employee = db.relationship('Employee', backref=db.backref('onboarding_credential', uselist=False, cascade='all, delete-orphan'))

    @property
    def is_active(self) -> bool:
        """Returns True if the temporary credential is still active."""
        return self.status == 'ACTIVE'

    def verify_password(self, candidate_password: str) -> bool:
        """
        Verifies candidate password against temporary_password_hash for initial portal activation.
        Strictly returns False if credential status is not ACTIVE.
        """
        if self.status != 'ACTIVE' or not self.temporary_password_hash or not candidate_password:
            return False
        try:
            return check_password_hash(self.temporary_password_hash, candidate_password)
        except Exception:
            return False

    def mark_reset(self):
        """
        Transitions temporary credential status to RESET upon successful password change.
        Permanently invalidates and purges the encrypted temporary password.
        """
        self.status = 'RESET'
        self.password_reset_at = datetime.now(timezone.utc)
        self.temporary_password_encrypted = None
        self.updated_at = datetime.now(timezone.utc)

    def to_dict(self):
        return {
            'id': self.id,
            'employee_id': self.employee_id,
            'status': self.status,
            'password_reset_at': self.password_reset_at.isoformat() if self.password_reset_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f"<EmployeeOnboardingCredential id={self.id} employee_id='{self.employee_id}' status='{self.status}'>"


# ─── MODULE 9: ATTENDANCE, LEAVE & INTERNSHIP SCHEDULING MODELS ─────────────

class AttendanceRecord(db.Model):
    """
    Daily attendance record for an intern (Phase 2 Module 9).
    Enforces that an intern can only have one attendance record per day.
    """
    __tablename__ = 'attendance_records'
    __table_args__ = (
        db.UniqueConstraint('student_id', 'date', name='uq_attendance_student_date'),
    )

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='CASCADE'), nullable=False, index=True)
    internship_id = db.Column(db.Integer, db.ForeignKey('internships.id', ondelete='CASCADE'), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    check_in_time = db.Column(db.DateTime, nullable=True)
    check_out_time = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default='PRESENT', nullable=False) # PRESENT, LATE, HALF_DAY, ABSENT, ON_LEAVE, HOLIDAY
    work_duration_minutes = db.Column(db.Integer, default=0, nullable=False)
    ip_address = db.Column(db.String(50), nullable=True)
    check_in_notes = db.Column(db.Text, nullable=True)
    is_corrected = db.Column(db.Boolean, default=False, nullable=False)
    correction_reason = db.Column(db.Text, nullable=True)
    corrected_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    corrected_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    student = db.relationship('Student', back_populates='attendance_records')
    internship = db.relationship('Internship', back_populates='attendance_records')
    corrector = db.relationship('User', foreign_keys=[corrected_by])

    @property
    def work_duration_hours(self):
        return round((self.work_duration_minutes or 0) / 60.0, 1)

    @property
    def work_duration_formatted(self):
        mins = self.work_duration_minutes or 0
        h = mins // 60
        m = mins % 60
        return f"{h}h {m}m"

    @property
    def display_status(self):
        return (self.status or 'PRESENT').replace('_', ' ').title()

    def __repr__(self):
        return f"<AttendanceRecord student_id={self.student_id} date='{self.date}' status='{self.status}'>"


class LeaveRequest(db.Model):
    """
    Leave application and approval tracking (Phase 2 Module 9).
    """
    __tablename__ = 'leave_requests'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='CASCADE'), nullable=False, index=True)
    internship_id = db.Column(db.Integer, db.ForeignKey('internships.id', ondelete='CASCADE'), nullable=False, index=True)
    leave_type = db.Column(db.String(30), nullable=False) # SICK_LEAVE, CASUAL_LEAVE, ACADEMIC_LEAVE, EMERGENCY_LEAVE
    start_date = db.Column(db.Date, nullable=False, index=True)
    end_date = db.Column(db.Date, nullable=False, index=True)
    days_count = db.Column(db.Integer, default=1, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    supporting_document_path = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(20), default='PENDING', nullable=False) # PENDING, APPROVED, REJECTED, CANCELLED
    reviewer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewer_remarks = db.Column(db.Text, nullable=True)
    cancellation_reason = db.Column(db.Text, nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship('Student', back_populates='leave_requests')
    internship = db.relationship('Internship', back_populates='leave_requests')
    reviewer = db.relationship('User', foreign_keys=[reviewer_id])

    @property
    def display_type(self):
        return (self.leave_type or 'CASUAL_LEAVE').replace('_', ' ').title()

    @property
    def is_pending(self):
        return self.status == 'PENDING'

    @property
    def is_approved(self):
        return self.status == 'APPROVED'

    @property
    def is_rejected(self):
        return self.status == 'REJECTED'

    @property
    def is_cancelled(self):
        return self.status == 'CANCELLED'

    def __repr__(self):
        return f"<LeaveRequest student_id={self.student_id} type='{self.leave_type}' {self.start_date}->{self.end_date} status='{self.status}'>"


class AttendancePolicy(db.Model):
    """
    Configurable organizational attendance policy and thresholds (Phase 2 Module 9).
    """
    __tablename__ = 'attendance_policies'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), default='Anti-Matrix Standard Engineering Attendance Policy', nullable=False)
    work_start_time = db.Column(db.String(10), default='09:00', nullable=False) # HH:MM (24-hour)
    work_end_time = db.Column(db.String(10), default='17:00', nullable=False)   # HH:MM
    late_threshold_time = db.Column(db.String(10), default='09:30', nullable=False) # Check-in after this is marked LATE
    half_day_min_hours = db.Column(db.Float, default=4.0, nullable=False)      # Worked < 4 hours is HALF_DAY
    full_day_min_hours = db.Column(db.Float, default=7.0, nullable=False)      # Worked >= 7 hours is full PRESENT
    weekly_working_days = db.Column(db.String(50), default='MON,TUE,WED,THU,FRI', nullable=False) # Comma-separated
    min_attendance_percentage = db.Column(db.Float, default=80.0, nullable=False) # Minimum threshold for completion
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    updater = db.relationship('User', foreign_keys=[updated_by])

    def get_working_days_list(self):
        return [d.strip().upper() for d in self.weekly_working_days.split(',') if d.strip()]

    def __repr__(self):
        return f"<AttendancePolicy name='{self.name}' active={self.is_active}>"


class CompanyHoliday(db.Model):
    """
    Official company and national holiday calendar (Phase 2 Module 9).
    """
    __tablename__ = 'company_holidays'

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, unique=True, nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    is_optional = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def holiday_date(self):
        return self.date

    @holiday_date.setter
    def holiday_date(self, value):
        self.date = value

    def __repr__(self):
        return f"<CompanyHoliday date='{self.date}' name='{self.name}'>"


