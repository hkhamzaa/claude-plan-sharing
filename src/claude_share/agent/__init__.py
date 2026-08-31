"""Local agent layer: "which user, on which device, acting as which pool
member" - a thin identity/config layer above QuotaService/CapacityService.

Nothing here does networking (there is no central server until Milestone 5)
or real authentication (see docs/architecture.md: "login" here just points
a machine at an already-existing member_id, it does not create accounts or
verify a credential). This package only reads/writes a local config file
and composes existing application-layer services - it introduces no new
quota-math or persistence concerns of its own beyond the `Device` model.
"""
