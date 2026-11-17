# fiber.node.reflex.flow.engine
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from enum import Enum
from typing import Any

from xphi.arch.contract.registry.flow import FlowDSL, FlowRegistry, load_sample_registry
from xphi.arch.contract.reflex.flow import (
    CompiledChoice,
    CompiledNode,
    CompiledTask,
    CompiledTerminal,
)
from xphi.arch.contract.reflex.fsm import (
    ASL_CHOICE_OP_MAP,
    END_TOKEN,
    EdgeMode,
    NodeType,
    parse_resource_uri,
)
from xphi.arch.xor.fsm.compiler import FSMCompiler
from xphi.arch.xor.fsm.parser import (
    ASLValidationError,
    apply_projection,
    bind_parameters,
    eval_choice_rule,
    verify_asl,
)
from xphi.node.runtime.reflex.event import (
    EngineError,
    Event,
    EventData,
    State,
    Transition,
    listify,
)

DispatcherFn = Callable[[str, str, dict[str, Any]], dict[str, Any] | None]

class FlowExecutionError(EngineError):
    """Raised when an event flow encounters an unhandled failure or deadlock."""

class FlowEngine:
    """Compact FSM & ASL Workflow Engine."""

    def __init__(
        self,
        states: Any = None,
        initial: str | State | Enum | None = "initial",
        transitions: Any = None,
        *,
        send_event: bool = False,
        ignore_invalid_triggers: bool | None = None,
        before_state_change: Any = None,
        after_state_change: Any = None,
        prepare_event: Any = None,
        finalize_event: Any = None,
        on_exception: Any = None,
        on_final: Any = None,
        name: str | None = None,
    ) -> None:
        self.name = f"{name}: " if name else ""
        self.states: OrderedDict[str, State] = OrderedDict()
        self.events: OrderedDict[str, Event] = OrderedDict()
        self.send_event = send_event
        self.ignore_invalid_triggers = ignore_invalid_triggers

        self.prepare_event: list[Any] = list(listify(prepare_event))
        self.before_state_change: list[Any] = list(listify(before_state_change))
        self.after_state_change: list[Any] = list(listify(after_state_change))
        self.finalize_event: list[Any] = list(listify(finalize_event))
        self.on_exception: list[Any] = list(listify(on_exception))
        self.on_final: list[Any] = list(listify(on_final))

        self._initial: str | None = None
        self.state: str = ""
        self.context: dict[str, Any] = {}
        self._default_context: dict[str, Any] = {}
        self.last_error: BaseException | None = None
        self._compiled_nodes: dict[str, CompiledNode] = {}
        self._dispatcher: DispatcherFn | None = None

        if states is not None:
            self.add_states(states)
        if initial is not None:
            self.initial = initial
        if transitions is not None:
            self.add_transitions(transitions)

    # =====================================================================
    # 1. ASL & Contract Graph Builders (FSMCompiler-backed)
    # =====================================================================

    @staticmethod
    def compile_asl_nodes(
        asl: dict[str, Any],
        *,
        validate: bool = True,
    ) -> tuple[str, dict[str, CompiledNode]]:
        if validate and (errors := verify_asl(asl)):
            raise ASLValidationError("ASL validation failed:\n" + "\n".join(f"  - {e}" for e in errors))

        states = asl.get("States", {})
        start_at = asl.get("StartAt", "")
        if start_at not in states:
            raise KeyError(f"StartAt state '{start_at}' not found in ASL States.")

        norm_states = {
            k: ({**v, "Type": "Task", "Resource": "pass://noop"} if v.get("Type") == "Pass" else v)
            for k, v in states.items()
        }
        compiler = FSMCompiler(use_wasm_linker=False)
        sig = compiler._build_fragment_sig({**asl, "States": norm_states}, trace_id="runtime")

        table: dict[str, CompiledNode] = {}
        needs_ok, needs_fail = False, False

        for name, frag in sig.nodes.items():
            raw = states[name]
            stype = raw.get("Type")

            if stype in ("Succeed", "Fail"):
                table[name] = CompiledTerminal(name=name, succeeded=(stype == "Succeed"))
            elif frag.type == NodeType.ROUTER:
                raw_choices = raw.get("Choices", [])
                cond_edges = [e for e in frag.relations if e.edge_type == EdgeMode.CONDITIONAL]
                rules = tuple(
                    (
                        {"if": e.condition}
                        if "if" in rc or (any(k in ASL_CHOICE_OP_MAP for k in rc) and not any(k.endswith("Path") for k in rc))
                        else {k: v for k, v in rc.items() if k != "Next"},
                        e.target,
                    )
                    for rc, e in zip(raw_choices, cond_edges)
                )
                direct = [e.target for e in frag.relations if e.edge_type == EdgeMode.DIRECT]
                if not direct:
                    needs_fail = True
                table[name] = CompiledChoice(
                    name=name,
                    rules=rules,
                    default_state=direct[0] if direct else "fail_end",
                )
            else:
                io = compiler._cas_pool.get(frag.attributes.io_ref, {}) if frag.attributes.io_ref else {}
                parsed = parse_resource_uri(io.get("resource_uri", "pass://noop"))
                scheme = "pass" if stype == "Pass" else ("arn" if parsed.raw_uri.startswith("arn:") else parsed.scheme)

                raw_params = raw.get("Parameters", {})
                raw_ext = raw_params.get("__fiber_extensions") if isinstance(raw_params.get("__fiber_extensions"), dict) else {}
                raw_proj = raw_ext.get("Projection") if isinstance(raw_ext.get("Projection"), dict) else {}
                ext_keys = tuple(k.removeprefix("$.") for k in raw_proj.get("extract_keys", ()))

                direct = [e.target for e in frag.relations if e.edge_type == EdgeMode.DIRECT and e.target != END_TOKEN]
                fb = [e.target for e in frag.relations if e.edge_type == EdgeMode.FALLBACK]
                if not direct:
                    needs_ok = True
                if not fb:
                    needs_fail = True

                table[name] = CompiledTask(
                    name=name,
                    scheme=scheme,
                    target=parsed.target,
                    params=raw_params,
                    extract_keys=ext_keys,
                    next_state=direct[0] if direct else "success_end",
                    catch_state=fb[0] if fb else None,
                )

        if needs_ok and "success_end" not in table:
            table["success_end"] = CompiledTerminal(name="success_end", succeeded=True)
        if needs_fail and "fail_end" not in table:
            table["fail_end"] = CompiledTerminal(name="fail_end", succeeded=False)

        return sig.entry_point, table

    @classmethod
    def from_nodes(
        cls,
        start_at: str,
        nodes: dict[str, CompiledNode],
        dispatcher: DispatcherFn,
        *,
        default_context: dict[str, Any] | None = None,
    ) -> FlowEngine:
        fsm_states = [State(name, final=isinstance(node, CompiledTerminal)) for name, node in nodes.items()]
        engine = cls(states=fsm_states, initial=start_at, send_event=False)
        engine._default_context = dict(default_context or {})
        engine.bind_nodes(nodes, dispatcher)
        return engine

    @classmethod
    def from_asl(
        cls,
        asl: dict[str, Any],
        dispatcher: DispatcherFn,
        *,
        validate: bool = True,
        default_context: dict[str, Any] | None = None,
    ) -> FlowEngine:
        start_at, table = cls.compile_asl_nodes(asl, validate=validate)
        return cls.from_nodes(start_at, table, dispatcher, default_context=default_context)

    @classmethod
    def from_contract(
        cls,
        flow_name: str,
        dispatcher: DispatcherFn,
        registry: FlowRegistry | None = None,
    ) -> FlowEngine:
        reg = registry or load_sample_registry()
        asl = FlowDSL.compile_to_asl(flow_name, reg)
        flow_defaults = reg.flows[flow_name].in_defaults if flow_name in reg.flows else {}
        return cls.from_asl(asl, dispatcher=dispatcher, validate=True, default_context=flow_defaults)

    def bind_nodes(self, nodes: dict[str, CompiledNode], dispatcher: DispatcherFn) -> None:
        self._compiled_nodes = dict(nodes)
        self._dispatcher = dispatcher

        for name, node in self._compiled_nodes.items():
            if name not in self.states:
                self.add_states(State(name, final=isinstance(node, CompiledTerminal)))

            if isinstance(node, CompiledTask):
                self.add_transition("step", name, node.next_state, before=self._make_task_runner(node))
                self.add_transition("fail", name, node.catch_state or "fail_end")
            elif isinstance(node, CompiledChoice):
                self.add_asl_choice("step", name, node.rules, node.default_state, lambda: self.context)

    def _make_task_runner(self, task: CompiledTask) -> Callable[[], None]:
        def _run() -> None:
            if self._dispatcher is None:
                raise FlowExecutionError(f"No dispatcher configured for task '{task.name}'.")

            bound_params, dyn_extract, each_spec = bind_parameters(task.params, self.context)

            if each_spec is not None:
                each_item, each_over = each_spec
                items = self.context.get(each_over)
                if not isinstance(items, list):
                    raise FlowExecutionError(f"Task '{task.name}' expected list at '$.{each_over}', got {type(items).__name__}.")
                collected: dict[str, list[Any]] = {}
                extract_keys = task.extract_keys or dyn_extract
                for elem in items:
                    iter_params, _, _ = bind_parameters(task.params, {**self.context, each_item: elem})
                    out = self._dispatcher(task.scheme, task.target, iter_params)
                    if isinstance(out, dict):
                        for k in extract_keys or tuple(out.keys()):
                            clean = k.removeprefix("$.")
                            if clean in out:
                                collected.setdefault(clean, []).append(out[clean])
                self.context.update(collected)
                return

            out = self._dispatcher(task.scheme, task.target, bound_params)
            apply_projection(self.context, out, task.extract_keys or dyn_extract)

        return _run

    def invoke(self, initial_context: dict[str, Any] | None = None) -> dict[str, Any]:
        self.context = {**self._default_context, **(initial_context or {})}
        self.last_error = None
        if self.initial is None:
            raise FlowExecutionError("FlowEngine has no initial state configured.")
        self.set_state(self.initial)

        while not self.get_state(self.state).final:
            try:
                if not self.dispatch("step"):
                    raise FlowExecutionError(f"Deadlock at state '{self.state}': no choice rule matched.")
            except BaseException as exc:
                self.last_error = exc
                self.context["__error__"] = str(exc)
                if not self.can_trigger("fail"):
                    raise
                self.dispatch("fail")

        term = self._compiled_nodes.get(self.state)
        if isinstance(term, CompiledTerminal) and not term.succeeded:
            raise FlowExecutionError(f"Workflow terminated at Fail state '{self.state}'.") from self.last_error
        return dict(self.context)

    # =====================================================================
    # 2. Core FSM Mechanics
    # =====================================================================

    @property
    def initial(self) -> str | None:
        return self._initial

    @initial.setter
    def initial(self, value: str | State | Enum) -> None:
        name = value.name if isinstance(value, (State, Enum)) else value
        if name not in self.states:
            self.add_states(value if isinstance(value, State) else name)
        self._initial = name
        if not self.state:
            self.state = name

    def get_state(self, state: str | Enum) -> State:
        name = state.name if isinstance(state, Enum) else state
        if name not in self.states:
            raise ValueError(f"State '{name}' is not a registered state.")
        return self.states[name]

    def set_state(self, state: str | State | Enum, _model: Any = None) -> None:
        self.state = state.name if isinstance(state, (State, Enum)) else self.get_state(state).name

    def add_states(self, states: Any, **kwargs: Any) -> None:
        for s in listify(states):
            if isinstance(s, State):
                state_obj = s
            elif isinstance(s, dict):
                state_obj = State(**s)
            else:
                state_obj = State(s, **kwargs)
            self.states[state_obj.name] = state_obj

    def add_transition(
        self,
        trigger: str,
        source: str | list[str],
        dest: str | None,
        conditions: Any = None,
        unless: Any = None,
        before: Any = None,
        after: Any = None,
        prepare: Any = None,
    ) -> None:
        if trigger not in self.events:
            self.events[trigger] = Event(trigger, self)
        sources = list(self.states) if source == "*" else [s.name if hasattr(s, "name") else s for s in listify(source)]
        for src in sources:
            target = src if dest == "=" else (dest.name if hasattr(dest, "name") else dest)
            self.events[trigger].add_transition(Transition(src, target, conditions, unless, before, after, prepare))

    def add_transitions(self, transitions: Any) -> None:
        for t in listify(transitions):
            self.add_transition(*t) if isinstance(t, (list, tuple)) else self.add_transition(**t)

    def add_asl_choice(
        self,
        trigger: str,
        source: str,
        rules: tuple[tuple[dict[str, Any] | str, str], ...] | list[tuple[dict[str, Any] | str, str]],
        default_dest: str | None,
        context_getter: Callable[[], dict[str, Any]],
    ) -> None:
        for rule_spec, dst in rules:
            self.add_transition(trigger, source, dst, conditions=lambda *_a, _r=rule_spec, **_kw: eval_choice_rule(_r, context_getter()))
        if default_dest is not None:
            self.add_transition(trigger, source, default_dest)

    def can_trigger(self, trigger: str, *args: Any, **kwargs: Any) -> bool:
        ev = self.events.get(trigger)
        if ev is None or self.state not in ev.transitions:
            return False
        event_data = EventData(self.get_state(self.state), ev, self, self, args, kwargs)
        return any(all(c.check(event_data) for c in t.conditions) for t in ev.transitions[self.state])

    def dispatch(self, trigger: str, *args: Any, **kwargs: Any) -> bool:
        if trigger not in self.events:
            if self.ignore_invalid_triggers:
                return False
            raise AttributeError(f"Unknown event trigger '{trigger}'.")
        return self.events[trigger].trigger(self, *args, **kwargs)

    @staticmethod
    def resolve_callable(func: Any, event_data: EventData) -> Callable[..., Any]:
        return getattr(event_data.model, func) if isinstance(func, str) else func

    def callbacks(self, funcs: Any, event_data: EventData) -> None:
        for fn in funcs:
            resolved = self.resolve_callable(fn, event_data)
            resolved(event_data) if self.send_event else resolved(*event_data.args, **event_data.kwargs)