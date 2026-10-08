"""Failure taxonomy, retry/repair policies and unit-failure records.

``errors``    what kind of failure it is, and what that kind means (one table)
``policies``  every retry and repair budget (one table)
``retry``     the only two retry loops: transient infrastructure, model-output repair
``units``     failure records at unit boundaries and their stage-level collection
"""
