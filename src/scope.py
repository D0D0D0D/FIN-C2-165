"""Single source of truth for the LC-preflight KB partition contract."""

SCOPE_KEYS: tuple[str, ...] = ("tenant_id",)


def scope_for(tenant_id: str) -> dict[str, str]:
    """Build the exact one-axis scope accepted by all scoped parts."""
    return {"tenant_id": tenant_id}
