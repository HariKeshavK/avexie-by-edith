"""Example: wrapping a tool with toaa_wrap — documentation-by-example.

This file shows other tracks exactly how to audit-wrap a tool function.
It is NOT a real feature — it exists so kb_search, run_code, generate_docx,
and every other tool author can copy the pattern.

Key differences from CONTRACTS.md §1.1 (illustrative signature):

  1. tool_fn is Callable[..., Awaitable] (async, **kwargs), not
     Callable[[dict], dict].  The existing OWUI tool convention passes
     keyword arguments, not a single dict.  toaa_wrap calls
     tool_fn(**tool_input) internally.

  2. Return type is Any (the raw tool result), not ToaaResult.
     Downstream code (process_tool_result, citation builder) expects
     the unwrapped value.  The audit record is written as a side effect.

  3. toaa_wrap is async — all OWUI tool functions are async.
"""

from avexie.toaa.wrapper import toaa_wrap


# ── Step 1: Define your tool as a plain async function ──
# This is the OWUI convention: async def, typed params, docstring.
# Do NOT write audit logic here — the wrapper handles it.

async def echo(message: str = '') -> dict:
    """Return the input message unchanged.
    :param message: The text to echo back.
    :return: dict with the echoed message.
    """
    if not message:
        raise ValueError('message must not be empty')
    return {'echoed': message}


# ── Step 2: Call it through toaa_wrap at the dispatch site ──
# In production, the middleware does this for you (see INTEGRATION.md).
# This function shows what that call looks like.

async def echo_audited(
    message: str,
    *,
    session_id: str,
    user_id: str,
) -> dict:
    """Call the echo tool through the TOAA audit wrapper."""
    return await toaa_wrap(
        tool_name='echo',
        tool_input={'message': message},
        tool_fn=echo,
        session_id=session_id,
        user_id=user_id,
    )
