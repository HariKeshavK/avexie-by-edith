import logging
from typing import Any, Awaitable, Callable, Optional

from avexie.toaa.models import ToaaAudit, ToaaAuditRecordModel

log = logging.getLogger(__name__)

REDACTED_KEYS = frozenset({
    'password', 'secret', 'token', 'api_key', 'apikey',
    'authorization', 'credential', 'private_key',
})


def _sanitize_input(obj: Any, depth: int = 0) -> Any:
    if depth > 10:
        return '<nested>'
    if isinstance(obj, dict):
        return {
            k: '<REDACTED>' if k.lower() in REDACTED_KEYS else _sanitize_input(v, depth + 1)
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_sanitize_input(item, depth + 1) for item in obj]
    return obj


def _safe_output(result: Any) -> Optional[dict]:
    if result is None:
        return None
    if isinstance(result, dict):
        return _sanitize_input(result)
    try:
        return {'value': str(result)[:10_000]}
    except Exception:
        return {'value': '<unserializable>'}


async def toaa_wrap(
    tool_name: str,
    tool_input: dict,
    tool_fn: Callable[..., Awaitable],
    *,
    session_id: str,
    user_id: str,
    requires_approval: bool = False,
) -> Any:
    """Wrap a tool call with a TOAA audit record.

    Writes a pending row BEFORE calling tool_fn, then updates it to
    success/error AFTER. Per CONTRACTS.md §1.1.
    """
    sanitized_input = _sanitize_input(tool_input)

    audit_record: Optional[ToaaAuditRecordModel] = None
    try:
        audit_record = await ToaaAudit.insert_pending(
            tool_name=tool_name,
            tool_input=sanitized_input,
            session_id=session_id,
            user_id=user_id,
            requires_approval=requires_approval,
        )
    except Exception:
        log.exception('TOAA: failed to insert pending audit row for %s', tool_name)

    if audit_record and requires_approval:
        await ToaaAudit.complete(
            audit_record.id,
            status='blocked',
            error_detail='Approval required (not yet implemented)',
        )
        return {'error': f'Tool {tool_name} requires approval (not yet implemented)'}

    try:
        result = await tool_fn(**tool_input)
    except Exception as e:
        if audit_record:
            try:
                await ToaaAudit.complete(
                    audit_record.id,
                    status='error',
                    error_detail=str(e)[:5_000],
                )
            except Exception:
                log.exception('TOAA: failed to record error for audit row %s', audit_record.id)
        raise

    if audit_record:
        try:
            await ToaaAudit.complete(
                audit_record.id,
                status='success',
                tool_output=_safe_output(result),
            )
        except Exception:
            log.exception('TOAA: failed to record success for audit row %s', audit_record.id)

    return result
