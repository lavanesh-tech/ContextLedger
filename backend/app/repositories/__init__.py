"""Data-access layer. Repositories build queries but never commit: services own
transactions. Tenant-owned repositories are bound to one organization at
construction time, so every query they run is tenant-filtered."""
