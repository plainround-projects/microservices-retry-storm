"""The resilience policy of the chain, as plain objects with no I/O.

Every class takes its clock (and sleeping, and randomness) as an argument, so each behavior is a unit test with a
fake clock instead of a timing experiment. Services import this package from their domain and application layers.
"""
