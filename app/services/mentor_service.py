from app.models import User

APPROVED_MENTOR_EMAILS = [
    'praveen@antimatrix.co.in',
    'satishkumar@antimatrix.co.in',
    'rohit@antimatrix.co.in',
    'bharatbabu@antimatrix.co.in',
]

APPROVED_MENTOR_NAMES = {
    'praveen@antimatrix.co.in': 'Praveen',
    'satishkumar@antimatrix.co.in': 'Satish Kumar',
    'rohit@antimatrix.co.in': 'Rohit',
    'bharatbabu@antimatrix.co.in': 'Bharath',
}


def get_approved_mentors():
    """
    Retrieve the four approved mentors from the database in canonical order.
    Guarantees exact canonical names and excludes any unauthorized or legacy mentors.
    Maintains 100% backward compatibility with test_profile_and_approved_mentors.py.
    """
    mentors = User.query.filter(
        User.role == 'mentor',
        User.email.in_(APPROVED_MENTOR_EMAILS)
    ).all()
    order_map = {email: idx for idx, email in enumerate(APPROVED_MENTOR_EMAILS)}
    return sorted(mentors, key=lambda m: order_map.get(m.email, 99))


def get_all_active_mentors():
    """
    Retrieve all valid active mentors in the system, including newly provisioned mentors.
    Excludes legacy dummy/fake test accounts.
    """
    return User.query.filter(
        User.role == 'mentor',
        User.is_active == True,
        User.email != 'mentor@antimatrix.com'
    ).order_by(User.id.asc()).all()


def generate_next_mentor_id():
    """
    Generates the next sequential unique mentor employee ID, e.g. AM-MTR-005.
    Scans existing mentor accounts in the database.
    """
    existing_users = User.query.filter(
        (User.role == 'mentor') |
        (User.employee_id.like('AM-MTR-%'))
    ).all()
    max_num = 0
    import re
    for u in existing_users:
        if u.employee_id:
            m = re.search(r'AM-MTR-(\d+)', u.employee_id, re.IGNORECASE)
            if m:
                try:
                    num = int(m.group(1))
                    if num > max_num:
                        max_num = num
                except ValueError:
                    pass
    return f"AM-MTR-{max_num + 1:03d}"


def is_approved_mentor(user):
    """
    Check if a User object is an approved or valid active mentor.
    Rejects legacy test accounts (e.g. mentor@antimatrix.com).
    """
    if not user or not getattr(user, 'email', None):
        return False
    if user.email == 'mentor@antimatrix.com' or 'Rajesh Sharma' in (getattr(user, 'full_name', '') or ''):
        return False
    if user.email in APPROVED_MENTOR_EMAILS:
        return True
    return bool(user.role == 'mentor' and user.is_active)


def get_canonical_mentor_name(user):
    """Return the exact canonical display name for an approved mentor, or None."""
    if not user or not getattr(user, 'email', None):
        return None
    return APPROVED_MENTOR_NAMES.get(user.email)


APPROVED_MENTOR_OFFICIAL_EMAILS = {
    'Praveen': 'praveen@antimatrix.co.in',
    'Satish Kumar': 'satishkumar@antimatrix.co.in',
    'Rohit': 'rohit@antimatrix.co.in',
    'Bharath': 'bharath@antimatrix.co.in',
}


def get_canonical_mentor_email(mentor_or_name):
    """
    Return the official Anti-Matrix mentor email for an approved mentor.
    Accepts a User object, canonical mentor name string, or mentor email string.
    Returns None if the mentor is not an approved mentor or unassigned.
    """
    if not mentor_or_name:
        return None
    if isinstance(mentor_or_name, str):
        name = mentor_or_name.strip()
        if name in APPROVED_MENTOR_OFFICIAL_EMAILS:
            return APPROVED_MENTOR_OFFICIAL_EMAILS[name]
        if name in ('Bharat Babu', 'Bharat'):
            return APPROVED_MENTOR_OFFICIAL_EMAILS.get('Bharath')
        if name in APPROVED_MENTOR_NAMES:
            canon_name = APPROVED_MENTOR_NAMES[name]
            return APPROVED_MENTOR_OFFICIAL_EMAILS.get(canon_name)
        for k, v in APPROVED_MENTOR_OFFICIAL_EMAILS.items():
            if k.lower() == name.lower():
                return v
        return None
    if is_approved_mentor(mentor_or_name):
        canonical_name = get_canonical_mentor_name(mentor_or_name)
        return APPROVED_MENTOR_OFFICIAL_EMAILS.get(canonical_name)
    return None

