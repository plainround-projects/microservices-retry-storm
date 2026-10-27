"""Service chassis shared by the four services: everything that is infrastructure and not policy.

Settings, logging, trace context, metrics, one-attempt upstream calls, health and the database pool live here. The
decisions (when to retry, when to open a breaker) live in the ``resilience`` package and in each service.
"""
