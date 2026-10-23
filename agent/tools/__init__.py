"""Tool registry.

Every handler is a plain synchronous function. It returns a dict with any of:
    text    : str          -> given back to Claude
    images  : [Path/str]   -> shown to Claude AND sent to Discord
    files   : [Path/str]   -> sent to Discord only
"""

SCHEMAS = []
HANDLERS = {}


def tool(name, description, properties, required=()):
    def decorate(fn):
        SCHEMAS.append(
            {
                "name": name,
                "description": description,
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": list(required),
                },
            }
        )
        HANDLERS[name] = fn
        return fn

    return decorate


def load_all():
    from . import (  # noqa: F401
        apps, browser, files, memory, messaging, network, schedules, screen,
        shell, system, uinput, voice_tool,
    )

    return SCHEMAS, HANDLERS
