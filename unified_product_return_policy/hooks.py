def post_init_hook(env):
    """Write the opening (baseline) policy-history row for every company.

    Products and categories need no baseline: the customisation fields are new and
    therefore False for every record at install time.
    """
    env["urp.policy.history"]._urp_write_baseline()
