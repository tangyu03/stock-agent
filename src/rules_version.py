"""Global, immutable rules version used by decision audit logs."""

RULES_VERSION = "v2.6"


def get_rules_version() -> str:
    return RULES_VERSION
