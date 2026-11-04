"""Helpers available to user-authored journey modules."""

def require(condition, message):
    # Explicit checks remain active with PYTHONOPTIMIZE=1.
    if not condition:
        raise AssertionError(message)


def payload(response, status):
    transport = getattr(response, 'error', None)
    names = []
    for _ in range(4):
        if transport is None:
            break
        names.append(type(transport).__name__)
        transport = getattr(transport, 'reason', None) or getattr(transport, '__cause__', None) or (
            transport.args[0] if getattr(transport, 'args', ()) and isinstance(transport.args[0], Exception) else None)
    detail = f' ({"/".join(names)})' if names else ''
    require(response.status_code == status, f'Expected HTTP {status}, received {response.status_code}{detail}')
    value = response.json()
    require(isinstance(value, dict), 'Expected a JSON object')
    return value


