# fiber.node.reflex.flow.event
from __future__ import annotations
from collections import defaultdict
from collections.abc import Callable
from enum import Enum
import itertools
from typing import Any

class EngineError(Exception):
    def __init__(self, value: Any) -> None:
        super().__init__(value)
        self.value = value

    def __str__(self) -> str:
        return repr(self.value)

def listify(obj: Any) -> list[Any] | tuple[Any, ...]:
    if obj is None:
        return []
    return obj if isinstance(obj, (list, tuple)) else [obj]

class State:
    def __init__(
        self,
        name: str | Enum,
        on_enter: Any = None,
        on_exit: Any = None,
        ignore_invalid_triggers: bool | None = None,
        final: bool = False,
    ) -> None:
        self._name = name
        self.final = final
        self.ignore_invalid_triggers = ignore_invalid_triggers
        self.on_enter: list[Callable[..., Any]] = list(listify(on_enter))
        self.on_exit: list[Callable[..., Any]] = list(listify(on_exit))

    @property
    def name(self) -> str:
        return self._name.name if isinstance(self._name, Enum) else self._name

    @property
    def value(self) -> str | Enum:
        return self._name

    def enter(self, event_data: EventData) -> None:
        event_data.machine.callbacks(self.on_enter, event_data)

    def exit(self, event_data: EventData) -> None:
        event_data.machine.callbacks(self.on_exit, event_data)

    def add_callback(self, trigger: str, func: Callable[..., Any]) -> None:
        getattr(self, f"on_{trigger}").append(func)

    def __repr__(self) -> str:
        return f"<State('{self.name}')>"

class Condition:
    def __init__(self, func: Callable[..., Any], target: bool = True) -> None:
        self.func = func
        self.target = target

    def check(self, event_data: EventData) -> bool:
        predicate = event_data.machine.resolve_callable(self.func, event_data)
        if event_data.machine.send_event:
            return bool(predicate(event_data) == self.target)
        return bool(predicate(*event_data.args, **event_data.kwargs) == self.target)

class Transition:
    condition_cls = Condition

    def __init__(
        self,
        source: str,
        dest: str | None,
        conditions: Any = None,
        unless: Any = None,
        before: Any = None,
        after: Any = None,
        prepare: Any = None,
    ) -> None:
        self.source = source
        self.dest = dest
        self.prepare: list[Any] = list(listify(prepare))
        self.before: list[Any] = list(listify(before))
        self.after: list[Any] = list(listify(after))
        self.conditions: list[Condition] = [
            *(self.condition_cls(c, True) for c in listify(conditions)),
            *(self.condition_cls(u, False) for u in listify(unless)),
        ]

    def execute(self, event_data: EventData) -> bool:
        machine = event_data.machine
        machine.callbacks(self.prepare, event_data)
        if not all(c.check(event_data) for c in self.conditions):
            return False

        machine.callbacks(itertools.chain(machine.before_state_change, self.before), event_data)
        if self.dest is not None:
            machine.get_state(self.source).exit(event_data)
            machine.set_state(self.dest, event_data.model)
            event_data.update(machine.state)
            dest_state = machine.get_state(self.dest)
            dest_state.enter(event_data)
            if dest_state.final:
                machine.callbacks(machine.on_final, event_data)

        machine.callbacks(itertools.chain(self.after, machine.after_state_change), event_data)
        return True

    def __repr__(self) -> str:
        return f"<Transition('{self.source}' -> '{self.dest}')>"

class EventData:
    def __init__(self, state: State | None, event: Event, machine: Any, model: Any, args: Any, kwargs: Any) -> None:
        self.state = state
        self.event = event
        self.machine = machine
        self.model = model
        self.args = args
        self.kwargs = kwargs
        self.transition: Transition | None = None
        self.error: BaseException | None = None
        self.result = False

    def update(self, state: State | str | Enum) -> None:
        self.state = state if isinstance(state, State) else self.machine.get_state(state)

class Event:
    def __init__(self, name: str, machine: Any) -> None:
        self.name = name
        self.machine = machine
        self.transitions: defaultdict[str, list[Transition]] = defaultdict(list)

    def add_transition(self, transition: Transition) -> None:
        self.transitions[transition.source].append(transition)

    def trigger(self, model: Any, *args: Any, **kwargs: Any) -> bool:
        event_data = EventData(self.machine.get_state(self.machine.state), self, self.machine, model, args, kwargs)
        return self._trigger(event_data)

    def _trigger(self, event_data: EventData) -> bool:
        state = event_data.state
        if state is None or state.name not in self.transitions:
            if state and state.ignore_invalid_triggers or self.machine.ignore_invalid_triggers:
                return False
            raise EngineError(f"Can't trigger event '{self.name}' from state '{state.name if state else None}'!")

        try:
            self.machine.callbacks(self.machine.prepare_event, event_data)
            for trans in self.transitions[state.name]:
                event_data.transition = trans
                if trans.execute(event_data):
                    event_data.result = True
                    break
        except BaseException as err:
            event_data.error = err
            if self.machine.on_exception:
                self.machine.callbacks(self.machine.on_exception, event_data)
            else:
                raise
        finally:
            self.machine.callbacks(self.machine.finalize_event, event_data)
        return event_data.result