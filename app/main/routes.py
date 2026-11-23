from datetime import datetime, timezone
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from flask_login import current_user
from app import db
from app.models import CertificateVerification, AuditLog

main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def index():
    if current_user.is_authenticated:
        if session.get('auth_realm') == 'employee' or not current_user.is_admin_or_staff:
            return redirect(url_for('student.dashboard'))
        return redirect(url_for('admin.dashboard'))
    return redirect(url_for('auth.login'))

@main_bp.route('/verify', methods=['GET', 'POST'])
def verify_search():
    if request.method == 'POST':
        cert_id = request.form.get('certificate_id', '').strip()
        if cert_id:
            return redirect(url_for('main.verify_certificate', certificate_id=cert_id))
        flash('Please enter a valid Certificate Identification Number.', 'warning')
    return render_template('public/verify_search.html')

@main_bp.route('/verify/<certificate_id>')
def verify_certificate(certificate_id):
    clean_id = certificate_id.strip()
    cert = CertificateVerification.query.filter_by(certificate_no=clean_id).first()

    # Log public verification attempt in AuditLog
    try:
        audit = AuditLog(
            actor_id=None,
            actor_role='PUBLIC',
            action='CERTIFICATE_VERIFIED' if cert else 'CERTIFICATE_VERIFY_NOT_FOUND',
            target_entity='CertificateVerification',
            target_id=clean_id,
            details_json=str({
                'certificate_id': clean_id,
                'status': cert.status if cert else 'NOT_FOUND',
                'user_agent': request.headers.get('User-Agent', '')[:120]
            }),
            ip_address=request.remote_addr,
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(audit)
        db.session.commit()
    except Exception:
        db.session.rollback()

    if request.is_json or request.headers.get('Accept') == 'application/json':
        if cert:
            return jsonify({
                'valid': cert.is_valid,
                'status': cert.status,
                'certificate_no': cert.certificate_no,
                'student_name': cert.student_name,
                'program_title': cert.program_title,
                'duration': cert.duration,
                'start_date': cert.start_date,
                'end_date': cert.end_date,
                'issue_date': cert.issue_date,
                'issuing_organization': 'ANTI MATRIX'
            })
        return jsonify({
            'valid': False,
            'status': 'NOT_FOUND',
            'certificate_no': clean_id,
            'message': 'No authentic Anti-Matrix certificate was found matching this identification number.'
        }), 404

    return render_template('public/verify_result.html', cert=cert, cert_id=clean_id)
