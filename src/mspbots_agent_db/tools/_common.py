from .._json import error_envelope

NO_TOKEN = error_envelope(
    "not_configured",
    "No Agent Data Core credentials. Send the X-MSP-Host, X-API-Key, and "
    "X-MSP-Tenant-Id headers.",
    False,
)
