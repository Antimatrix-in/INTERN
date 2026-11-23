"""
ANTI-MATRIX Phase 2 — Module 8 Test Suite
Document Management & Digital Certificate System.
"""

import io
import os
import json
from datetime import datetime, timezone, timedelta
import unittest
from flask import url_for
from app import create_app, db
from app.models import (
    User, Student, Internship, Project, ProjectAssignment,
    WeeklyMilestone, WeeklyTask, WeeklySubmission, Document,
    IssuedDocument, CertificateVerification, AuditLog
)
from app.seed import seed_initial_data
from app.services.document_service import DocumentService
from app.services.notification_service import NotificationService
from tests.test_config import TestConfig


class TestDocumentCertificateModule8(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.app.config['TESTING'] = True
        self.app_context = self.app.app_context()
        self.app_context.push()

        db.drop_all()
        db.create_all()
        seed_initial_data()

        self.client = self.app.test_client()

        # Seeded admin user
        self.admin = User.query.filter_by(employee_id='admin').first() or User.query.filter_by(role='admin').first()

        # Seeded student 1 & active internship
        self.student1 = Student.query.filter_by(student_uid='AM-INT-2026-001').first()
        self.student1_user = self.student1.user
        self.internship1 = self.student1.active_internship

        # Seeded student 2 (for IDOR boundary tests)
        self.student2 = Student.query.filter_by(student_uid='AM-INT-2026-002').first()
        if not self.student2:
            u2 = User(
                email='student2@test.com',
                full_name='Second Intern',
                role='student',
                employee_id='AM-INT-2026-002',
                is_active=True
            )
            u2.set_password('Student@2026Password!')
            db.session.add(u2)
            db.session.flush()

            self.student2 = Student(
                user_id=u2.id,
                student_uid='AM-INT-2026-002',
                college_id=self.student1.college_id,
                department_id=self.student1.department_id,
                roll_number='ROLL-002',
                degree='B.Tech',
                current_year='3rd Year',
                graduation_year='2027'
            )
            db.session.add(self.student2)
            db.session.flush()

            internship2 = Internship(
                student_id=self.student2.id,
                internship_no='AM-INTRN-2026-002',
                status='ACTIVE',
                start_date=datetime.now(timezone.utc) - timedelta(days=30),
                end_date=datetime.now(timezone.utc) + timedelta(days=60)
            )
            db.session.add(internship2)
            db.session.commit()
            self.internship2 = internship2
        else:
            self.internship2 = self.student2.active_internship

        self.student2_user = self.student2.user

        # Ensure internship1 has assignment
        self.assignment1 = self.internship1.active_assignment
        if not self.assignment1:
            prj = Project.query.first()
            self.assignment1 = ProjectAssignment(
                internship_id=self.internship1.id,
                project_id=prj.id,
                assigned_by=self.admin.id,
                status='ASSIGNED'
            )
            db.session.add(self.assignment1)
            db.session.commit()

        # Clean document tables for test determinism
        IssuedDocument.query.delete()
        CertificateVerification.query.delete()
        Document.query.delete()
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.app_context.pop()

    def login_admin(self):
        self.client.get('/logout')
        return self.client.post('/admin/login', data={
            'email_or_id': 'admin',
            'password': 'Admin@12345'
        }, follow_redirects=True)

    def login_student(self, student_uid='AM-INT-2026-001', password='Student@2026Password!'):
        self.client.get('/logout')
        return self.client.post('/login', data={
            'employee_id': student_uid,
            'password': password
        }, follow_redirects=True)

    # ──────────────────────────────────────────────────────────────────────────
    # 1. PDF Generation Tests (ReportLab)
    # ──────────────────────────────────────────────────────────────────────────

    def test_document_service_pdf_generation_all_types(self):
        """Verifies reportlab PDF generation for all corporate document and certificate types."""
        # 1. Offer Letter
        doc_no = DocumentService.generate_unique_doc_no('OFFER_LETTER')
        self.assertTrue(doc_no.startswith('AM-OFFER-2026-'))
        pdf_path = DocumentService.generate_offer_letter_pdf(self.internship1, doc_no)
        self.assertTrue(os.path.exists(pdf_path))
        with open(pdf_path, 'rb') as f:
            header = f.read(5)
            self.assertEqual(header, b'%PDF-')
        self.assertGreater(os.path.getsize(pdf_path), 1000)

        # 2. Joining Letter
        doc_no = DocumentService.generate_unique_doc_no('JOINING_LETTER')
        self.assertTrue(doc_no.startswith('AM-JOIN-2026-'))
        pdf_path = DocumentService.generate_joining_letter_pdf(self.internship1, doc_no)
        self.assertTrue(os.path.exists(pdf_path))
        with open(pdf_path, 'rb') as f:
            self.assertEqual(f.read(5), b'%PDF-')

        # 3. Completion Certificate (Landscape with QR code)
        doc_no = DocumentService.generate_unique_doc_no('CERTIFICATE')
        self.assertTrue(doc_no.startswith('AM-CERT-2026-'))
        pdf_path = DocumentService.generate_certificate_pdf(self.internship1, doc_no)
        self.assertTrue(os.path.exists(pdf_path))
        with open(pdf_path, 'rb') as f:
            self.assertEqual(f.read(5), b'%PDF-')

        # 4. Experience Letter
        doc_no = DocumentService.generate_unique_doc_no('EXPERIENCE_LETTER')
        self.assertTrue(doc_no.startswith('AM-EXP-2026-'))
        pdf_path = DocumentService.generate_experience_letter_pdf(self.internship1, doc_no)
        self.assertTrue(os.path.exists(pdf_path))
        with open(pdf_path, 'rb') as f:
            self.assertEqual(f.read(5), b'%PDF-')

        # 5. Internship Agreement
        doc_no = DocumentService.generate_unique_doc_no('INTERNSHIP_AGREEMENT')
        self.assertTrue(doc_no.startswith('AM-AGR-2026-'))
        pdf_path = DocumentService.generate_agreement_pdf(self.internship1, doc_no)
        self.assertTrue(os.path.exists(pdf_path))
        with open(pdf_path, 'rb') as f:
            self.assertEqual(f.read(5), b'%PDF-')

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Eligibility Validation Tests
    # ──────────────────────────────────────────────────────────────────────────

    def test_certificate_eligibility_validation(self):
        """Validates that certificates cannot be issued prematurely without verified completion."""
        # Setup clean test milestones on assignment1
        WeeklySubmission.query.delete()
        WeeklyMilestone.query.filter_by(assignment_id=self.assignment1.id).delete()
        db.session.commit()

        m1 = WeeklyMilestone(assignment_id=self.assignment1.id, week_number=1, title='W1 Deliverable', objective='Deliverable 1', status='IN_PROGRESS')
        m2 = WeeklyMilestone(assignment_id=self.assignment1.id, week_number=2, title='W2 Deliverable', objective='Deliverable 2', status='LOCKED')
        db.session.add_all([m1, m2])
        self.internship1.status = 'ACTIVE'
        db.session.commit()

        # Check eligibility - should fail
        res = DocumentService.validate_certificate_eligibility(self.internship1)
        self.assertFalse(res['is_eligible'])
        self.assertEqual(res['approved_milestones'], 0)
        self.assertEqual(res['total_milestones'], 2)

        # Attempting to issue without override raises ValueError
        with self.assertRaises(ValueError) as ctx:
            DocumentService.issue_certificate(self.internship1.id, self.admin.id, override_eligibility=False)
        self.assertIn('Ineligible for certificate', str(ctx.exception))

        # Now mark all milestones completed
        m1.status = 'COMPLETED'
        m2.status = 'APPROVED'
        db.session.commit()

        res2 = DocumentService.validate_certificate_eligibility(self.internship1)
        self.assertTrue(res2['is_eligible'])
        self.assertEqual(res2['approved_milestones'], 2)
        self.assertEqual(res2['completion_percentage'], 100.0)

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Certificate Issuance & QR Verification
    # ──────────────────────────────────────────────────────────────────────────

    def test_certificate_issuance_and_records(self):
        """Verifies full certificate issuance pipeline: IssuedDocument, CertificateVerification, and notification."""
        issued_doc, cert_ver = DocumentService.issue_certificate(
            internship_id=self.internship1.id,
            admin_user_id=self.admin.id,
            override_eligibility=True,
            override_reason='Administrative approval after manual viva review'
        )

        self.assertIsNotNone(issued_doc)
        self.assertIsNotNone(cert_ver)
        self.assertTrue(issued_doc.document_no.startswith('AM-CERT-2026-'))
        self.assertEqual(issued_doc.document_no, cert_ver.certificate_no)
        self.assertEqual(issued_doc.status, 'ISSUED')
        self.assertEqual(cert_ver.status, 'Successfully Completed')
        self.assertIn('/verify/', cert_ver.qr_code_data)
        self.assertEqual(self.internship1.status, 'COMPLETED')

        # Verify audit log
        audit = AuditLog.query.filter_by(action='CERTIFICATE_ISSUED', target_id=issued_doc.document_no).first()
        self.assertIsNotNone(audit)
        self.assertIn('manual viva review', audit.details_json)

    # ──────────────────────────────────────────────────────────────────────────
    # 4. Public Verification Page & Privacy Leakage Protection
    # ──────────────────────────────────────────────────────────────────────────

    def test_public_verification_page_and_privacy_guard(self):
        """Verifies that the public verification page shows authenticity without leaking personal data."""
        issued_doc, cert_ver = DocumentService.issue_certificate(
            internship_id=self.internship1.id,
            admin_user_id=self.admin.id,
            override_eligibility=True
        )

        # 1. Valid certificate query
        resp = self.client.get(f'/verify/{cert_ver.certificate_no}')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode('utf-8')

        # Must display authorized verification details
        self.assertIn(cert_ver.certificate_no, html)
        self.assertIn('CERTIFICATE VERIFIED', html)
        self.assertIn(self.student1.full_name, html)
        self.assertIn('ANTI MATRIX', html)

        # MUST NOT leak private personal data
        self.assertNotIn(self.student1.email, html)
        self.assertNotIn('student_profile', html)
        self.assertNotIn('aadhaar', html.lower())
        self.assertNotIn('phone', html.lower())

        # 2. JSON API verification
        json_resp = self.client.get(
            f'/verify/{cert_ver.certificate_no}',
            headers={'Accept': 'application/json'}
        )
        self.assertEqual(json_resp.status_code, 200)
        data = json_resp.get_json()
        self.assertTrue(data['valid'])
        self.assertEqual(data['certificate_no'], cert_ver.certificate_no)
        self.assertNotIn('email', data)

        # 3. Non-existent certificate query
        resp_fake = self.client.get('/verify/AM-CERT-FAKE-99999')
        self.assertEqual(resp_fake.status_code, 200)
        self.assertIn('Certificate Not Found', resp_fake.data.decode('utf-8'))

    # ──────────────────────────────────────────────────────────────────────────
    # 5. Certificate Revocation & Public Status Update
    # ──────────────────────────────────────────────────────────────────────────

    def test_certificate_revocation(self):
        """Verifies certificate revocation updates verification record and public portal immediately."""
        issued_doc, cert_ver = DocumentService.issue_certificate(
            internship_id=self.internship1.id,
            admin_user_id=self.admin.id,
            override_eligibility=True
        )

        rev_reason = "Academic integrity violation: plagiarized milestone deliverables."
        success = DocumentService.revoke_certificate(
            certificate_no=cert_ver.certificate_no,
            admin_user_id=self.admin.id,
            reason=rev_reason
        )
        self.assertTrue(success)

        # Check DB states
        db.session.refresh(issued_doc)
        db.session.refresh(cert_ver)
        self.assertEqual(issued_doc.status, 'REVOKED')
        self.assertEqual(issued_doc.revocation_reason, rev_reason)
        self.assertEqual(cert_ver.status, 'Revoked')
        self.assertTrue(cert_ver.is_revoked)

        # Check public verification page shows revoked
        resp = self.client.get(f'/verify/{cert_ver.certificate_no}')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode('utf-8')
        self.assertIn('CERTIFICATE REVOKED', html)

        # Check audit log
        audit = AuditLog.query.filter_by(action='CERTIFICATE_REVOKED', target_id=cert_ver.certificate_no).first()
        self.assertIsNotNone(audit)
        self.assertIn('plagiarized', audit.details_json)

    # ──────────────────────────────────────────────────────────────────────────
    # 6. Certificate Reissuance
    # ──────────────────────────────────────────────────────────────────────────

    def test_certificate_reissuance(self):
        """Verifies reissuance revokes old certificate and generates a new active certificate."""
        old_doc, old_cert = DocumentService.issue_certificate(
            internship_id=self.internship1.id,
            admin_user_id=self.admin.id,
            override_eligibility=True
        )
        old_no = old_cert.certificate_no

        new_doc, new_cert = DocumentService.reissue_certificate(
            old_certificate_no=old_no,
            admin_user_id=self.admin.id,
            reason='Corrected typographical error in candidate name'
        )

        self.assertNotEqual(old_no, new_cert.certificate_no)
        self.assertEqual(new_cert.status, 'Successfully Completed')
        self.assertEqual(new_doc.status, 'ISSUED')

        # Check old doc is revoked
        db.session.refresh(old_doc)
        self.assertEqual(old_doc.status, 'REVOKED')
        self.assertIn('Superseded by reissuance', old_doc.revocation_reason)

        # Check reissued audit log
        audit = AuditLog.query.filter_by(action='CERTIFICATE_REISSUED', target_id=new_cert.certificate_no).first()
        self.assertIsNotNone(audit)
        self.assertIn(old_no, audit.details_json)

    # ──────────────────────────────────────────────────────────────────────────
    # 7. Student Document Upload & Review
    # ──────────────────────────────────────────────────────────────────────────

    def test_student_document_upload_and_validation(self):
        """Verifies student document upload with extension and size validation."""
        # 1. Valid PDF upload
        fake_pdf = io.BytesIO(b'%PDF-1.4 test document content')
        fake_pdf.filename = 'My_Resume.pdf'
        fake_pdf.mimetype = 'application/pdf'

        doc = DocumentService.upload_student_document(
            student_id=self.student1.id,
            doc_type='RESUME',
            file_obj=fake_pdf,
            uploaded_by_user_id=self.student1_user.id
        )
        self.assertIsNotNone(doc.id)
        self.assertEqual(doc.status, 'UPLOADED')
        self.assertEqual(doc.doc_type, 'RESUME')
        self.assertEqual(doc.file_name, 'My_Resume.pdf')

        # 2. Disallowed extension upload
        bad_file = io.BytesIO(b'malicious script')
        bad_file.filename = 'malicious.exe'
        bad_file.mimetype = 'application/x-msdownload'

        with self.assertRaises(ValueError) as ctx:
            DocumentService.upload_student_document(
                student_id=self.student1.id,
                doc_type='RESUME',
                file_obj=bad_file
            )
        self.assertIn('not permitted', str(ctx.exception))

    def test_admin_document_review(self):
        """Verifies administrative review (approve / reject) of uploaded documents."""
        fake_pdf = io.BytesIO(b'%PDF-1.4 college id proof')
        fake_pdf.filename = 'College_ID.pdf'
        fake_pdf.mimetype = 'application/pdf'

        doc = DocumentService.upload_student_document(
            student_id=self.student1.id,
            doc_type='COLLEGE_ID',
            file_obj=fake_pdf
        )

        # Admin approves
        DocumentService.review_student_document(
            document_id=doc.id,
            admin_user_id=self.admin.id,
            new_status='APPROVED'
        )
        self.assertEqual(doc.status, 'APPROVED')
        self.assertEqual(doc.verified_by, self.admin.id)
        self.assertIsNotNone(doc.verified_at)

        # Admin rejects
        DocumentService.review_student_document(
            document_id=doc.id,
            admin_user_id=self.admin.id,
            new_status='REJECTED',
            rejection_reason='Unclear photograph, please re-upload clear scan.'
        )
        self.assertEqual(doc.status, 'REJECTED')
        self.assertIn('Unclear photograph', doc.rejection_reason)

    # ──────────────────────────────────────────────────────────────────────────
    # 8. Security & RBAC: IDOR & Path Traversal Prevention
    # ──────────────────────────────────────────────────────────────────────────

    def test_security_idor_and_download_boundaries(self):
        """Verifies that interns can only download their own documents and cannot access peers' files."""
        # Issue an offer letter for Student 1
        doc1 = DocumentService.issue_document(
            doc_type='OFFER_LETTER',
            internship_id=self.internship1.id,
            admin_user_id=self.admin.id
        )

        # Issue an offer letter for Student 2
        doc2 = DocumentService.issue_document(
            doc_type='OFFER_LETTER',
            internship_id=self.internship2.id,
            admin_user_id=self.admin.id
        )

        # 1. Student 1 downloads own document -> Allowed
        path, name, mime = DocumentService.get_document_for_download(
            doc_identifier=doc1.document_no,
            current_user_obj=self.student1_user,
            is_issued=True
        )
        self.assertTrue(os.path.exists(path))

        # 2. Student 2 attempts to download Student 1's document -> IDOR blocked
        with self.assertRaises(PermissionError):
            DocumentService.get_document_for_download(
                doc_identifier=doc1.document_no,
                current_user_obj=self.student2_user,
                is_issued=True
            )

        # 3. Admin downloads any document -> Allowed
        admin_path, _, _ = DocumentService.get_document_for_download(
            doc_identifier=doc1.document_no,
            current_user_obj=self.admin,
            is_issued=True
        )
        self.assertTrue(os.path.exists(admin_path))

    # ──────────────────────────────────────────────────────────────────────────
    # 9. HTTP Endpoints & Access Control
    # ──────────────────────────────────────────────────────────────────────────

    def test_http_endpoints_and_views(self):
        """Tests student and admin document HTTP routes and access controls."""
        # 1. Student views /documents
        self.login_student()
        resp = self.client.get('/documents')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Official Offer Letter', resp.data.decode('utf-8'))
        self.assertIn('Completion Certificate Eligibility Status', resp.data.decode('utf-8'))

        # 2. Student uploads via HTTP POST /documents/upload
        upload_resp = self.client.post('/documents/upload', data={
            'doc_type': 'RESUME',
            'file': (io.BytesIO(b'%PDF-1.4 test resume'), 'resume.pdf')
        }, follow_redirects=True)
        self.assertEqual(upload_resp.status_code, 200)
        self.assertIn('uploaded successfully', upload_resp.data.decode('utf-8'))

        # 3. Admin views /admin/documents workspace
        self.login_admin()
        admin_resp = self.client.get('/admin/documents')
        self.assertEqual(admin_resp.status_code, 200)
        self.assertIn('Document Management & Digital Certificates', admin_resp.data.decode('utf-8'))

        # 4. Admin issues document via HTTP POST /admin/documents/issue
        issue_resp = self.client.post('/admin/documents/issue', data={
            'internship_id': self.internship1.id,
            'doc_type': 'OFFER_LETTER',
            'notes': 'Automated test issuance'
        }, follow_redirects=True)
        self.assertEqual(issue_resp.status_code, 200)
        self.assertIn('successfully generated and issued', issue_resp.data.decode('utf-8'))

        # 5. Non-admin student attempts admin endpoint -> redirected/blocked
        self.login_student()
        blocked_resp = self.client.get('/admin/documents')
        self.assertIn(blocked_resp.status_code, [302, 403])
