"""
Anti-Matrix Intern Portal — Document Management & Digital Certificate Service (Phase 2, Module 8).
Handles secure document generation, PDF certificate creation with QR verification,
document uploads, eligibility validation, certificate revocation, reissuance, and secure downloads.
"""

import os
import io
import hashlib
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, url_for
from werkzeug.utils import secure_filename
from sqlalchemy import or_

from app import db
from app.models import (
    Document, IssuedDocument, CertificateVerification,
    Student, Internship, ProjectAssignment, WeeklyMilestone,
    WeeklySubmission, User, AuditLog
)
from app.services.notification_service import NotificationService

# ReportLab & Imaging Imports
from reportlab.lib.pagesizes import A4, landscape, letter
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, KeepTogether, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas
import qrcode
from PIL import Image as PILImage


class DocumentService:
    """Centralized service for corporate document and digital certificate management."""

    ALLOWED_UPLOAD_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'docx'}
    MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

    # Anti-Matrix Brand Colors for ReportLab
    COLOR_PRIMARY = colors.HexColor('#10B981')       # Emerald
    COLOR_PRIMARY_DARK = colors.HexColor('#059669')  # Deep Emerald
    COLOR_PRIMARY_LIGHT = colors.HexColor('#D1FAE5') # Mint / Soft Light
    COLOR_OBSIDIAN = colors.HexColor('#030A07')      # Deep Obsidian
    COLOR_SLATE_DARK = colors.HexColor('#0F172A')    # Slate 900
    COLOR_SLATE_BODY = colors.HexColor('#334155')    # Slate 700
    COLOR_SLATE_MUTED = colors.HexColor('#64748B')   # Slate 500
    COLOR_BORDER = colors.HexColor('#E2E8F0')        # Border light
    COLOR_GOLD = colors.HexColor('#D97706')          # Gold Seal

    # ──────────────────────────────────────────────────────────────────────────
    # Storage & Helper Methods
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def get_storage_dir(cls) -> Path:
        """Returns the absolute base path for stored corporate and uploaded documents."""
        upload_folder = current_app.config.get('UPLOAD_FOLDER')
        if upload_folder:
            base_dir = Path(upload_folder) / 'documents'
        else:
            base_dir = Path(current_app.root_path) / 'static' / 'uploads' / 'documents'
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir

    @classmethod
    def generate_unique_doc_no(cls, doc_type: str) -> str:
        """
        Generates a standardized, collision-free document or certificate number.
        Examples: AM-CERT-2026-X8F2A, AM-OFFER-2026-B912C, AM-JOIN-2026-78AC1
        """
        prefixes = {
            'CERTIFICATE': 'AM-CERT-2026-',
            'OFFER_LETTER': 'AM-OFFER-2026-',
            'JOINING_LETTER': 'AM-JOIN-2026-',
            'EXPERIENCE_LETTER': 'AM-EXP-2026-',
            'INTERNSHIP_AGREEMENT': 'AM-AGR-2026-',
        }
        prefix = prefixes.get(doc_type, 'AM-DOC-2026-')

        for _ in range(50):
            suffix = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(5))
            candidate = f"{prefix}{suffix}"

            # Check collision across IssuedDocument and CertificateVerification
            if doc_type == 'CERTIFICATE':
                if not CertificateVerification.query.filter_by(certificate_no=candidate).first() and \
                   not IssuedDocument.query.filter_by(document_no=candidate).first():
                    return candidate
            else:
                if not IssuedDocument.query.filter_by(document_no=candidate).first():
                    return candidate

        # Fallback timestamp suffix if collision loop somehow exhausts
        ts_suffix = datetime.now(timezone.utc).strftime('%H%M%S')
        return f"{prefix}{ts_suffix}"

    @classmethod
    def format_date_str(cls, val, default: str = "") -> str:
        """Safely formats a date object or string into a human-readable date."""
        if not val:
            return default
        if hasattr(val, 'strftime'):
            return val.strftime('%B %d, %Y')
        if isinstance(val, str):
            val_clean = val.strip()
            for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d-%m-%Y', '%d/%m/%Y', '%B %d, %Y', '%d %b %Y'):
                try:
                    return datetime.strptime(val_clean, fmt).strftime('%B %d, %Y')
                except ValueError:
                    pass
            return val_clean
        return str(val)

    @classmethod
    def generate_qr_code_image(cls, verify_url: str) -> io.BytesIO:
        """Generates a high-contrast QR code pointing to the public certificate verification URL."""
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=4,
            border=2,
        )
        qr.add_data(verify_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#030A07", back_color="#FFFFFF")

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return buf

    # ──────────────────────────────────────────────────────────────────────────
    # Eligibility Validation
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def validate_certificate_eligibility(cls, internship: Internship) -> dict:
        """
        Validates whether an intern is rigorously eligible for a Completion Certificate.
        Rule: Do NOT automatically issue certificates solely because an end date has passed.
        Requires verified completion and authorized approval.
        """
        if not internship:
            return {
                'is_eligible': False,
                'reason': 'Internship record not found.',
                'total_milestones': 0,
                'approved_milestones': 0,
                'completion_percentage': 0.0,
                'internship_status': 'UNKNOWN'
            }

        assignment = internship.active_assignment
        if not assignment:
            return {
                'is_eligible': False,
                'reason': 'No project assignment linked to this internship.',
                'total_milestones': 0,
                'approved_milestones': 0,
                'completion_percentage': 0.0,
                'internship_status': internship.status
            }

        milestones = assignment.weekly_milestones.all()
        total_milestones = len(milestones)
        approved_milestones = 0

        for m in milestones:
            # Check milestone completion flag or approved submission
            if m.is_completed:
                approved_milestones += 1
            else:
                sub = WeeklySubmission.query.filter_by(milestone_id=m.id, student_id=internship.student_id).first()
                if sub and sub.status in ['APPROVED', 'COMPLETED', 'VERIFIED']:
                    approved_milestones += 1

        completion_pct = round((approved_milestones / total_milestones * 100), 1) if total_milestones > 0 else 0.0

        # Eligibility criteria:
        # 1. If milestones exist: all milestones must be approved (100% completion) OR
        # 2. Internship status is explicitly COMPLETED/VERIFIED by academic staff.
        if internship.status in ['COMPLETED', 'VERIFIED']:
            is_eligible = True
            reason = 'Internship has been verified and marked COMPLETED by administration.'
        elif total_milestones > 0 and approved_milestones == total_milestones:
            is_eligible = True
            reason = f'All {total_milestones} weekly project milestones successfully completed and approved.'
        elif total_milestones == 0:
            is_eligible = False
            reason = 'No weekly deliverables configured for this internship track.'
        else:
            is_eligible = False
            reason = f'Only {approved_milestones} of {total_milestones} milestones approved ({completion_pct}% complete). All deliverables must be approved.'

        return {
            'is_eligible': is_eligible,
            'reason': reason,
            'total_milestones': total_milestones,
            'approved_milestones': approved_milestones,
            'completion_percentage': completion_pct,
            'internship_status': internship.status
        }

    # ──────────────────────────────────────────────────────────────────────────
    # PDF Generators (ReportLab)
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def generate_certificate_pdf(
        cls,
        internship: Internship,
        certificate_no: str,
        issue_date_str: str = None,
        public_verify_url: str = None
    ) -> str:
        """
        Generates a premium landscape Completion Certificate PDF with custom canvas borders,
        Anti-Matrix branding, dynamic embedded QR code, and digital verification seal.
        Returns the relative file path.
        """
        storage_dir = cls.get_storage_dir()
        filename = f"Certificate_{certificate_no}.pdf"
        file_path = storage_dir / filename

        student = internship.student
        assignment = internship.active_assignment
        project = assignment.project if assignment else None

        student_name = student.full_name if student else "Intern"
        student_uid = student.student_uid if student else "AM-INT"
        project_title = project.title if project else "Enterprise Software Engineering"
        project_code = project.project_code if project else "AM-PRJ-01"
        duration = internship.duration_weeks_display if hasattr(internship, 'duration_weeks_display') else "3 Months"
        if not duration or duration == '0 Weeks':
            duration = "3 Months"

        start_date = cls.format_date_str(internship.start_date, "Commencement")
        end_date = cls.format_date_str(internship.end_date, "Completion")
        if not issue_date_str:
            issue_date_str = datetime.now(timezone.utc).strftime('%B %d, %Y')

        if not public_verify_url:
            public_verify_url = f"https://portal.antimatrix.co.in/verify/{certificate_no}"

        # Generate QR code in-memory
        qr_buf = cls.generate_qr_code_image(public_verify_url)

        # Page geometry: Landscape A4
        page_w, page_h = landscape(A4)

        # Custom Canvas Callback to draw borders and seals
        def draw_certificate_decorations(canvas_obj, doc_obj):
            canvas_obj.saveState()

            # 1. Outer Double Border in Anti-Matrix Emerald
            canvas_obj.setStrokeColor(colors.HexColor('#10B981'))
            canvas_obj.setLineWidth(3.5)
            canvas_obj.rect(26, 26, page_w - 52, page_h - 52)

            canvas_obj.setStrokeColor(colors.HexColor('#059669'))
            canvas_obj.setLineWidth(1.0)
            canvas_obj.rect(32, 32, page_w - 64, page_h - 64)

            # 2. Corner Flourish Accents
            flourish_size = 14
            for x, y in [
                (32, 32), (page_w - 32 - flourish_size, 32),
                (32, page_h - 32 - flourish_size), (page_w - 32 - flourish_size, page_h - 32 - flourish_size)
            ]:
                canvas_obj.setFillColor(colors.HexColor('#10B981'))
                canvas_obj.rect(x, y, flourish_size, flourish_size, fill=1, stroke=0)

            # 3. Security Watermark text in center background
            canvas_obj.setFillColor(colors.HexColor('#F8FAFC'))
            canvas_obj.setFont('Helvetica-Bold', 48)
            canvas_obj.drawCentredString(page_w / 2, page_h / 2 - 10, "")

            # 4. Gold Digital Corporate Seal
            seal_x = page_w - 110
            seal_y = 65
            canvas_obj.setStrokeColor(colors.HexColor('#D97706'))
            canvas_obj.setLineWidth(1.5)
            canvas_obj.circle(seal_x, seal_y, 34, stroke=1, fill=0)
            canvas_obj.circle(seal_x, seal_y, 30, stroke=1, fill=0)
            canvas_obj.setFont('Helvetica-Bold', 7)
            canvas_obj.setFillColor(colors.HexColor('#D97706'))
            canvas_obj.drawCentredString(seal_x, seal_y + 12, "ANTI MATRIX")
            canvas_obj.drawCentredString(seal_x, seal_y + 2, "★ VERIFIED ★")
            canvas_obj.drawCentredString(seal_x, seal_y - 8, "CREDENTIAL")
            canvas_obj.drawCentredString(seal_x, seal_y - 18, "2026")

            canvas_obj.restoreState()

        # Build Flowable Story
        doc = SimpleDocTemplate(
            str(file_path),
            pagesize=landscape(A4),
            leftMargin=50,
            rightMargin=50,
            topMargin=46,
            bottomMargin=42
        )

        styles = getSampleStyleSheet()

        # Custom Typography Styles
        style_header = ParagraphStyle(
            'CertHeader',
            fontName='Helvetica-Bold',
            fontSize=11,
            leading=14,
            textColor=colors.HexColor('#059669'),
            alignment=1, # Center
            spaceAfter=3
        )
        style_sub_header = ParagraphStyle(
            'CertSubHeader',
            fontName='Helvetica',
            fontSize=8,
            leading=10,
            textColor=colors.HexColor('#64748B'),
            alignment=1,
            spaceAfter=12
        )
        style_title = ParagraphStyle(
            'CertTitle',
            fontName='Helvetica-Bold',
            fontSize=21,
            leading=25,
            textColor=colors.HexColor('#030A07'),
            alignment=1,
            spaceAfter=8
        )
        style_conferred = ParagraphStyle(
            'CertConferred',
            fontName='Helvetica',
            fontSize=9,
            leading=12,
            textColor=colors.HexColor('#475569'),
            alignment=1,
            spaceAfter=8
        )
        style_recipient = ParagraphStyle(
            'CertRecipient',
            fontName='Helvetica-Bold',
            fontSize=24,
            leading=28,
            textColor=colors.HexColor('#065F46'),
            alignment=1,
            spaceAfter=10
        )
        style_body = ParagraphStyle(
            'CertBody',
            fontName='Helvetica',
            fontSize=10,
            leading=15,
            textColor=colors.HexColor('#1E293B'),
            alignment=1,
            spaceAfter=18
        )
        style_meta_label = ParagraphStyle(
            'CertMetaLabel',
            fontName='Helvetica-Bold',
            fontSize=7.5,
            leading=9,
            textColor=colors.HexColor('#64748B'),
            alignment=0
        )
        style_meta_val = ParagraphStyle(
            'CertMetaVal',
            fontName='Helvetica-Bold',
            fontSize=8.5,
            leading=11,
            textColor=colors.HexColor('#0F172A'),
            alignment=0
        )
        style_sig_title = ParagraphStyle(
            'CertSigTitle',
            fontName='Helvetica-Bold',
            fontSize=9,
            leading=11,
            textColor=colors.HexColor('#0F172A'),
            alignment=1
        )
        style_sig_sub = ParagraphStyle(
            'CertSigSub',
            fontName='Helvetica',
            fontSize=7.5,
            leading=9,
            textColor=colors.HexColor('#64748B'),
            alignment=1
        )

        story = []

        # 1. Header
        story.append(Paragraph("ANTI&bull;MATRIX ACADEMIC & RESEARCH DIVISION", style_header))
        story.append(Paragraph("CENTER FOR ENTERPRISE SOFTWARE ENGINEERING & ADVANCED COMPUTING", style_sub_header))
        story.append(Spacer(1, 4))

        # 2. Main Title
        story.append(Paragraph("CERTIFICATE OF EXCELLENCE & INTERNSHIP COMPLETION", style_title))
        story.append(Spacer(1, 4))

        # 3. Recipient
        story.append(Paragraph("THIS DIGITAL CREDENTIAL IS PROUDLY CONFERRED UPON", style_conferred))
        story.append(Paragraph(student_name.upper(), style_recipient))

        # 4. Body Statement
        body_text = (
            f"for successfully completing the rigorous <b>{duration}</b> Technical Internship program "
            f"on project <b>{project_title}</b> (<code>{project_code}</code>) between <b>{start_date}</b> and <b>{end_date}</b>. "
            f"During the tenure, the candidate demonstrated distinguished diligence, mastery of modern full-stack "
            f"architecture, algorithmic problem solving, and adherence to production engineering quality standards."
        )
        story.append(Paragraph(body_text, style_body))
        story.append(Spacer(1, 10))

        # 5. Metadata Block + Embedded QR Code + Signatures Table
        qr_image = RLImage(qr_buf, width=64, height=64)

        meta_table_data = [
            [
                qr_image,
                Table([
                    [Paragraph("CERTIFICATE ID", style_meta_label), Paragraph(certificate_no, style_meta_val)],
                    [Paragraph("INTERN ID", style_meta_label), Paragraph(student_uid, style_meta_val)],
                    [Paragraph("ISSUE DATE", style_meta_label), Paragraph(issue_date_str, style_meta_val)],
                    [Paragraph("STATUS", style_meta_label), Paragraph("<font color='#059669'>VERIFIED & AUTHENTIC</font>", style_meta_val)],
                ], colWidths=[90, 150]),
                Table([
                    [Paragraph("_____________________________", style_sig_title)],
                    [Paragraph("Director of Engineering", style_sig_title)],
                    [Paragraph("Anti-Matrix Technologies Pvt. Ltd.", style_sig_sub)],
                ], colWidths=[180]),
                Table([
                    [Paragraph("_____________________________", style_sig_title)],
                    [Paragraph("Head of Academic Operations", style_sig_title)],
                    [Paragraph("Anti-Matrix Research Board", style_sig_sub)],
                ], colWidths=[180]),
            ]
        ]

        footer_table = Table(meta_table_data, colWidths=[70, 250, 190, 190])
        footer_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (0, 0), 'CENTER'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
        ]))

        story.append(footer_table)

        # Build document
        doc.build(story, onFirstPage=draw_certificate_decorations)

        return str(file_path)

    @classmethod
    def generate_offer_letter_pdf(
        cls,
        internship: Internship,
        document_no: str,
        issue_date_str: str = None
    ) -> str:
        """Generates an official formal Offer Letter PDF on corporate letterhead."""
        storage_dir = cls.get_storage_dir()
        filename = f"OfferLetter_{document_no}.pdf"
        file_path = storage_dir / filename

        student = internship.student
        assignment = internship.active_assignment
        project = assignment.project if assignment else None

        student_name = student.full_name if student else "Intern Candidate"
        student_uid = student.student_uid if student else "AM-INT"
        college_name = student.college.name if (student and student.college and hasattr(student.college, 'name')) else "Partner Engineering Institute"
        department = student.department.name if (student and student.department and hasattr(student.department, 'name')) else (str(student.department) if student and student.department else "Computer Science & Engineering")
        project_title = project.title if project else "Enterprise Full-Stack Application"
        project_code = project.project_code if project else "AM-PRJ-01"

        start_date = cls.format_date_str(internship.start_date, "Upon Onboarding")
        end_date = cls.format_date_str(internship.end_date, "Program Completion")
        duration = internship.duration_weeks_display if hasattr(internship, 'duration_weeks_display') else "3 Months"
        if not duration or duration == '0 Weeks':
            duration = "3 Months"

        if not issue_date_str:
            issue_date_str = datetime.now(timezone.utc).strftime('%B %d, %Y')

        doc = SimpleDocTemplate(
            str(file_path),
            pagesize=A4,
            leftMargin=45,
            rightMargin=45,
            topMargin=40,
            bottomMargin=40
        )

        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            'OfferTitle',
            fontName='Helvetica-Bold',
            fontSize=15,
            leading=18,
            textColor=colors.HexColor('#030A07'),
            alignment=1,
            spaceAfter=15
        )
        body_style = ParagraphStyle(
            'OfferBody',
            fontName='Helvetica',
            fontSize=9.5,
            leading=14.5,
            textColor=colors.HexColor('#1E293B'),
            spaceAfter=10
        )
        h2_style = ParagraphStyle(
            'OfferH2',
            fontName='Helvetica-Bold',
            fontSize=10.5,
            leading=13,
            textColor=colors.HexColor('#059669'),
            spaceBefore=6,
            spaceAfter=4
        )

        story = []

        # Header Table
        header_data = [
            [
                Paragraph("<b>ANTI&bull;MATRIX TECHNOLOGIES</b><br/><font size=7.5 color='#64748B'>Center for Enterprise Software Research & Development<br/>Bangalore &bull; Hyderabad &bull; Remote Operations</font>", body_style),
                Paragraph(f"<b>Ref No:</b> {document_no}<br/><b>Date:</b> {issue_date_str}", ParagraphStyle('RightRef', parent=body_style, alignment=2))
            ]
        ]
        h_table = Table(header_data, colWidths=[330, 175])
        h_table.setStyle(TableStyle([
            ('BOTTOMPADDING', (0,0), (-1,-1), 8),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('LINEBELOW', (0,0), (-1,-1), 1, colors.HexColor('#10B981'))
        ]))
        story.append(h_table)
        story.append(Spacer(1, 14))

        # Addressee
        story.append(Paragraph(f"<b>To:</b><br/><b>{student_name}</b> (ID: {student_uid})<br/>{department}<br/>{college_name}", body_style))
        story.append(Spacer(1, 10))

        # Title
        story.append(Paragraph("OFFICIAL OFFER OF INTERNSHIP APPOINTMENT", title_style))

        # Body clauses
        story.append(Paragraph(
            f"Dear <b>{student_name}</b>,<br/><br/>"
            f"We are delighted to formally offer you an internship position at <b>Anti-Matrix Technologies</b> as an "
            f"<b>Engineering Intern</b>. This offer is extended based on your academic credentials, performance evaluation, "
            f"and passion for modern software engineering.",
            body_style
        ))

        story.append(Paragraph("1. Position & Project Allocation", h2_style))
        story.append(Paragraph(
            f"You will be assigned to the product engineering group working on <b>{project_title}</b> (Code: <code>{project_code}</code>). "
            f"Your responsibilities include building enterprise full-stack modules, implementing automated test suites, and "
            f"participating in weekly code evaluations with your designated mentor.",
            body_style
        ))

        story.append(Paragraph("2. Program Tenure & Commencement", h2_style))
        story.append(Paragraph(
            f"Your internship tenure is scheduled for <b>{duration}</b>, starting effectively on <b>{start_date}</b> and concluding on "
            f"<b>{end_date}</b>. Continuation through the program is contingent upon satisfactory progress across all weekly milestone deliverables.",
            body_style
        ))

        story.append(Paragraph("3. Workspace & Code Standards", h2_style))
        story.append(Paragraph(
            f"You will be granted access to the Anti-Matrix Intern Portal with your Employee UID <b>{student_uid}</b>. "
            f"All source code submissions must be managed via version-controlled repositories and adhere to strict code quality rubrics.",
            body_style
        ))

        story.append(Paragraph("4. Confidentiality & Intellectual Property", h2_style))
        story.append(Paragraph(
            "All technical documentation, proprietary source code, and design architecture developed during the internship "
            "remain the sole intellectual property of Anti-Matrix Technologies.",
            body_style
        ))

        story.append(Spacer(1, 14))

        # Signatures
        sig_data = [
            [
                Paragraph("<b>For Anti-Matrix Technologies Pvt. Ltd.</b><br/><br/><br/><b>Authorized Signatory</b><br/><font size=7.5 color='#64748B'>Human Resources & Talent Acquisition</font>", body_style),
                Paragraph("<b>Candidate Acceptance:</b><br/><br/><br/><b>Signature: _______________________</b><br/><font size=7.5 color='#64748B'>Name: " + student_name + "</font>", body_style)
            ]
        ]
        s_table = Table(sig_data, colWidths=[270, 235])
        s_table.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'TOP')]))
        story.append(s_table)

        doc.build(story)
        return str(file_path)

    @classmethod
    def generate_joining_letter_pdf(
        cls,
        internship: Internship,
        document_no: str,
        issue_date_str: str = None
    ) -> str:
        """Generates an official Joining / Induction Confirmation Letter PDF."""
        storage_dir = cls.get_storage_dir()
        filename = f"JoiningLetter_{document_no}.pdf"
        file_path = storage_dir / filename

        student = internship.student
        assignment = internship.active_assignment
        project = assignment.project if assignment else None
        mentor = internship.mentor if internship and internship.mentor else None

        student_name = student.full_name if student else "Intern"
        student_uid = student.student_uid if student else "AM-INT"
        mentor_name = mentor.full_name if mentor else "Senior Engineering Mentor"
        mentor_email = mentor.email if mentor else "mentors@antimatrix.co.in"
        project_title = project.title if project else "Enterprise Systems Architecture"
        project_code = project.project_code if project else "AM-PRJ-01"

        start_date = cls.format_date_str(internship.start_date, "Immediate")
        duration = internship.duration_weeks_display if hasattr(internship, 'duration_weeks_display') else "3 Months"
        if not duration or duration == '0 Weeks':
            duration = "3 Months"

        if not issue_date_str:
            issue_date_str = datetime.now(timezone.utc).strftime('%B %d, %Y')

        doc = SimpleDocTemplate(
            str(file_path),
            pagesize=A4,
            leftMargin=45,
            rightMargin=45,
            topMargin=40,
            bottomMargin=40
        )

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            'JoinTitle',
            fontName='Helvetica-Bold',
            fontSize=15,
            leading=18,
            textColor=colors.HexColor('#030A07'),
            alignment=1,
            spaceAfter=15
        )
        body_style = ParagraphStyle(
            'JoinBody',
            fontName='Helvetica',
            fontSize=9.5,
            leading=14.5,
            textColor=colors.HexColor('#1E293B'),
            spaceAfter=10
        )
        h2_style = ParagraphStyle(
            'JoinH2',
            fontName='Helvetica-Bold',
            fontSize=10.5,
            leading=13,
            textColor=colors.HexColor('#059669'),
            spaceBefore=6,
            spaceAfter=4
        )

        story = []

        # Header Table
        header_data = [
            [
                Paragraph("<b>ANTI&bull;MATRIX TECHNOLOGIES</b><br/><font size=7.5 color='#64748B'>Engineering Operations & Induction Desk<br/>Bangalore &bull; Remote Intern Operations</font>", body_style),
                Paragraph(f"<b>Ref No:</b> {document_no}<br/><b>Date:</b> {issue_date_str}", ParagraphStyle('RightRef', parent=body_style, alignment=2))
            ]
        ]
        h_table = Table(header_data, colWidths=[330, 175])
        h_table.setStyle(TableStyle([
            ('BOTTOMPADDING', (0,0), (-1,-1), 8),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('LINEBELOW', (0,0), (-1,-1), 1, colors.HexColor('#10B981'))
        ]))
        story.append(h_table)
        story.append(Spacer(1, 14))

        story.append(Paragraph(f"<b>To:</b><br/><b>{student_name}</b><br/>Allocated Employee ID: <b>{student_uid}</b>", body_style))
        story.append(Spacer(1, 10))

        story.append(Paragraph("OFFICIAL INTERNSHIP INDUCTION & JOINING ORDER", title_style))

        story.append(Paragraph(
            f"Dear <b>{student_name}</b>,<br/><br/>"
            f"This letter confirms your formal induction and commencement of your <b>{duration}</b> technical internship "
            f"with Anti-Matrix Technologies, effective <b>{start_date}</b>.",
            body_style
        ))

        story.append(Paragraph("1. Induction Credentials", h2_style))
        cred_data = [
            [Paragraph("<b>Employee ID</b>", body_style), Paragraph(student_uid, body_style)],
            [Paragraph("<b>Allocated Project</b>", body_style), Paragraph(f"{project_title} ({project_code})", body_style)],
            [Paragraph("<b>Reporting Mentor</b>", body_style), Paragraph(f"{mentor_name} ({mentor_email})", body_style)],
            [Paragraph("<b>Portal Workspace</b>", body_style), Paragraph("https://portal.antimatrix.co.in", body_style)],
        ]
        c_table = Table(cred_data, colWidths=[150, 355])
        c_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F8FAFC')),
            ('BOX', (0,0), (-1,-1), 1, colors.HexColor('#E2E8F0')),
            ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
            ('TOPPADDING', (0,0), (-1,-1), 5),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ]))
        story.append(c_table)
        story.append(Spacer(1, 10))

        story.append(Paragraph("2. Onboarding Directives", h2_style))
        story.append(Paragraph(
            "Please log in to your Anti-Matrix Portal dashboard to complete your environment setup, review your Week 1 "
            "problem statement deliverables, and confirm your scheduled mentor touchpoints.",
            body_style
        ))

        story.append(Spacer(1, 18))
        story.append(Paragraph("<b>Issued By:</b><br/><br/><br/><b>Director of Engineering Operations</b><br/>Anti-Matrix Technologies Pvt. Ltd.", body_style))

        doc.build(story)
        return str(file_path)

    @classmethod
    def generate_experience_letter_pdf(
        cls,
        internship: Internship,
        document_no: str,
        issue_date_str: str = None
    ) -> str:
        """Generates an official Experience Letter PDF summarizing completed tenure and ratings."""
        storage_dir = cls.get_storage_dir()
        filename = f"ExperienceLetter_{document_no}.pdf"
        file_path = storage_dir / filename

        student = internship.student
        assignment = internship.active_assignment
        project = assignment.project if assignment else None

        student_name = student.full_name if student else "Intern"
        student_uid = student.student_uid if student else "AM-INT"
        project_title = project.title if project else "Enterprise Systems Architecture"
        project_code = project.project_code if project else "AM-PRJ-01"

        start_date = cls.format_date_str(internship.start_date, "Commencement")
        end_date = cls.format_date_str(internship.end_date, "Completion")
        duration = internship.duration_weeks_display if hasattr(internship, 'duration_weeks_display') else "3 Months"
        if not duration or duration == '0 Weeks':
            duration = "3 Months"

        if not issue_date_str:
            issue_date_str = datetime.now(timezone.utc).strftime('%B %d, %Y')

        doc = SimpleDocTemplate(
            str(file_path),
            pagesize=A4,
            leftMargin=45,
            rightMargin=45,
            topMargin=40,
            bottomMargin=40
        )

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            'ExpTitle',
            fontName='Helvetica-Bold',
            fontSize=15,
            leading=18,
            textColor=colors.HexColor('#030A07'),
            alignment=1,
            spaceAfter=15
        )
        body_style = ParagraphStyle(
            'ExpBody',
            fontName='Helvetica',
            fontSize=9.5,
            leading=14.5,
            textColor=colors.HexColor('#1E293B'),
            spaceAfter=10
        )

        story = []

        header_data = [
            [
                Paragraph("<b>ANTI&bull;MATRIX TECHNOLOGIES</b><br/><font size=7.5 color='#64748B'>Academic & Enterprise Credentials Directorate<br/>Bangalore &bull; Remote Intern Operations</font>", body_style),
                Paragraph(f"<b>Ref No:</b> {document_no}<br/><b>Date:</b> {issue_date_str}", ParagraphStyle('RightRef', parent=body_style, alignment=2))
            ]
        ]
        h_table = Table(header_data, colWidths=[330, 175])
        h_table.setStyle(TableStyle([
            ('BOTTOMPADDING', (0,0), (-1,-1), 8),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('LINEBELOW', (0,0), (-1,-1), 1, colors.HexColor('#10B981'))
        ]))
        story.append(h_table)
        story.append(Spacer(1, 16))

        story.append(Paragraph("TO WHOMSOEVER IT MAY CONCERN", title_style))

        story.append(Paragraph(
            f"This is to certify that <b>{student_name}</b> (Intern ID: <b>{student_uid}</b>) has successfully completed a "
            f"<b>{duration}</b> Technical Internship with <b>Anti-Matrix Technologies</b> as an Engineering Intern from "
            f"<b>{start_date}</b> to <b>{end_date}</b>.",
            body_style
        ))

        story.append(Paragraph(
            f"During the internship, {student_name} was actively involved in engineering deliverables on the project "
            f"<b>{project_title}</b> (<code>{project_code}</code>). Key contributions included system design, backend and "
            f"frontend feature implementation, API integrations, and participating in weekly technical reviews.",
            body_style
        ))

        story.append(Paragraph(
            "During the tenure, the candidate demonstrated strong analytical capability, proactive problem solving, and "
            "a commendable work ethic. All designated weekly project milestones were thoroughly reviewed and approved by our "
            "engineering leads.",
            body_style
        ))

        story.append(Paragraph(
            "We wish the candidate the greatest success in all future professional and academic endeavors.",
            body_style
        ))

        story.append(Spacer(1, 24))
        story.append(Paragraph("<b>For Anti-Matrix Technologies Pvt. Ltd.</b><br/><br/><br/><b>Director of Human Resources & Operations</b>", body_style))

        doc.build(story)
        return str(file_path)

    @classmethod
    def generate_agreement_pdf(
        cls,
        internship: Internship,
        document_no: str,
        issue_date_str: str = None
    ) -> str:
        """Generates an official Internship Agreement PDF."""
        storage_dir = cls.get_storage_dir()
        filename = f"Agreement_{document_no}.pdf"
        file_path = storage_dir / filename

        student = internship.student
        student_name = student.full_name if student else "Intern"
        student_uid = student.student_uid if student else "AM-INT"

        if not issue_date_str:
            issue_date_str = datetime.now(timezone.utc).strftime('%B %d, %Y')

        doc = SimpleDocTemplate(
            str(file_path),
            pagesize=A4,
            leftMargin=45,
            rightMargin=45,
            topMargin=40,
            bottomMargin=40
        )
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            'AgrTitle',
            fontName='Helvetica-Bold',
            fontSize=14,
            leading=17,
            textColor=colors.HexColor('#030A07'),
            alignment=1,
            spaceAfter=15
        )
        body_style = ParagraphStyle(
            'AgrBody',
            fontName='Helvetica',
            fontSize=9.5,
            leading=14.5,
            textColor=colors.HexColor('#1E293B'),
            spaceAfter=10
        )

        story = []
        story.append(Paragraph(f"<b>ANTI&bull;MATRIX TECHNOLOGIES</b> &bull; Document Ref: {document_no} &bull; Date: {issue_date_str}", body_style))
        story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#10B981'), spaceAfter=14))
        story.append(Paragraph("INTERNSHIP AGREEMENT & CODE OF CONDUCT", title_style))
        story.append(Paragraph(
            f"This Agreement is entered into between <b>Anti-Matrix Technologies</b> and <b>{student_name}</b> ({student_uid}). "
            "By participating in the Anti-Matrix Internship Program, the candidate agrees to maintain absolute confidentiality "
            "regarding proprietary technical assets, complete weekly milestone tasks with original source code, and uphold "
            "professional engineering integrity.",
            body_style
        ))
        story.append(Spacer(1, 20))
        story.append(Paragraph("<b>Signatures:</b><br/><br/><br/>_____________________ &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; _____________________<br/>Anti-Matrix Representative &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; Candidate Signature", body_style))

        doc.build(story)
        return str(file_path)

    # ──────────────────────────────────────────────────────────────────────────
    # Certificate & Document Issuance Operations
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def issue_certificate(
        cls,
        internship_id: int,
        admin_user_id: int,
        override_eligibility: bool = False,
        override_reason: str = None
    ) -> tuple:
        """
        Issues an authentic Completion Certificate for an internship:
        - Validates eligibility (unless authorized admin override is provided)
        - Generates tamper-proof certificate ID
        - Creates PDF with embedded QR verification code
        - Inserts/Updates IssuedDocument and CertificateVerification records
        - Dispatches in-portal + email completion notifications
        - Records immutable AuditLog
        """
        internship = db.session.get(Internship, internship_id)
        if not internship:
            raise ValueError("Internship not found.")

        # Eligibility check
        eligibility = cls.validate_certificate_eligibility(internship)
        if not eligibility['is_eligible'] and not override_eligibility:
            raise ValueError(f"Ineligible for certificate: {eligibility['reason']}")

        student = internship.student
        assignment = internship.active_assignment
        project = assignment.project if assignment else None

        student_name = student.full_name if student else "Intern"
        program_title = project.title if project else "Enterprise Software Engineering"
        duration = internship.duration_weeks_display if hasattr(internship, 'duration_weeks_display') else "3 Months"
        if not duration or duration == '0 Weeks':
            duration = "3 Months"

        start_date = cls.format_date_str(internship.start_date, "Commencement")
        end_date = cls.format_date_str(internship.end_date, "Completion")
        issue_date_str = datetime.now(timezone.utc).strftime('%B %d, %Y')

        # Generate unique certificate ID
        certificate_no = cls.generate_unique_doc_no('CERTIFICATE')
        try:
            public_verify_url = url_for('main.verify_certificate', certificate_id=certificate_no, _external=True)
        except (RuntimeError, Exception):
            public_verify_url = f"https://portal.antimatrix.co.in/verify/{certificate_no}"

        # Generate PDF
        pdf_path = cls.generate_certificate_pdf(
            internship=internship,
            certificate_no=certificate_no,
            issue_date_str=issue_date_str,
            public_verify_url=public_verify_url
        )
        rel_file_path = os.path.basename(pdf_path)

        # 1. IssuedDocument
        issued_doc = IssuedDocument(
            document_no=certificate_no,
            internship_id=internship.id,
            student_id=student.id,
            doc_type='CERTIFICATE',
            template_version='v2.0',
            file_path=rel_file_path,
            status='ISSUED',
            issued_by=admin_user_id,
            issued_at=datetime.now(timezone.utc)
        )
        db.session.add(issued_doc)

        # 2. CertificateVerification
        cert_ver = CertificateVerification.query.filter_by(internship_id=internship.id).first()
        if not cert_ver:
            cert_ver = CertificateVerification(
                certificate_no=certificate_no,
                internship_id=internship.id,
                student_name=student_name,
                program_title=program_title,
                duration=duration,
                start_date=start_date,
                end_date=end_date,
                issue_date=issue_date_str,
                status='Successfully Completed',
                qr_code_data=public_verify_url,
                created_at=datetime.now(timezone.utc)
            )
            db.session.add(cert_ver)
        else:
            cert_ver.certificate_no = certificate_no
            cert_ver.student_name = student_name
            cert_ver.program_title = program_title
            cert_ver.duration = duration
            cert_ver.start_date = start_date
            cert_ver.end_date = end_date
            cert_ver.issue_date = issue_date_str
            cert_ver.status = 'Successfully Completed'
            cert_ver.qr_code_data = public_verify_url

        # Optionally advance internship status to COMPLETED if not already
        if internship.status != 'COMPLETED':
            internship.status = 'COMPLETED'

        db.session.commit()

        # 3. Notification Dispatch
        try:
            try:
                cert_dl_url = url_for('student.download_document', doc_no=certificate_no, _external=True)
            except (RuntimeError, Exception):
                cert_dl_url = f"/documents/download/{certificate_no}"

            NotificationService.notify_internship_completed(
                internship_id=internship.id,
                certificate_url=cert_dl_url
            )
        except Exception as e:
            current_app.logger.warning(f"Notification error on certificate issuance: {e}")

        # 4. Audit Log
        audit = AuditLog(
            actor_id=admin_user_id,
            actor_role='ADMIN',
            action='CERTIFICATE_ISSUED',
            target_entity='CertificateVerification',
            target_id=certificate_no,
            details_json=str({
                'internship_id': internship.id,
                'student_id': student.id,
                'certificate_no': certificate_no,
                'override': override_eligibility,
                'override_reason': override_reason
            }),
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(audit)
        db.session.commit()

        return issued_doc, cert_ver

    @classmethod
    def revoke_certificate(cls, certificate_no: str, admin_user_id: int, reason: str) -> bool:
        """Revokes an issued certificate, updating verification records and audit logs."""
        if not reason or not reason.strip():
            raise ValueError("Revocation reason is required.")

        cert_ver = CertificateVerification.query.filter_by(certificate_no=certificate_no).first()
        issued_doc = IssuedDocument.query.filter_by(document_no=certificate_no).first()

        if not cert_ver and not issued_doc:
            raise ValueError("Certificate record not found.")

        if cert_ver:
            cert_ver.status = 'Revoked'
        if issued_doc:
            issued_doc.status = 'REVOKED'
            issued_doc.revocation_reason = reason.strip()

        audit = AuditLog(
            actor_id=admin_user_id,
            actor_role='ADMIN',
            action='CERTIFICATE_REVOKED',
            target_entity='CertificateVerification',
            target_id=certificate_no,
            details_json=str({
                'certificate_no': certificate_no,
                'reason': reason.strip()
            }),
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(audit)
        db.session.commit()
        return True

    @classmethod
    def reissue_certificate(cls, old_certificate_no: str, admin_user_id: int, reason: str) -> tuple:
        """
        Reissues a certificate:
        - Marks old certificate as REVOKED with superseding reason
        - Issues a fresh certificate with new unique certificate ID
        - Cross-references old and new certificates in audit log
        """
        cert_ver = CertificateVerification.query.filter_by(certificate_no=old_certificate_no).first()
        issued_doc = IssuedDocument.query.filter_by(document_no=old_certificate_no).first()

        internship_id = cert_ver.internship_id if cert_ver else (issued_doc.internship_id if issued_doc else None)
        if not internship_id:
            raise ValueError(f"Could not locate internship for certificate {old_certificate_no}.")

        # Revoke old
        cls.revoke_certificate(
            certificate_no=old_certificate_no,
            admin_user_id=admin_user_id,
            reason=f"Superseded by reissuance: {reason}"
        )

        # Issue new
        new_doc, new_cert = cls.issue_certificate(
            internship_id=internship_id,
            admin_user_id=admin_user_id,
            override_eligibility=True,
            override_reason=f"Reissuance of {old_certificate_no}: {reason}"
        )

        audit = AuditLog(
            actor_id=admin_user_id,
            actor_role='ADMIN',
            action='CERTIFICATE_REISSUED',
            target_entity='CertificateVerification',
            target_id=new_cert.certificate_no,
            details_json=str({
                'old_certificate_no': old_certificate_no,
                'new_certificate_no': new_cert.certificate_no,
                'reason': reason
            }),
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(audit)
        db.session.commit()

        return new_doc, new_cert

    @classmethod
    def issue_document(
        cls,
        doc_type: str,
        internship_id: int,
        admin_user_id: int,
        custom_notes: str = None
    ) -> IssuedDocument:
        """
        Generates and issues corporate letters (Offer, Joining, Experience, Agreement).
        Creates IssuedDocument record and logs audit history.
        """
        internship = db.session.get(Internship, internship_id)
        if not internship:
            raise ValueError("Internship not found.")

        student = internship.student
        doc_no = cls.generate_unique_doc_no(doc_type)

        if doc_type == 'OFFER_LETTER':
            pdf_path = cls.generate_offer_letter_pdf(internship, doc_no)
        elif doc_type == 'JOINING_LETTER':
            pdf_path = cls.generate_joining_letter_pdf(internship, doc_no)
        elif doc_type == 'EXPERIENCE_LETTER':
            pdf_path = cls.generate_experience_letter_pdf(internship, doc_no)
        elif doc_type == 'INTERNSHIP_AGREEMENT':
            pdf_path = cls.generate_agreement_pdf(internship, doc_no)
        else:
            raise ValueError(f"Unsupported document type: {doc_type}")

        rel_path = os.path.basename(pdf_path)

        issued_doc = IssuedDocument(
            document_no=doc_no,
            internship_id=internship.id,
            student_id=student.id,
            doc_type=doc_type,
            template_version='v2.0',
            file_path=rel_path,
            status='ISSUED',
            issued_by=admin_user_id,
            issued_at=datetime.now(timezone.utc)
        )
        db.session.add(issued_doc)

        audit = AuditLog(
            actor_id=admin_user_id,
            actor_role='ADMIN',
            action='DOCUMENT_ISSUED',
            target_entity='IssuedDocument',
            target_id=doc_no,
            details_json=str({
                'doc_type': doc_type,
                'internship_id': internship.id,
                'student_id': student.id,
                'notes': custom_notes
            }),
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(audit)
        db.session.commit()

        # Send notification to student
        try:
            try:
                doc_link = url_for('student.documents')
            except (RuntimeError, Exception):
                doc_link = '/documents'

            NotificationService.create_in_portal_notification(
                user_id=student.user_id,
                title=f"New Document Issued: {issued_doc.display_type}",
                message=f"Your official {issued_doc.display_type} ({doc_no}) has been generated and is ready for download.",
                notif_type='INFO',
                category='ALLOCATION',
                link=doc_link
            )
        except Exception as e:
            current_app.logger.warning(f"Notification error on document issuance: {e}")

        return issued_doc

    # ──────────────────────────────────────────────────────────────────────────
    # Document Upload & Review
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def upload_student_document(
        cls,
        student_id: int,
        doc_type: str,
        file_obj,
        uploaded_by_user_id: int = None
    ) -> Document:
        """
        Securely processes, validates, and stores student verification documents:
        - Validates extension and file size
        - Sanitizes filename to prevent directory traversal
        - Persists Document database record
        """
        student = db.session.get(Student, student_id)
        if not student:
            raise ValueError("Student record not found.")

        if not file_obj or not file_obj.filename:
            raise ValueError("No file provided for upload.")

        filename = secure_filename(file_obj.filename)
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if ext not in cls.ALLOWED_UPLOAD_EXTENSIONS:
            raise ValueError(f"File type '.{ext}' is not permitted. Allowed: {', '.join(cls.ALLOWED_UPLOAD_EXTENSIONS)}")

        # Read file data to check size and compute hash
        file_bytes = file_obj.read()
        file_size = len(file_bytes)
        if file_size > cls.MAX_UPLOAD_SIZE_BYTES:
            raise ValueError(f"File size exceeds maximum permitted limit of {cls.MAX_UPLOAD_SIZE_BYTES // (1024*1024)}MB.")

        # Determine MIME type
        mime_type = file_obj.mimetype or 'application/octet-stream'
        if ext == 'pdf':
            mime_type = 'application/pdf'
        elif ext in ['jpg', 'jpeg']:
            mime_type = 'image/jpeg'
        elif ext == 'png':
            mime_type = 'image/png'
        elif ext == 'docx':
            mime_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

        # Generate unique collision-safe filename
        unique_token = secrets.token_hex(6)
        safe_stored_filename = f"{student.student_uid}_{doc_type}_{unique_token}.{ext}"

        storage_dir = cls.get_storage_dir()
        dest_path = storage_dir / safe_stored_filename
        with open(dest_path, 'wb') as f:
            f.write(file_bytes)

        # Create Document record
        doc_record = Document(
            student_id=student.id,
            doc_type=doc_type.upper(),
            file_name=filename,
            file_path=safe_stored_filename,
            file_size=file_size,
            mime_type=mime_type,
            status='UPLOADED',
            uploaded_at=datetime.now(timezone.utc)
        )
        db.session.add(doc_record)

        audit = AuditLog(
            actor_id=uploaded_by_user_id or student.user_id,
            actor_role='STUDENT' if (uploaded_by_user_id == student.user_id) else 'STAFF',
            action='DOCUMENT_UPLOADED',
            target_entity='Document',
            target_id=str(doc_record.id),
            details_json=str({
                'student_id': student.id,
                'doc_type': doc_type,
                'file_name': filename,
                'file_size': file_size
            }),
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(audit)
        db.session.commit()

        return doc_record

    @classmethod
    def review_student_document(
        cls,
        document_id: int,
        admin_user_id: int,
        new_status: str,
        rejection_reason: str = None
    ) -> Document:
        """Approves, rejects, or flags student verification documents with audit logging."""
        valid_statuses = {'APPROVED', 'REJECTED', 'UNDER_REVIEW', 'RE_UPLOAD_REQUIRED'}
        if new_status not in valid_statuses:
            raise ValueError(f"Invalid status '{new_status}'. Permitted: {valid_statuses}")

        doc = db.session.get(Document, document_id)
        if not doc:
            raise ValueError("Document not found.")

        doc.status = new_status
        doc.verified_by = admin_user_id
        doc.verified_at = datetime.now(timezone.utc)
        if new_status in ['REJECTED', 'RE_UPLOAD_REQUIRED']:
            doc.rejection_reason = rejection_reason or "Document does not meet verification requirements."
        else:
            doc.rejection_reason = None

        audit = AuditLog(
            actor_id=admin_user_id,
            actor_role='ADMIN',
            action='DOCUMENT_STATUS_CHANGED',
            target_entity='Document',
            target_id=str(doc.id),
            details_json=str({
                'document_id': doc.id,
                'new_status': new_status,
                'rejection_reason': doc.rejection_reason
            }),
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(audit)
        db.session.commit()

        return doc

    # ──────────────────────────────────────────────────────────────────────────
    # Secure Download & Path Traversal Prevention
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def get_document_for_download(cls, doc_identifier, current_user_obj, is_issued: bool = True) -> tuple:
        """
        Enforces strict RBAC and directory traversal checks:
        - Students can only download their own issued or uploaded documents.
        - Verifies that resolved path lies strictly within storage directory.
        Returns: (absolute_file_path, download_filename, mime_type)
        """
        storage_dir = cls.get_storage_dir().resolve()

        if is_issued:
            # Can be queried by document_no or integer ID
            if isinstance(doc_identifier, int) or (isinstance(doc_identifier, str) and doc_identifier.isdigit()):
                doc = db.session.get(IssuedDocument, int(doc_identifier))
            else:
                doc = IssuedDocument.query.filter_by(document_no=str(doc_identifier).strip()).first()

            if not doc:
                raise FileNotFoundError("Issued corporate document not found.")

            # RBAC Check
            is_admin = getattr(current_user_obj, 'is_admin_or_staff', False) or getattr(current_user_obj, 'role', '') in ['admin', 'super_admin']
            if not is_admin:
                student = getattr(current_user_obj, 'student_profile', None)
                if not student or student.id != doc.student_id:
                    raise PermissionError("Access denied: You are not authorized to download this document.")

            stored_filename = os.path.basename(doc.file_path)
            download_name = f"{doc.doc_type}_{doc.document_no}.pdf"
            mime_type = 'application/pdf'
        else:
            doc = db.session.get(Document, int(doc_identifier))
            if not doc:
                raise FileNotFoundError("Uploaded document not found.")

            is_admin = getattr(current_user_obj, 'is_admin_or_staff', False) or getattr(current_user_obj, 'role', '') in ['admin', 'super_admin']
            if not is_admin:
                student = getattr(current_user_obj, 'student_profile', None)
                if not student or student.id != doc.student_id:
                    raise PermissionError("Access denied: You are not authorized to download this document.")

            stored_filename = os.path.basename(doc.file_path)
            download_name = doc.file_name or f"document_{doc.id}"
            mime_type = doc.mime_type or 'application/octet-stream'

        # Path Traversal Prevention
        target_path = (storage_dir / stored_filename).resolve()
        try:
            # Check if target_path is within storage_dir
            target_path.relative_to(storage_dir)
        except ValueError:
            raise PermissionError("Security violation: Path traversal detected.")

        if not target_path.exists() or not target_path.is_file():
            # Check static/uploads fallback
            fallback_dir = Path(current_app.root_path) / 'static' / 'uploads'
            fallback_path = (fallback_dir / stored_filename).resolve()
            if fallback_path.exists() and fallback_path.is_file():
                return str(fallback_path), download_name, mime_type
            raise FileNotFoundError(f"Physical document file does not exist on disk.")

        return str(target_path), download_name, mime_type
