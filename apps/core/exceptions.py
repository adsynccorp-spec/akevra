class HardDeleteNotAllowed(Exception):
    """Permanent deletion is unavailable anywhere in the system."""


class AccountLocked(Exception):
    def __init__(self, locked_until=None):
        self.locked_until = locked_until
        super().__init__("Account is locked")


class AuthenticationFailed(Exception):
    pass


class MFARequired(Exception):
    pass


class WorkspaceSelectionRequired(Exception):
    pass


class PermissionDeniedError(Exception):
    pass
