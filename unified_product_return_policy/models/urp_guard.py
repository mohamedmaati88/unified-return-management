"""Server-only privilege markers for the return policy.

Odoo contexts can be sent by any client (JSON-RPC ``context`` argument). A plain value
such as ``urp_internal=True`` could therefore be forged to disable a protection. The
markers below are private Python objects: a JSON payload can only produce strings,
numbers, lists and dicts, never one of these instances, so ``is`` checks cannot be
satisfied from outside the server code of this module.
"""


class _UrpMarker:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "<urp %s>" % self.name

    def __copy__(self):                        # copies keep the identity (``is`` checks)
        return self

    def __deepcopy__(self, memo):
        return self


INTERNAL = _UrpMarker("internal")          # module code writes snapshot / protected fields
CHECKED = _UrpMarker("checked")            # return check wizard already shown
SPLIT = _UrpMarker("split")                # backorder moves created by the validation of their move


class UrpPayload:
    """Server-side data passed through the context (batch map, POS sync metadata)."""
    __slots__ = ("data",)

    def __init__(self, data):
        self.data = data


def urp_internal(records):
    return records.with_context(urp_internal=INTERNAL)


def is_urp_internal(env):
    return env.context.get("urp_internal") is INTERNAL


def urp_checked(records):
    return records.with_context(urp_checked=CHECKED)


def is_urp_checked(env):
    return env.context.get("urp_checked") is CHECKED


def urp_split(records):
    return records.with_context(urp_split=SPLIT)


def is_urp_split(env):
    return env.context.get("urp_split") is SPLIT


def urp_payload(env, key, default=None):
    """Return the server-side payload stored under ``key``; values forged by a client are ignored."""
    value = env.context.get(key)
    return value.data if isinstance(value, UrpPayload) else default
