"""Booking domain: availability rules and the bounded booking service.

``booking.availability`` is pure: no I/O, plain values in and out. ``booking.service`` is the only
module in the domain that reaches the database, and only through ``postgres.Database``.
"""
