"""Canonical Sprint 0 RBAC permission matrix.

Roles
-----
- Administrator, Clinical Director: organization-scoped (OrganizationRoleGrant)
- Supervisor, Supervisee: relationship-scoped (DEC-057) — never a single
  global attribute on the user account
- Supervisor in Phase 1 is BCBA only (enforced when creating a relationship)

RBAC-003 / DEC-061 / REQ-024
----------------------------
An Administrator who is personally named Supervisor or Supervisee on a
specific SupervisoryRelationship may Edit/View that one relationship.
An Administrator who is not a party is denied any relationship access.
The exception grants no other clinical right.

M3 facilitation (client review, Sep 2026)
-----------------------------------------
An Administrator may create an eligible Supervisor-Supervisee relationship
without being a party to it. Creating it grants no access afterwards: the
record, Intake and IDP stay with the parties (and Clinical Director oversight).
"""

from apps.rbac.models import OrgRole, RelationshipRole

# Every approved role × permission. Values: True / False / "self-party"
PERMISSIONS = [
    "org.settings.manage",
    "org.users.manage",
    "relationship.create",
    "relationship.edit",
    "relationship.view",
    "relationship.conclude",
    "intake.manage",
    "idp.manage",
    "idp.view",
    "cycle.view",
    "assignment.create",
    "assignment.submit",
    "hours.log",
    "hours.verify",
    "session.document",
    "session.view",
    "assessment.manage",
    "assessment.view",
    "compliance.view",
    "reports.export",
    "audit.view",
    "clinical.view",
]

MATRIX = {
    OrgRole.ADMINISTRATOR: {
        "org.settings.manage": True,
        "org.users.manage": True,
        "relationship.create": True,
        "relationship.edit": "self-party",
        "relationship.view": "self-party",
        "relationship.conclude": False,
        "intake.manage": False,
        "idp.manage": False,
        "idp.view": False,
        "cycle.view": False,
        "assignment.create": False,
        "assignment.submit": False,
        "hours.log": False,
        "hours.verify": False,
        "session.document": False,
        "session.view": False,
        "assessment.manage": False,
        "assessment.view": False,
        "compliance.view": False,
        "reports.export": True,
        "audit.view": True,
        "clinical.view": False,
    },
    OrgRole.CLINICAL_DIRECTOR: {
        "org.settings.manage": False,
        "org.users.manage": False,
        "relationship.create": False,
        "relationship.edit": False,
        "relationship.view": True,
        "relationship.conclude": False,
        "intake.manage": False,
        "idp.manage": False,
        "idp.view": True,
        "cycle.view": True,
        "assignment.create": False,
        "assignment.submit": False,
        "hours.log": False,
        "hours.verify": False,
        "session.document": False,
        "session.view": True,
        "assessment.manage": False,
        "assessment.view": True,
        "compliance.view": True,
        "reports.export": True,
        "audit.view": True,
        "clinical.view": True,
    },
    RelationshipRole.SUPERVISOR: {
        "org.settings.manage": False,
        "org.users.manage": False,
        "relationship.create": True,
        "relationship.edit": True,
        "relationship.view": True,
        "relationship.conclude": True,
        "intake.manage": True,
        "idp.manage": True,
        "idp.view": True,
        "cycle.view": True,
        "assignment.create": True,
        "assignment.submit": False,
        "hours.log": False,
        "hours.verify": True,
        "session.document": True,
        "session.view": True,
        "assessment.manage": True,
        "assessment.view": True,
        "compliance.view": True,
        "reports.export": True,
        "audit.view": True,
        "clinical.view": True,
    },
    RelationshipRole.SUPERVISEE: {
        "org.settings.manage": False,
        "org.users.manage": False,
        "relationship.create": False,
        "relationship.edit": False,
        "relationship.view": True,
        "relationship.conclude": False,
        "intake.manage": False,
        "idp.manage": False,
        "idp.view": True,
        "cycle.view": True,
        "assignment.create": False,
        "assignment.submit": True,
        "hours.log": True,
        "hours.verify": False,
        "session.document": False,
        "session.view": True,
        "assessment.manage": False,
        "assessment.view": True,
        "compliance.view": True,
        "reports.export": False,
        "audit.view": False,
        "clinical.view": True,
    },
}

# Rights granted solely by the RBAC-003 Administrator self-party exception.
RBAC_003_RIGHTS = frozenset({
    "relationship.edit",
    "relationship.view",
})


def matrix_as_dict():
    return {
        "permissions": PERMISSIONS,
        "roles": {
            "administrator": MATRIX[OrgRole.ADMINISTRATOR],
            "clinical_director": MATRIX[OrgRole.CLINICAL_DIRECTOR],
            "supervisor": MATRIX[RelationshipRole.SUPERVISOR],
            "supervisee": MATRIX[RelationshipRole.SUPERVISEE],
        },
        "notes": {
            "supervisor_phase1": "Supervisor role is BCBA only (Version 2.3).",
            "dec_057": "Supervisor/Supervisee are evaluated per SupervisoryRelationship.",
            "dec_058": (
                "Supervisee population includes BCBAs generally. A BCBA may be assigned as "
                "Supervisee for professional development, consultation, competency development, "
                "leadership development, or case discussion. No fifth role. Not itself compliance-bearing."
            ),
            "rbac_003": "Administrator self-party exception is limited to relationship edit/view.",
            "m3_facilitation": (
                "Administrator may create an eligible relationship without being a party; "
                "this grants no access to the record afterwards."
            ),
        },
    }
