import asyncio

_stop_events: dict[str, asyncio.Event] = {}


def start_generation(conversation_id: str) -> asyncio.Event:
    event = asyncio.Event()
    _stop_events[conversation_id] = event
    return event


def request_stop(conversation_id: str) -> bool:
    event = _stop_events.get(conversation_id)
    if event is None:
        return False
    event.set()
    return True


def finish_generation(conversation_id: str) -> None:
    _stop_events.pop(conversation_id, None)
