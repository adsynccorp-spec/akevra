from drf_spectacular.extensions import OpenApiAuthenticationExtension


class SessionTokenScheme(OpenApiAuthenticationExtension):
    target_class = "apps.accounts.authentication.SessionTokenAuthentication"
    name = "bearerAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "Token",
            "description": (
                "Paste the token from POST /api/v1/auth/login (or MFA / workspace). "
                "Swagger will send it as: Authorization: Bearer <token>"
            ),
        }
