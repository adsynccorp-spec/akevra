"""AKEVRA password policy, shared by invitation activation and password reset.

Mirrors the requirements checklist on the frontend, then runs Django's validators.
"""

import re

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from apps.accounts.models import Identity


class PasswordPolicyError(Exception):
    """The new password does not meet the policy (HTTP 400)."""


def check_new_password(password, email):
    problems = []
    if len(password) < 8:
        problems.append("at least 8 characters")
    if not re.search(r"[A-Z]", password):
        problems.append("one uppercase letter")
    if not re.search(r"\d", password):
        problems.append("one number")
    if not re.search(r"[^A-Za-z0-9]", password):
        problems.append("one special character")
    if problems:
        raise PasswordPolicyError("Password needs " + ", ".join(problems) + ".")
    try:
        validate_password(password, user=Identity(email=email))
    except ValidationError as exc:
        raise PasswordPolicyError(" ".join(exc.messages)) from exc
