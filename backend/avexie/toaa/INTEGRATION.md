# TOAA Integration Guide

How to wire `toaa_wrap` into the tool dispatch choke points in
`backend/avexie/utils/middleware.py`. This file is outside the `toaa/`
track per CONTRACTS.md §0, so the middleware owner should apply these
changes.

## Overview

There are **three** tool dispatch sites in `middleware.py`. All three must
be wrapped so that every tool call produces an audit row.

## Dispatch site 1: Legacy JSON-parsing function calling

**File:** `backend/avexie/utils/middleware.py`  
**Function:** `chat_completion_tools_handler` → inner `tool_call_handler`  
**Lines:** 1392–1407

```python
# BEFORE (current code, line 1405-1407):
                    else:
                        tool_function = tool['callable']
                        tool_result = await tool_function(**tool_function_params)

# AFTER:
                    else:
                        from avexie.toaa.wrapper import toaa_wrap
                        tool_function = tool['callable']
                        tool_result = await toaa_wrap(
                            tool_name=tool_function_name,
                            tool_input=tool_function_params,
                            tool_fn=tool_function,
                            session_id=metadata.get('session_id', ''),
                            user_id=user.id,
                        )
```

## Dispatch site 2: Filter-invoked tool calls

**Function:** `execute_tool_call_for_output`  
**Lines:** 3191–3198

```python
# BEFORE (current code, line 3190-3198):
        else:
            function = await get_updated_tool_function(
                function=tool['callable'],
                extra_params={...},
            )
            result = await function(**params)

# AFTER:
        else:
            from avexie.toaa.wrapper import toaa_wrap
            function = await get_updated_tool_function(
                function=tool['callable'],
                extra_params={...},
            )
            result = await toaa_wrap(
                tool_name=name,
                tool_input=params,
                tool_fn=function,
                session_id=metadata.get('session_id', ''),
                user_id=user.id,
            )
```

## Dispatch site 3: Native FC streaming loop

**Function:** Inner `execute_tool_call` in the streaming handler  
**Lines:** 5647–5654

```python
# BEFORE (current code, line 5646-5654):
                            else:
                                function = await get_updated_tool_function(
                                    function=tool['callable'],
                                    extra_params={...},
                                )
                                result = await function(**params)

# AFTER:
                            else:
                                from avexie.toaa.wrapper import toaa_wrap
                                function = await get_updated_tool_function(
                                    function=tool['callable'],
                                    extra_params={...},
                                )
                                result = await toaa_wrap(
                                    tool_name=name,
                                    tool_input=params,
                                    tool_fn=function,
                                    session_id=metadata.get('session_id', ''),
                                    user_id=user.id,
                                )
```

## Direct/MCP tools

Each dispatch site also has a `direct_tool` branch that calls
`event_caller(...)` instead of a callable. To audit those:

```python
# Wrap the event_caller call similarly:
from avexie.toaa.wrapper import toaa_wrap

async def _direct_tool_fn(**_kwargs):
    return await event_caller({
        'type': 'execute:tool',
        'data': {
            'id': str(uuid4()),
            'name': tool_function_name,
            'params': tool_function_params,
            'server': tool.get('server', {}),
            'session_id': metadata.get('session_id', None),
        },
    })

tool_result = await toaa_wrap(
    tool_name=tool_function_name,
    tool_input=tool_function_params,
    tool_fn=_direct_tool_fn,
    session_id=metadata.get('session_id', ''),
    user_id=user.id,
)
```

## Migration

The Alembic migration at
`backend/avexie/migrations/versions/e2a1b3c4d5f6_add_toaa_audit_record_table.py`
will run automatically at startup (if `ENABLE_DB_MIGRATIONS` is true).
No manual SQL is needed.
