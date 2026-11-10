"""Single application route registry; importing a domain never imports app.main."""


def register_routers(app):
    from app.routes import public, auth, account, developer, admin, catalog_admin
    from app import public_stats, public_gateway_secure, cliproxy_gateway, ai_console, credit_console, credit_payments
    for module in (public_stats, public_gateway_secure, cliproxy_gateway, ai_console,
                   credit_console, credit_payments, public, auth, account, developer, admin, catalog_admin):
        app.include_router(module.router)
