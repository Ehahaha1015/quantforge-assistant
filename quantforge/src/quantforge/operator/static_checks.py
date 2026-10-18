"""Finite deterministic source guards for OPERATOR-001.

These checks are intentionally small and syntactic.  They implement the
reviewed Tripwires A/B/C and the frozen EXECSTATE private-writer topology
without attempting general taint analysis or whole-program reasoning.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .canonical import normalize_sql  # noqa: F401  (kept for contract parity)

_EVALUATOR = "quantforge.frontier.evaluate.evaluate_frontier_eligibility"
_EVIDENCE = "quantforge.frontier.models.OperatorExecutionEnableEvidence"
_HELPER = "quantforge.operator.store.operator_evidence_for_frontier"
_OPERATOR_STORE = "quantforge.operator.store.SQLiteOperatorControlStore"
_EXECSTATE_STORE = "quantforge.execstate.store.SQLiteExecutionStateStore"
_DISPATCH = "quantforge.execstate.store.SQLiteExecutionStateStore.mark_dispatching"
_PRIVATE_WRITERS = {
    "_update_submission_state",
    "_insert_receipt",
    "_write_audit",
}
_DYNAMIC_LOOKUP_NAMES = {
    "getattr",
    "locals",
    "globals",
    "vars",
    "__import__",
}
_DYNAMIC_LOOKUP_MODULES = {
    "importlib",
    "functools",
}
_SAFETY_NAMES = {
    "evaluate_frontier_eligibility",
    "operator_evidence_for_frontier",
    "OperatorExecutionEnableEvidence",
    "SQLiteOperatorControlStore",
    "SQLiteExecutionStateStore",
    "mark_dispatching",
    *_PRIVATE_WRITERS,
}
_CONSTRUCTION_NAMES = {
    "OperatorExecutionEnableEvidence",
    "SQLiteOperatorControlStore",
}
_Function = ast.FunctionDef | ast.AsyncFunctionDef

_KNOWN_REEXPORTS = {
    ("quantforge.frontier", "OperatorExecutionEnableEvidence"): _EVIDENCE,
    ("quantforge.frontier", "evaluate_frontier_eligibility"): _EVALUATOR,
    (
        "quantforge.frontier",
        "FrontierEligibilityDecision",
    ): "quantforge.frontier.models.FrontierEligibilityDecision",
    ("quantforge.operator", "operator_evidence_for_frontier"): _HELPER,
    ("quantforge.operator", "SQLiteOperatorControlStore"): _OPERATOR_STORE,
    ("quantforge.execstate", "SQLiteExecutionStateStore"): _EXECSTATE_STORE,
}

_APPROVED_EXECSTATE_EDGES: frozenset[tuple[str, str, int]] = frozenset(
    {
        ("_update_submission_state", "SQLiteExecutionStateStore._recover", 3170),
        ("_update_submission_state", "SQLiteExecutionStateStore._recover", 3210),
        (
            "_update_submission_state",
            "SQLiteExecutionStateStore.mark_dispatching",
            3931,
        ),
        (
            "_update_submission_state",
            "SQLiteExecutionStateStore.persist_transport_outcome",
            4035,
        ),
        (
            "_update_submission_state",
            "SQLiteExecutionStateStore.persist_pre_dispatch_no_effect",
            4178,
        ),
        (
            "_update_submission_state",
            "SQLiteExecutionStateStore.apply_reconciliation",
            4309,
        ),
        ("_insert_receipt", "SQLiteExecutionStateStore._recover", 3162),
        ("_insert_receipt", "SQLiteExecutionStateStore._recover", 3202),
        ("_insert_receipt", "SQLiteExecutionStateStore.prepare", 3480),
        ("_insert_receipt", "SQLiteExecutionStateStore.mark_dispatching", 3923),
        (
            "_insert_receipt",
            "SQLiteExecutionStateStore.persist_transport_outcome",
            4027,
        ),
        (
            "_insert_receipt",
            "SQLiteExecutionStateStore.persist_pre_dispatch_no_effect",
            4170,
        ),
        (
            "_insert_receipt",
            "SQLiteExecutionStateStore.apply_reconciliation",
            4301,
        ),
        ("_write_audit", "SQLiteExecutionStateStore._recover", 3178),
        ("_write_audit", "SQLiteExecutionStateStore._recover", 3190),
        ("_write_audit", "SQLiteExecutionStateStore._recover", 3218),
        ("_write_audit", "SQLiteExecutionStateStore._recover", 3261),
        ("_write_audit", "SQLiteExecutionStateStore.prepare", 3416),
        ("_write_audit", "SQLiteExecutionStateStore.prepare", 3495),
        ("_write_audit", "SQLiteExecutionStateStore.claim", 3636),
        ("_write_audit", "SQLiteExecutionStateStore.claim", 3656),
        ("_write_audit", "SQLiteExecutionStateStore.claim", 3701),
        ("_write_audit", "SQLiteExecutionStateStore.claim", 3759),
        ("_write_audit", "SQLiteExecutionStateStore.mark_dispatching", 3828),
        ("_write_audit", "SQLiteExecutionStateStore.mark_dispatching", 3939),
        (
            "_write_audit",
            "SQLiteExecutionStateStore.persist_transport_outcome",
            4043,
        ),
        (
            "_write_audit",
            "SQLiteExecutionStateStore.persist_transport_outcome",
            4071,
        ),
        (
            "_write_audit",
            "SQLiteExecutionStateStore.persist_pre_dispatch_no_effect",
            4186,
        ),
        (
            "_write_audit",
            "SQLiteExecutionStateStore.persist_pre_dispatch_no_effect",
            4210,
        ),
        (
            "_write_audit",
            "SQLiteExecutionStateStore.apply_reconciliation",
            4317,
        ),
        (
            "_write_audit",
            "SQLiteExecutionStateStore.apply_reconciliation",
            4342,
        ),
    }
)


@dataclass(frozen=True)
class Violation:
    code: str
    path: str
    line: int
    message: str


@dataclass(frozen=True)
class TopologyResult:
    actual: frozenset[tuple[str, str, int]]
    approved: frozenset[tuple[str, str, int]]

    @property
    def exact(self) -> bool:
        return self.actual == self.approved

    @property
    def missing(self) -> frozenset[tuple[str, str, int]]:
        return self.approved - self.actual

    @property
    def extra(self) -> frozenset[tuple[str, str, int]]:
        return self.actual - self.approved


class _ScopeMap(ast.NodeVisitor):
    """Build a node -> enclosing qualname map and binding maps."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.qualnames: dict[ast.AST, str] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.qualnames[node] = ".".join(self.stack + [node.name])
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _visit_function(self, node: _Function) -> None:
        self.qualnames[node] = ".".join(self.stack + [node.name])
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.qualnames[node] = ".".join(self.stack)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self.qualnames[node] = ".".join(self.stack)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.qualnames[node] = ".".join(self.stack)
        self.generic_visit(node)


def _canonical_imports(tree: ast.Module) -> dict[str, str]:
    names: dict[str, str] = {}
    modules: dict[str, str] = {}
    module_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                if alias.asname:
                    modules[alias.name] = local
                else:
                    modules[alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if any(alias.name == "*" for alias in node.names):
                module_aliases[node.module] = "*"
                continue
            for alias in node.names:
                local = alias.asname or alias.name
                canonical = _KNOWN_REEXPORTS.get((node.module, alias.name))
                if canonical is not None:
                    names[local] = canonical
                else:
                    names[local] = f"{node.module}.{alias.name}"
    for module, local in modules.items():
        names[local] = module
    for module, flag in module_aliases.items():
        if flag == "*":
            for symbol in _SAFETY_NAMES:
                names.setdefault(symbol, f"{module}.{symbol}*")
    return names


def _module_definitions(tree: ast.Module) -> dict[str, str]:
    definitions: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions[node.name] = node.name
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if isinstance(node, ast.Assign):
                targets: Iterable[ast.AST] = node.targets
            else:
                targets = (node.target,)
            if not isinstance(node.value, ast.Call):
                continue
            if not (
                isinstance(node.value.func, ast.Name)
                and node.value.func.id == "SQLiteExecutionStateStore"
            ) and not (
                isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "SQLiteExecutionStateStore"
            ):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    definitions[target.id] = target.id
    return definitions


def _module_name(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    marker = "/src/"
    if marker not in f"/{normalized}":
        return None
    tail = f"/{normalized}".split(marker, 1)[1]
    if not tail.endswith(".py"):
        return None
    return tail[:-3].replace("/", ".")


def _symbol_identity(
    node: ast.AST,
    *,
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
) -> str | None:
    if isinstance(node, ast.Name):
        if node.id in definitions and module is not None:
            return f"{module}.{node.id}"
        return imports.get(node.id)
    if isinstance(node, ast.Attribute):
        base = node.value
        if isinstance(base, ast.Name):
            local = definitions.get(base.id)
            if local is not None and module is not None:
                return f"{module}.{local}.{node.attr}"
            module_path = imports.get(base.id)
            if module_path is not None:
                return f"{module_path}.{node.attr}"
            return None
        base_identity = _symbol_identity(
            base,
            imports=imports,
            definitions=definitions,
            module=module,
        )
        if base_identity is None:
            return None
        return f"{base_identity}.{node.attr}"
    return None


def _enclosing_functions(tree: ast.Module) -> dict[ast.AST, _Function]:
    parent: dict[ast.AST, _Function] = {}

    def walk(node: ast.AST, current: _Function | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if current is not None:
                parent[node] = current
            current = node
        elif current is not None:
            parent[node] = current
        for child in ast.iter_child_nodes(node):
            walk(child, current)

    walk(tree, None)
    return parent


def _function_scopes(tree: ast.Module) -> dict[ast.AST, _Function]:
    scopes: dict[ast.AST, _Function] = {}

    def walk(node: ast.AST, current: _Function | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes[node] = node
            current = node
        for child in ast.iter_child_nodes(node):
            walk(child, current)

    walk(tree, None)
    return scopes


def _function_for(
    node: ast.AST,
    enclosing: Mapping[ast.AST, _Function],
) -> _Function | None:
    return enclosing.get(node)


def _statement_index(function: _Function, node: ast.AST) -> int | None:
    for index, statement in enumerate(function.body):
        if statement is node or any(child is node for child in ast.walk(statement)):
            return index
    return None


def _is_dynamic_lookup_name(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in _DYNAMIC_LOOKUP_NAMES
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id in _DYNAMIC_LOOKUP_MODULES
    return False


def _is_ast_descendant(node: ast.AST, root: ast.AST) -> bool:
    return any(child is node for child in ast.walk(root))


def _callee_identity(
    node: ast.AST,
    imports: Mapping[str, str],
) -> str | None:
    if isinstance(node, ast.Name):
        return imports.get(node.id)
    if isinstance(node, ast.Attribute):
        base = node.value
        if isinstance(base, ast.Name):
            module = imports.get(base.id)
            if module is None:
                return None
            return f"{module}.{node.attr}"
        return None
    return None


def _is_store_construction(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "SQLiteExecutionStateStore") or (
        isinstance(func, ast.Attribute) and func.attr == "SQLiteExecutionStateStore"
    )


def _store_receiver_names(
    body: Iterable[ast.stmt],
) -> set[str]:
    names: set[str] = set()
    for statement in body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        if statement.value is None or not _is_store_construction(statement.value):
            continue
        if isinstance(statement, ast.Assign):
            targets: Iterable[ast.AST] = statement.targets
        else:
            targets = (statement.target,)
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
        if isinstance(statement, ast.AnnAssign):
            annotation = statement.annotation
            annotated = (
                isinstance(annotation, ast.Name)
                and annotation.id == "SQLiteExecutionStateStore"
            ) or (
                isinstance(annotation, ast.Attribute)
                and annotation.attr == "SQLiteExecutionStateStore"
            )
            if annotated and isinstance(statement.target, ast.Name):
                names.add(statement.target.id)
    return names


def _is_mark_dispatching_call(
    node: ast.Call,
    receiver_names: set[str],
) -> bool:
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "mark_dispatching":
        return False
    receiver = node.func.value
    if not isinstance(receiver, ast.Name):
        return False
    return receiver.id in receiver_names


def _receiver_is_statically_known(
    *,
    function: _Function | None,
    receiver: ast.AST,
    scopes: _ScopeMap,
    receiver_names: set[str],
    module_receivers: set[str],
    imports: Mapping[str, str],
) -> bool:
    if isinstance(receiver, ast.Call):
        return _is_store_construction(receiver)
    if not isinstance(receiver, ast.Name):
        return False
    if receiver.id in receiver_names or receiver.id in module_receivers:
        return True
    if function is not None and _is_local_store_binding(function, receiver.id):
        return True
    if function is not None:
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        ):
            if argument.arg != receiver.id or argument.annotation is None:
                continue
            if (
                _callee_identity(argument.annotation, imports)
                == "quantforge.execstate.store.SQLiteExecutionStateStore"
            ):
                return True
        if function.args.args:
            first = function.args.args[0]
            if first.arg == receiver.id and first.annotation is None:
                # The frozen EXECSTATE instance-method receiver: any private
                # writer reached through it is still checked against the
                # contract's approved call-edge allow-list.
                scope_qualname = scopes.qualnames.get(function, "")
                return scope_qualname.startswith("SQLiteExecutionStateStore.")
    return False


def _is_dispatch_candidate(
    node: ast.Call,
    aliases: Mapping[str, str],
) -> bool:
    names = {"mark_dispatching", *_PRIVATE_WRITERS}
    func = node.func
    if isinstance(func, ast.Subscript):
        if _mentions_safety_symbol(func.value, names):
            return True
        return any(
            isinstance(child, ast.Name)
            and (
                aliases.get(child.id) in names
                or (
                    aliases.get(child.id) is not None
                    and aliases[child.id].rsplit(".", 1)[-1] in names
                )
            )
            for child in ast.walk(func.value)
        )
    if isinstance(func, ast.IfExp):
        if _mentions_safety_symbol(func, names):
            return True
        return any(
            isinstance(child, ast.Name)
            and (
                aliases.get(child.id) in names
                or (
                    aliases.get(child.id) is not None
                    and aliases[child.id].rsplit(".", 1)[-1] in names
                )
            )
            for child in ast.walk(func)
        )
    if isinstance(func, ast.Name):
        return aliases.get(func.id) in names
    return False


def _private_writer_violation(
    node: ast.Call,
    *,
    path: str,
    function: _Function | None,
    scopes: _ScopeMap,
) -> Violation | None:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _PRIVATE_WRITERS:
        return None
    qualname = (
        scopes.qualnames.get(function)
        if function is not None
        else None
    ) or getattr(function, "name", "")
    allowed = {
        caller
        for (writer, caller, _line) in _APPROVED_EXECSTATE_EDGES
        if writer == func.attr
    }
    if qualname in allowed:
        return None
    return Violation(
        "BOUNDARY-PRIVATE-WRITER",
        path,
        node.lineno,
        "external private EXECSTATE writer call",
    )


def _bound_value(function: _Function, name: str) -> ast.AST | None:
    for statement in function.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(statement, ast.Assign):
            targets: Iterable[ast.AST] = statement.targets
        else:
            targets = (statement.target,)
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return statement.value
    return None


def _is_dynamic_lookup_call_node(node: ast.AST, imports: Mapping[str, str]) -> bool:
    if isinstance(node, ast.Call):
        if _uses_dynamic_lookup(node):
            return True
        if _resolves_to_partial(node, imports):
            return True
    if isinstance(node, ast.Attribute):
        return node.attr == "__dict__"
    return False


def _is_forbidden_callee_binding(
    function: _Function | None,
    node: ast.Call,
    names: set[str],
    imports: Mapping[str, str],
) -> bool:
    func = node.func
    if not isinstance(func, ast.Name):
        return False
    if function is None:
        return False
    value = _bound_value(function, func.id)
    if value is None:
        if imports.get(func.id) is not None:
            return False
        return False
    if isinstance(value, ast.Call) and _is_dynamic_lookup_call_node(value, imports):
        return True
    for child in ast.walk(value):
        if isinstance(child, ast.Name) and child.id in names:
            return True
        if isinstance(child, ast.Attribute) and child.attr in names:
            return True
    return False


def _assignment_target_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for element in target.elts:
            names.extend(_assignment_target_names(element))
        return names
    return []


_UNRESOLVED = "__unresolved_safety_candidate"
_CLASS_AMBIGUOUS = "__class_ambiguity__"
_CONTAINER_PREFIX = "__container__:"
_OWNER_SLOT_PREFIX = "__ownerslot__:"
_CONTAINER_MUTATORS = frozenset(
    {"append", "appendleft", "extend", "add", "update", "insert"}
)


def _dotted_name(node: ast.AST) -> str | None:
    """Dotted attribute path for ``a.b.c`` style expressions, else ``None``."""

    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _constant_slot_value(value: object) -> str | int | None:
    """Canonical Python mapping-key value for a supported constant literal.

    ``bool`` is normalized to its integer mapping-key equivalence so ``True``
    and ``1`` (and ``False`` and ``0``) share one slot identity.
    """

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (str, int)):
        return value
    return None


def _subscript_slot_key(node: ast.AST) -> str | None:
    """Bounded ``container[key]`` slot key for constant literal keys."""

    if not isinstance(node, ast.Subscript):
        return None
    value = node.value
    base: str | None
    if isinstance(value, ast.Name):
        base = value.id
    elif isinstance(value, ast.Attribute):
        base = _dotted_name(value)
    else:
        base = None
    if base is None:
        return None
    key = node.slice
    if isinstance(key, ast.Constant):
        canonical = _constant_slot_value(key.value)
        if canonical is not None:
            return f"{base}[{canonical!r}]"
    return None


def _function_key(function: _Function) -> str:
    """Stable in-module identity of one function lexical owner."""

    return f"{function.lineno}:{function.col_offset}:{function.name}"


def _owner_slot_key(owner_kind: str, owner_key: str, slot: str) -> str:
    """Owner-qualified constant-key slot identity."""

    return f"{_OWNER_SLOT_PREFIX}{owner_kind}|{owner_key}|{slot}"


def _container_identity(owner_kind: str, owner_key: str, path: str) -> str:
    """Owner-qualified identity of a known authority-bearing container."""

    return f"{_CONTAINER_PREFIX}{owner_kind}|{owner_key}|{path}"


def _container_identity_parts(
    identity: str,
) -> tuple[str, str, str] | None:
    """Split a container identity into owner kind, owner key and path."""

    if not identity.startswith(_CONTAINER_PREFIX):
        return None
    payload = identity[len(_CONTAINER_PREFIX) :]
    owner_kind, separator, rest = payload.partition("|")
    if not separator or not owner_kind:
        return None
    owner_key, separator, path = rest.partition("|")
    if not separator or not owner_key or not path:
        return None
    return owner_kind, owner_key, path


def _container_iteration_identity(
    identity: str,
    aliases: Mapping[str, str],
    *,
    names: set[str],
    resolver: _ModuleResolver | None,
) -> str | None:
    """Resolve a container identity used by an iteration/context binding.

    A container with a known authority-bearing slot fails closed; a container
    with no authority slot stays benign.
    """

    parts = _container_identity_parts(identity)
    if parts is None:
        return identity
    owner_kind, owner_key, path = parts
    prefix = _owner_slot_key(owner_kind, owner_key, f"{path}[")
    mappings: list[Mapping[str, str]] = [aliases]
    if resolver is not None:
        mappings.append(resolver._owner_aliases(owner_kind, owner_key))
    for mapping in mappings:
        for key, value in mapping.items():
            if not key.startswith(prefix):
                continue
            if _resolved_boundary_name(value, names):
                return _UNRESOLVED
    return None


def _is_unresolved_identity(identity: str | None) -> bool:
    """Whether an identity is a fail-closed unresolved marker."""

    return identity in {_UNRESOLVED, _CLASS_AMBIGUOUS}


def _subscript_container_path(target: ast.AST) -> str | None:
    """Simple class-body container name receiving a constant-key slot."""

    if not isinstance(target, ast.Subscript):
        return None
    if not isinstance(target.value, ast.Name):
        return None
    return target.value.id


def _merge_alias(current: str | None, new: str) -> str:
    if current is None or current == new:
        return new
    return _UNRESOLVED


def _record_alias(
    aliases: dict[str, str],
    names: set[str],
    name: str,
    value: ast.AST,
    *,
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
    resolver: _ModuleResolver | None = None,
    function: _Function | None = None,
) -> None:
    identity = _alias_identity_from_value(
        value,
        names=names,
        imports=imports,
        definitions=definitions,
        module=module,
        known_aliases=aliases,
        resolver=resolver,
        function=function,
    )
    if identity is not None:
        aliases[name] = identity


def _record_composite_target(
    aliases: dict[str, str],
    names: set[str],
    target: ast.AST,
    value: ast.AST,
    *,
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
    resolver: _ModuleResolver | None,
    function: _Function | None,
) -> str | None:
    """Record attribute/subscript slots that receive a safety-relevant value."""

    identity = _alias_identity_from_value(
        value,
        names=names,
        imports=imports,
        definitions=definitions,
        module=module,
        known_aliases=aliases,
        resolver=resolver,
        function=function,
    )
    if identity is None:
        return None
    dotted = _dotted_name(target)
    if dotted is not None and isinstance(target, ast.Attribute):
        aliases[dotted] = _merge_alias(aliases.get(dotted), identity)
        return identity
    slot = _subscript_slot_key(target)
    if slot is not None:
        aliases[slot] = _merge_alias(aliases.get(slot), identity)
        return identity
    root = _node_root_name(target)
    if root is not None:
        aliases[root] = _merge_alias(aliases.get(root), identity)
        return identity
    return None


def _record_container_mutation(
    aliases: dict[str, str],
    names: set[str],
    call: ast.Call,
    *,
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
    resolver: _ModuleResolver | None,
    function: _Function | None,
) -> None:
    """Record ``container.append(authority_alias)`` style mutations."""

    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in _CONTAINER_MUTATORS:
        return
    root = _node_root_name(func.value)
    if root is None:
        return
    for argument in (
        *call.args,
        *(keyword.value for keyword in call.keywords),
    ):
        identity = _alias_identity_from_value(
            argument,
            names=names,
            imports=imports,
            definitions=definitions,
            module=module,
            known_aliases=aliases,
            resolver=resolver,
            function=function,
        )
        if identity is not None:
            aliases[root] = _merge_alias(aliases.get(root), identity)


def _record_dict_literal_slots(
    aliases: dict[str, str],
    target: ast.AST,
    value: ast.AST,
    *,
    names: set[str],
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
    resolver: _ModuleResolver | None,
    function: _Function | None,
    container_owner: tuple[str, str] | None,
) -> None:
    """Record constant-key dict-literal slots under their owner identity.

    The whole-dict identity is intentionally replaced by the owner-qualified
    container identity only when every key is a supported constant, because
    each non-trivial element identity is then preserved in its own slot.
    """

    if container_owner is None:
        return
    if not isinstance(target, ast.Name) or not isinstance(value, ast.Dict):
        return
    if not value.keys:
        return
    if any(
        not isinstance(key, ast.Constant)
        or _constant_slot_value(key.value) is None
        for key in value.keys
    ):
        return
    owner_kind, owner_key = container_owner
    for key, element in zip(value.keys, value.values):
        if not isinstance(key, ast.Constant):
            continue
        canonical = _constant_slot_value(key.value)
        if canonical is None:
            continue
        identity = _alias_identity_from_value(
            element,
            names=names,
            imports=imports,
            definitions=definitions,
            module=module,
            known_aliases=aliases,
            resolver=resolver,
            function=function,
        )
        if identity is None:
            continue
        slot = f"{target.id}[{canonical!r}]"
        aliases[slot] = _merge_alias(aliases.get(slot), identity)
        qualified = _owner_slot_key(owner_kind, owner_key, slot)
        aliases[qualified] = _merge_alias(aliases.get(qualified), identity)
    aliases[target.id] = _container_identity(
        owner_kind, owner_key, target.id
    )


def _iter_scope_statements(body: Iterable[ast.stmt]) -> Iterable[ast.stmt]:
    """Statements of one lexical scope, including finite compound bodies.

    Nested function, async-function and class bodies are new lexical owners:
    they are deliberately not descended into because each of them keeps its
    own bounded scope handling.  Every ordinary compound statement body is
    walked in source order so a reviewed mutation cannot hide one level down.
    """

    for statement in body:
        yield statement
        if isinstance(
            statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        for field in ("body", "orelse", "finalbody"):
            nested = getattr(statement, field, None)
            if isinstance(nested, list):
                yield from _iter_scope_statements(nested)
        handlers = getattr(statement, "handlers", None)
        if isinstance(handlers, list):
            for handler in handlers:
                yield from _iter_scope_statements(handler.body)
        cases = getattr(statement, "cases", None)
        if isinstance(cases, list):
            for case in cases:
                yield from _iter_scope_statements(case.body)


def _iter_named_exprs(node: ast.AST) -> Iterable[ast.NamedExpr]:
    """NamedExpr bindings of one lexical scope, skipping nested owners."""

    for child in ast.iter_child_nodes(node):
        if isinstance(
            child,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
        ):
            continue
        if isinstance(child, ast.NamedExpr):
            yield child
        yield from _iter_named_exprs(child)


def _scope_binding_names(body: Iterable[ast.stmt]) -> frozenset[str]:
    """Every simple name bound in one lexical scope, including compound bodies.

    Used only to give a nearer explicit binding deterministic precedence over a
    seeded outer alias environment.  Bindings inside nested function, class or
    lambda owners are not part of this scope.
    """

    names: set[str] = set()
    for statement in _iter_scope_statements(body):
        if isinstance(
            statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            names.add(statement.name)
            continue
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
            if value is None:
                continue
            targets: Iterable[ast.AST]
            if isinstance(statement, ast.Assign):
                targets = statement.targets
            else:
                targets = (statement.target,)
            for target in targets:
                names.update(_assignment_target_names(target))
        if isinstance(statement, (ast.For, ast.AsyncFor)):
            names.update(_assignment_target_names(statement.target))
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            for item in statement.items:
                if item.optional_vars is not None:
                    names.update(_assignment_target_names(item.optional_vars))
        for named in _iter_named_exprs(statement):
            names.update(_assignment_target_names(named.target))
    return frozenset(names)


def _collect_alias_sources(
    body: Iterable[ast.stmt],
    *,
    names: set[str],
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
    include_named_expr: bool,
    resolver: _ModuleResolver | None = None,
    function: _Function | None = None,
    seed: Mapping[str, str] | None = None,
    container_owner: tuple[str, str] | None = None,
    local_class_definitions: Mapping[str, str] | None = None,
) -> dict[str, str]:
    aliases: dict[str, str] = dict(seed) if seed is not None else {}
    statements = list(_iter_scope_statements(body))
    if seed:
        # A nearer explicit binding wins over the seeded outer environment.
        for shadowed in _scope_binding_names(body):
            for key in [
                key
                for key in aliases
                if key == shadowed
                or key.startswith((f"{shadowed}.", f"{shadowed}["))
            ]:
                del aliases[key]
    if local_class_definitions:
        # A ClassDef bound in this owner shadows any outer binding and is the
        # finite class-reference identity for nested attribute chains.
        aliases.update(local_class_definitions)
    # Deterministic bounded fixpoint: alias chains are finite (one pass adds
    # at most one entry per candidate name), so iterate until no change.
    for _ in range(len(statements) + 2):
        before = dict(aliases)
        for statement in statements:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                value = statement.value
                if value is None:
                    continue
                targets: Iterable[ast.AST]
                if isinstance(statement, ast.Assign):
                    targets = statement.targets
                else:
                    targets = (statement.target,)
                for target in targets:
                    for name in _assignment_target_names(target):
                        _record_alias(
                            aliases,
                            names,
                            name,
                            value,
                            imports=imports,
                            definitions=definitions,
                            module=module,
                            resolver=resolver,
                            function=function,
                        )
                    recorded = _record_composite_target(
                        aliases,
                        names,
                        target,
                        value,
                        imports=imports,
                        definitions=definitions,
                        module=module,
                        resolver=resolver,
                        function=function,
                    )
                    if recorded is not None and container_owner is not None:
                        container_path = _subscript_container_path(target)
                        slot = _subscript_slot_key(target)
                        if container_path is not None and slot is not None:
                            owner_kind, owner_key = container_owner
                            qualified = _owner_slot_key(
                                owner_kind, owner_key, slot
                            )
                            aliases[qualified] = _merge_alias(
                                aliases.get(qualified), recorded
                            )
                            aliases[container_path] = _merge_alias(
                                aliases.get(container_path),
                                _container_identity(
                                    owner_kind,
                                    owner_key,
                                    container_path,
                                ),
                            )
                    _record_dict_literal_slots(
                        aliases,
                        target,
                        value,
                        names=names,
                        imports=imports,
                        definitions=definitions,
                        module=module,
                        resolver=resolver,
                        function=function,
                        container_owner=container_owner,
                    )
            if isinstance(statement, (ast.For, ast.AsyncFor)):
                iterated = _alias_identity_from_value(
                    statement.iter,
                    names=names,
                    imports=imports,
                    definitions=definitions,
                    module=module,
                    known_aliases=aliases,
                    resolver=resolver,
                    function=function,
                )
                if iterated is not None:
                    binding = _container_iteration_identity(
                        iterated,
                        aliases,
                        names=names,
                        resolver=resolver,
                    )
                    if binding is not None:
                        for name in _assignment_target_names(
                            statement.target
                        ):
                            aliases[name] = _merge_alias(
                                aliases.get(name), binding
                            )
            if isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    if item.optional_vars is None:
                        continue
                    context = _alias_identity_from_value(
                        item.context_expr,
                        names=names,
                        imports=imports,
                        definitions=definitions,
                        module=module,
                        known_aliases=aliases,
                        resolver=resolver,
                        function=function,
                    )
                    if context is None:
                        continue
                    binding = _container_iteration_identity(
                        context,
                        aliases,
                        names=names,
                        resolver=resolver,
                    )
                    if binding is None:
                        continue
                    for name in _assignment_target_names(
                        item.optional_vars
                    ):
                        aliases[name] = _merge_alias(
                            aliases.get(name), binding
                        )
            if not isinstance(
                statement,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                for node in ast.walk(statement):
                    if not isinstance(
                        node,
                        (
                            ast.ListComp,
                            ast.SetComp,
                            ast.DictComp,
                            ast.GeneratorExp,
                        ),
                    ):
                        continue
                    carries = any(
                        _alias_identity_from_value(
                            generator.iter,
                            names=names,
                            imports=imports,
                            definitions=definitions,
                            module=module,
                            known_aliases=aliases,
                            resolver=resolver,
                            function=function,
                        )
                        is not None
                        for generator in node.generators
                    )
                    if not carries:
                        continue
                    for generator in node.generators:
                        for name in _assignment_target_names(
                            generator.target
                        ):
                            aliases[name] = _UNRESOLVED
            if isinstance(statement, ast.Expr) and isinstance(
                statement.value, ast.Call
            ):
                _record_container_mutation(
                    aliases,
                    names,
                    statement.value,
                    imports=imports,
                    definitions=definitions,
                    module=module,
                    resolver=resolver,
                    function=function,
                )
            if not include_named_expr:
                continue
            for node in ast.walk(statement):
                if not isinstance(node, ast.NamedExpr):
                    continue
                for name in _assignment_target_names(node.target):
                    _record_alias(
                        aliases,
                        names,
                        name,
                        node.value,
                        imports=imports,
                        definitions=definitions,
                        module=module,
                        resolver=resolver,
                        function=function,
                    )
        if aliases == before:
            break
    return aliases


def _scope_chain(
    function: _Function | None,
    enclosing: Mapping[ast.AST, _Function],
) -> tuple[_Function, ...]:
    chain: list[_Function] = []
    current = function
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = enclosing.get(current)
    return tuple(reversed(chain))


def _is_static_callee_value(
    value: ast.AST,
    *,
    names: set[str],
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
) -> bool:
    """Syntactically targets one of the reviewed authority symbols."""

    if any(
        isinstance(child, ast.Name) and child.id in names
        for child in ast.walk(value)
    ):
        return True
    if any(
        isinstance(child, ast.Attribute) and child.attr in names
        for child in ast.walk(value)
    ):
        return True
    for child in ast.walk(value):
        if not isinstance(child, (ast.Name, ast.Attribute)):
            continue
        identity = _symbol_identity(
            child,
            imports=imports,
            definitions=definitions,
            module=module,
        )
        if identity is not None and identity.rsplit(".", 1)[-1] in names:
            return True
    return False


def _mentions_boundary_identity(
    value: ast.AST,
    *,
    names: set[str],
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
) -> bool:
    """Any occurrence of a reviewed authority symbol in a candidate value."""

    for child in ast.walk(value):
        if isinstance(child, ast.Name) and child.id in names:
            return True
        if isinstance(child, ast.Attribute) and child.attr in names:
            return True
        if not isinstance(child, (ast.Name, ast.Attribute)):
            continue
        identity = _symbol_identity(
            child,
            imports=imports,
            definitions=definitions,
            module=module,
        )
        if identity is not None and (
            identity.rsplit(".", 1)[-1] in names
        ):
            return True
    return False


def _is_partial_construction(
    value: ast.AST,
    imports: Mapping[str, str],
) -> bool:
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    if isinstance(func, ast.Attribute) and func.attr == "partial":
        return True
    return (
        isinstance(func, ast.Name)
        and imports.get(func.id) == "functools.partial"
    )


def _alias_identity_from_value(
    value: ast.AST,
    *,
    names: set[str],
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
    known_aliases: Mapping[str, str] | None = None,
    resolver: _ModuleResolver | None = None,
    function: _Function | None = None,
) -> str | None:
    """Closed-grammar resolution of one alias-source expression."""

    if resolver is not None and isinstance(
        value,
        (
            ast.Call,
            ast.Subscript,
            ast.Attribute,
            ast.Lambda,
            ast.IfExp,
            ast.Tuple,
            ast.List,
            ast.Set,
            ast.Dict,
            ast.NamedExpr,
            ast.ListComp,
            ast.SetComp,
            ast.DictComp,
            ast.GeneratorExp,
        ),
    ):
        resolved = resolver.value_of(
            value,
            scope_aliases=known_aliases or {},
            function=function,
        )
        if resolved is not None:
            return resolved
    if isinstance(value, ast.Name) and resolver is not None:
        class_node, class_ambiguous = resolver.lookup_class(
            value.id, function=function
        )
        if class_ambiguous:
            return _CLASS_AMBIGUOUS
        if class_node is not None:
            return resolver.class_reference_key(class_node)
    if isinstance(value, (ast.Name, ast.Attribute)):
        identity = _symbol_identity(
            value,
            imports=imports,
            definitions=definitions,
            module=module,
        )
        if identity is not None:
            return identity
        if isinstance(value, ast.Name) and value.id in names:
            return value.id
        if isinstance(value, ast.Name) and known_aliases is not None:
            known = known_aliases.get(value.id)
            if known is not None:
                return known
        if isinstance(value, ast.Attribute) and value.attr in names:
            return value.attr
        if isinstance(value, ast.Attribute) and known_aliases is not None:
            known = known_aliases.get(value.attr)
            if known is not None:
                return known
        return None
    if (
        isinstance(value, ast.Call)
        and _uses_dynamic_lookup(value)
        and len(value.args) >= 2
        and isinstance(value.args[1], ast.Constant)
        and isinstance(value.args[1].value, str)
        and value.args[1].value in names
    ):
        return value.args[1].value
    if _is_partial_construction(value, imports) and _value_mentions_boundary(
        value,
        names=names,
        imports=imports,
        definitions=definitions,
        module=module,
        known_aliases=known_aliases,
    ):
        return "__unresolved_safety_candidate"
    if isinstance(value, (ast.List, ast.Tuple, ast.Set, ast.Dict)) and (
        _value_mentions_boundary(
            value,
            names=names,
            imports=imports,
            definitions=definitions,
            module=module,
            known_aliases=known_aliases,
        )
    ):
        return "__unresolved_safety_candidate"
    if isinstance(value, ast.IfExp) and _value_mentions_boundary(
        value,
        names=names,
        imports=imports,
        definitions=definitions,
        module=module,
        known_aliases=known_aliases,
    ):
        return "__unresolved_safety_candidate"
    if isinstance(value, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        elt = getattr(value, "elt", None)
        if elt is not None and _value_mentions_boundary(
            elt,
            names=names,
            imports=imports,
            definitions=definitions,
            module=module,
            known_aliases=known_aliases,
        ):
            return "__unresolved_safety_candidate"
    if isinstance(value, ast.Subscript):
        root = _node_root_name(value)
        if root is not None:
            if root in names:
                return "__unresolved_safety_candidate"
            if known_aliases is not None and root in known_aliases:
                return "__unresolved_safety_candidate"
        key = value.slice
        if (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and key.value in names
        ):
            # A reflection table keyed by a safety-relevant symbol name is an
            # unresolved candidate, not a resolvable canonical identity.
            return "__unresolved_safety_candidate"
    return None


def _value_mentions_boundary(
    value: ast.AST,
    *,
    names: set[str],
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
    known_aliases: Mapping[str, str] | None = None,
) -> bool:
    """Boundary-aware mention test used during alias propagation."""

    if _mentions_boundary_identity(
        value,
        names=names,
        imports=imports,
        definitions=definitions,
        module=module,
    ):
        return True
    if known_aliases is None:
        return False
    for child in ast.walk(value):
        if isinstance(child, ast.Name) and child.id in known_aliases:
            return True
        if (
            isinstance(child, ast.Subscript)
            and _node_root_name(child) in known_aliases
        ):
            return True
    return False


def _returned_expressions(function: _Function) -> tuple[ast.AST, ...]:
    returns: list[ast.AST] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Return) and node.value is not None:
            returns.append(node.value)
    return tuple(returns)


_RESOLVER_MAX_DEPTH = 6
_CLASS_REFERENCE_PREFIX = "__classref__:"


class _ModuleResolver:
    """Finite, bounded, deterministic same-module callable resolution.

    The resolver keeps three finite maps per module: plain alias maps for the
    module, each function and each class body, plus one resolved return
    identity per function.  Return identities are propagated by a bounded
    monotone fixpoint over the module's finite function set, and ``value_of``
    never recurses into alias collection, so termination is obvious and no
    path explosion is possible.  It is deliberately not a general evaluator.
    """

    def __init__(
        self,
        tree: ast.Module,
        *,
        names: set[str],
        imports: Mapping[str, str],
        definitions: Mapping[str, str],
        module: str | None,
    ) -> None:
        self._tree = tree
        self._names = names
        self._imports = imports
        self._definitions = definitions
        self._module = module
        self._enclosing = _enclosing_functions(tree)
        self._functions: tuple[_Function, ...] = tuple(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        self._module_owner_key = module or "module"
        self._function_nodes: dict[str, _Function] = {
            _function_key(function): function
            for function in self._functions
        }
        # Finite state is created before any collection so collection may
        # consult the resolver without recursing into alias collection.
        self._local_aliases: dict[int, dict[str, str]] = {}
        self._returns: dict[int, str] = {}
        self._class_nodes: dict[str, ast.ClassDef] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                # Later definitions shadow earlier ones, matching the finite
                # module population rule used for functions and names.
                self._class_nodes[self._class_key(node)] = node
        self._class_aliases: dict[str, dict[str, str]] = {
            name: {} for name in self._class_nodes
        }
        # The empty map is published first: collection may consult the
        # resolver while the module environment is still being built.
        self._module_aliases: dict[str, str] = {}
        self._module_aliases.update(self._scope_aliases(tree.body, function=None))
        self._refresh_class_aliases()
        self._fixpoint()

    # -- finite alias state -------------------------------------------------

    def _scope_aliases(
        self,
        body: Iterable[ast.stmt],
        *,
        function: _Function | None,
        seed: Mapping[str, str] | None = None,
        class_key: str | None = None,
        local_class_definitions: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        if class_key is not None:
            container_owner = ("class", class_key)
        elif function is not None:
            container_owner = ("function", _function_key(function))
        else:
            container_owner = ("module", self._module_owner_key)
        return _collect_alias_sources(
            body,
            names=self._names,
            imports=self._imports,
            definitions=self._definitions,
            module=self._module,
            include_named_expr=True,
            resolver=self,
            function=function,
            seed=seed,
            container_owner=container_owner,
            local_class_definitions=local_class_definitions,
        )

    def aliases_for(self, function: _Function) -> Mapping[str, str]:
        cached = self._local_aliases.get(id(function))
        if cached is None:
            cached = self._scope_aliases(
                function.body,
                function=function,
                seed=self._outer_context(function),
            )
            self._local_aliases[id(function)] = cached
        return cached

    def _outer_context(self, function: _Function | None) -> dict[str, str]:
        """Bounded outer alias environment: module plus enclosing scopes.

        Only already-collected maps are read, so seeding cannot recurse; the
        bounded fixpoint lets outer results become visible on later rounds.
        """

        context = dict(self._module_aliases)
        current = (
            self._enclosing.get(function) if function is not None else None
        )
        chain: list[_Function] = []
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            chain.append(current)
            current = self._enclosing.get(current)
        for scope in reversed(chain):
            cached = self._local_aliases.get(id(scope))
            if cached:
                context.update(cached)
        return context

    def _owner_aliases(
        self, owner_kind: str, owner_key: str
    ) -> Mapping[str, str]:
        """Already-collected alias map for one lexical owner kind."""

        if owner_kind == "module":
            return self._module_aliases
        if owner_kind == "function":
            function = self._function_nodes.get(owner_key)
            if function is None:
                return {}
            return self._local_aliases.get(id(function), {})
        if owner_kind == "class":
            return self._class_aliases.get(owner_key, {})
        return {}

    def _container_slot_value(
        self,
        identity: str,
        key: ast.AST,
        scope_aliases: Mapping[str, str],
    ) -> str | None:
        """Resolve a constant-key slot through one owner-qualified container."""

        parts = _container_identity_parts(identity)
        if parts is None:
            return None
        owner_kind, owner_key, path = parts
        if not isinstance(key, ast.Constant):
            return None
        canonical = _constant_slot_value(key.value)
        if canonical is None:
            return None
        qualified = _owner_slot_key(
            owner_kind, owner_key, f"{path}[{canonical!r}]"
        )
        value = scope_aliases.get(qualified)
        if value is not None:
            return value
        return self._owner_aliases(owner_kind, owner_key).get(qualified)

    @staticmethod
    def _class_key(node: ast.ClassDef) -> str:
        return (
            f"{_CLASS_REFERENCE_PREFIX}{node.lineno}:{node.col_offset}:"
            f"{node.name}"
        )

    def class_reference_key(self, node: ast.ClassDef) -> str:
        return self._class_key(node)

    @staticmethod
    def _scope_definitions(
        body: Iterable[ast.stmt],
    ) -> dict[str, list[ast.AST]]:
        """Definitions owned by one lexical scope, including compound bodies."""

        found: dict[str, list[ast.AST]] = {}
        for statement in _iter_scope_statements(body):
            if isinstance(
                statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                found.setdefault(statement.name, []).append(statement)
        return found

    def _lookup_definition(
        self, name: str, *, function: _Function | None
    ) -> tuple[ast.AST | None, bool]:
        """Nearest lexical definition; ambiguous nearest owner fails closed."""

        scope = function
        while scope is not None:
            found = self._scope_definitions(scope.body).get(name)
            if found:
                return (
                    (found[-1], False) if len(found) == 1 else (None, True)
                )
            scope = self._enclosing.get(scope)
        found = self._scope_definitions(self._tree.body).get(name)
        if found:
            return (found[-1], False) if len(found) == 1 else (None, True)
        return (None, False)

    def lookup_class(
        self, name: str, *, function: _Function | None
    ) -> tuple[ast.ClassDef | None, bool]:
        """Nearest lexical class definition of ``name`` plus ambiguity flag."""

        node, ambiguous = self._lookup_definition(name, function=function)
        if isinstance(node, ast.ClassDef):
            return (node, False)
        return (None, ambiguous)

    def _context_aliases(self, node: ast.AST) -> dict[str, str]:
        """Bounded outer alias environment visible to a class body."""

        function = self._enclosing.get(node)
        context = self._outer_context(function)
        if function is not None:
            cached = self._local_aliases.get(id(function))
            if cached:
                context.update(cached)
        return context

    def _visible_aliases(self, function: _Function) -> dict[str, str]:
        """Bounded lexical alias environment visible inside ``function``."""

        context = self._outer_context(function)
        context.update(self.aliases_for(function))
        return context

    def _refresh_class_aliases(self) -> bool:
        changed = False
        for class_key, class_node in self._class_nodes.items():
            local_definitions: dict[str, str] = {}
            for nested_name, definitions in self._scope_definitions(
                class_node.body
            ).items():
                classes = [
                    node
                    for node in definitions
                    if isinstance(node, ast.ClassDef)
                ]
                if not classes:
                    continue
                local_definitions[nested_name] = (
                    self._class_key(classes[0])
                    if len(classes) == 1
                    else _CLASS_AMBIGUOUS
                )
            collected = self._scope_aliases(
                class_node.body,
                function=self._enclosing.get(class_node),
                seed=self._context_aliases(class_node),
                class_key=class_key,
                local_class_definitions=local_definitions,
            )
            previous = self._class_aliases.get(class_key, {})
            if collected != previous:
                merged = dict(previous)
                merged.update(collected)
                self._class_aliases[class_key] = merged
                changed = True
        return changed

    def module_aliases(self) -> Mapping[str, str]:
        return self._module_aliases

    def class_aliases(self, class_key: str) -> Mapping[str, str]:
        return self._class_aliases.get(class_key, {})

    # -- bounded fixpoint ---------------------------------------------------

    def _fixpoint(self) -> None:
        # Every pass can only add entries to the finite alias/return maps, so
        # the loop terminates; the outer bound is a deterministic safety net.
        for _ in range(2 * len(self._functions) + 4):
            changed = False
            refreshed_module = self._scope_aliases(
                self._tree.body, function=None
            )
            if refreshed_module != self._module_aliases:
                merged = dict(self._module_aliases)
                merged.update(refreshed_module)
                self._module_aliases = merged
                changed = True
            for function in self._functions:
                refreshed = self._scope_aliases(
                    function.body,
                    function=function,
                    seed=self._outer_context(function),
                )
                previous = self._local_aliases.get(id(function), {})
                if refreshed and refreshed != previous:
                    merged = dict(previous)
                    merged.update(refreshed)
                    self._local_aliases[id(function)] = merged
                    changed = True
            if self._refresh_class_aliases():
                changed = True
            for function in self._functions:
                identity = self._resolve_own_returns(function)
                if (
                    identity is not None
                    and self._returns.get(id(function)) != identity
                ):
                    self._returns[id(function)] = identity
                    changed = True
            if not changed:
                break

    def _resolve_own_returns(self, function: _Function) -> str | None:
        for expression in _returned_expressions(function):
            resolved = self.value_of(
                expression,
                scope_aliases=self._visible_aliases(function),
                function=function,
                depth=0,
                safety_attribute_fallback=True,
            )
            if resolved is not None:
                return resolved
        return None

    # -- scope-aware function lookup ---------------------------------------

    def lookup_function(
        self, name: str, *, function: _Function | None
    ) -> tuple[_Function | None, bool]:
        """Nearest lexical definition of ``name`` plus an ambiguity flag."""

        node, ambiguous = self._lookup_definition(name, function=function)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return (node, False)
        return (None, ambiguous)

    def _class_slot_value(
        self, slot: str, *, function: _Function | None, depth: int = 0
    ) -> str | None:
        """Resolve a dotted class-qualified composite slot identity."""

        if depth > _RESOLVER_MAX_DEPTH:
            return None
        head, separator, tail = slot.partition(".")
        if not separator:
            head, separator, tail = slot.partition("[")
            if not separator:
                return None
            tail = "[" + tail
        class_node, ambiguous = self.lookup_class(head, function=function)
        if ambiguous:
            return _CLASS_AMBIGUOUS
        if class_node is None:
            return None
        return self._class_slot_in_node(tail, class_node, depth=depth + 1)

    def _class_slot_in_node(
        self, slot: str, class_node: ast.ClassDef, *, depth: int
    ) -> str | None:
        if depth > _RESOLVER_MAX_DEPTH:
            return None
        aliases = self._class_aliases.get(self._class_key(class_node), {})
        value = aliases.get(slot)
        if value is not None:
            return value
        head, separator, tail = slot.partition(".")
        if not separator:
            head, separator, tail = slot.partition("[")
            if not separator:
                return None
            tail = "[" + tail
        nested = aliases.get(head)
        if _is_unresolved_identity(nested):
            return nested
        container_parts = (
            _container_identity_parts(nested)
            if nested is not None
            else None
        )
        if container_parts is not None:
            owner_kind, owner_key, container_path = container_parts
            qualified = _owner_slot_key(
                owner_kind, owner_key, f"{container_path}{tail}"
            )
            value = aliases.get(qualified)
            if value is not None:
                return value
            return self._owner_aliases(owner_kind, owner_key).get(qualified)
        if nested is None or not nested.startswith(_CLASS_REFERENCE_PREFIX):
            return None
        nested_node = self._class_nodes.get(nested)
        if nested_node is None:
            return None
        return self._class_slot_in_node(
            tail, nested_node, depth=depth + 1
        )

    # -- finite expression resolution --------------------------------------

    def value_of(
        self,
        expression: ast.AST,
        *,
        scope_aliases: Mapping[str, str],
        function: _Function | None,
        depth: int = 0,
        safety_attribute_fallback: bool = False,
    ) -> str | None:
        """Identity of the callable/value an expression may evaluate to."""

        if depth > _RESOLVER_MAX_DEPTH:
            return None
        if isinstance(expression, ast.Name):
            identity = _symbol_identity(
                expression,
                imports=self._imports,
                definitions=self._definitions,
                module=self._module,
            )
            if (
                identity is not None
                and identity.rsplit(".", 1)[-1] in self._names
            ):
                return identity
            if expression.id in self._names:
                return expression.id
            value = scope_aliases.get(expression.id)
            if value is not None:
                return value
            value = self._module_aliases.get(expression.id)
            if value is not None:
                return value
            class_node, class_ambiguous = self.lookup_class(
                expression.id, function=function
            )
            if class_ambiguous:
                return _CLASS_AMBIGUOUS
            if class_node is not None:
                return self._class_key(class_node)
            definition, ambiguous = self.lookup_function(
                expression.id, function=function
            )
            if ambiguous:
                return _UNRESOLVED
            if definition is not None and definition is not function:
                return self._returns.get(id(definition))
            return None
        if isinstance(expression, ast.Attribute):
            identity = _symbol_identity(
                expression,
                imports=self._imports,
                definitions=self._definitions,
                module=self._module,
            )
            if (
                identity is not None
                and identity.rsplit(".", 1)[-1] in self._names
            ):
                return identity
            dotted = _dotted_name(expression)
            if dotted is not None:
                value = scope_aliases.get(dotted)
                if value is not None:
                    return value
                value = self._module_aliases.get(dotted)
                if value is not None:
                    return value
                if isinstance(expression.value, ast.Name):
                    class_node, class_ambiguous = self.lookup_class(
                        expression.value.id, function=function
                    )
                    if class_ambiguous:
                        return _CLASS_AMBIGUOUS
                    if class_node is not None:
                        value = self._class_aliases.get(
                            self._class_key(class_node), {}
                        ).get(expression.attr)
                        if value is not None:
                            return value
            container = self.value_of(
                expression.value,
                scope_aliases=scope_aliases,
                function=function,
                depth=depth + 1,
                safety_attribute_fallback=safety_attribute_fallback,
            )
            if container == _CLASS_AMBIGUOUS:
                # Ambiguity is sticky through every supported attribute link.
                return _CLASS_AMBIGUOUS
            if container is not None and container.startswith(
                _CLASS_REFERENCE_PREFIX
            ):
                value = self._class_aliases.get(container, {}).get(
                    expression.attr
                )
                if value is not None:
                    return value
            if safety_attribute_fallback and expression.attr in self._names:
                # A returned bound method / safety-named attribute of an
                # unresolved receiver is a reviewed candidate; fail closed
                # without changing direct-call handling.
                return expression.attr
            return None
        if isinstance(expression, ast.IfExp):
            for branch in (expression.body, expression.orelse):
                value = self.value_of(
                    branch,
                    scope_aliases=scope_aliases,
                    function=function,
                    depth=depth + 1,
                    safety_attribute_fallback=safety_attribute_fallback,
                )
                if value is not None:
                    return value
            return None
        if isinstance(expression, ast.Lambda):
            return self.value_of(
                expression.body,
                scope_aliases=scope_aliases,
                function=function,
                depth=depth + 1,
                safety_attribute_fallback=safety_attribute_fallback,
            )
        if isinstance(expression, ast.NamedExpr):
            return self.value_of(
                expression.value,
                scope_aliases=scope_aliases,
                function=function,
                depth=depth + 1,
                safety_attribute_fallback=safety_attribute_fallback,
            )
        if isinstance(expression, (ast.Tuple, ast.List, ast.Set)):
            for element in expression.elts:
                value = self.value_of(
                    element,
                    scope_aliases=scope_aliases,
                    function=function,
                    depth=depth + 1,
                    safety_attribute_fallback=safety_attribute_fallback,
                )
                if value is not None:
                    return value
            return None
        if isinstance(expression, ast.Dict):
            for value_node in expression.values:
                value = self.value_of(
                    value_node,
                    scope_aliases=scope_aliases,
                    function=function,
                    depth=depth + 1,
                    safety_attribute_fallback=safety_attribute_fallback,
                )
                if value is not None:
                    return value
            return None
        if isinstance(
            expression,
            (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp),
        ):
            for generator in expression.generators:
                value = self.value_of(
                    generator.iter,
                    scope_aliases=scope_aliases,
                    function=function,
                    depth=depth + 1,
                    safety_attribute_fallback=safety_attribute_fallback,
                )
                if value is not None:
                    return value
            elt = getattr(expression, "elt", None)
            if elt is None:
                elt = getattr(expression, "value", None)
            if elt is not None:
                return self.value_of(
                    elt,
                    scope_aliases=scope_aliases,
                    function=function,
                    depth=depth + 1,
                    safety_attribute_fallback=safety_attribute_fallback,
                )
            return None
        if isinstance(expression, ast.Subscript):
            slot = _subscript_slot_key(expression)
            if slot is not None:
                value = scope_aliases.get(slot)
                if value is not None:
                    return value
                value = self._module_aliases.get(slot)
                if value is not None:
                    return value
                value = self._class_slot_value(slot, function=function)
                if value is not None:
                    return value
            container = self.value_of(
                expression.value,
                scope_aliases=scope_aliases,
                function=function,
                depth=depth + 1,
                safety_attribute_fallback=safety_attribute_fallback,
            )
            if container == _CLASS_AMBIGUOUS:
                # Ambiguity is sticky through every supported subscript link.
                return _CLASS_AMBIGUOUS
            container_parts = (
                _container_identity_parts(container)
                if container is not None
                else None
            )
            if container_parts is not None:
                assert container is not None
                return self._container_slot_value(
                    container, expression.slice, scope_aliases
                )
            if container is not None:
                return container
            root = _node_root_name(expression)
            if root is not None:
                if root in self._names:
                    return _UNRESOLVED
                value = scope_aliases.get(root)
                if value is not None:
                    return value
                value = self._module_aliases.get(root)
                if value is not None:
                    return value
            key = expression.slice
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value in self._names
            ):
                return _UNRESOLVED
            return None
        if isinstance(expression, ast.Call):
            func = expression.func
            if isinstance(func, ast.Lambda):
                return self.value_of(
                    func.body,
                    scope_aliases=scope_aliases,
                    function=function,
                    depth=depth + 1,
                    safety_attribute_fallback=safety_attribute_fallback,
                )
            if isinstance(func, ast.Name):
                if (
                    func.id in scope_aliases
                    or func.id in self._module_aliases
                    or func.id in self._names
                ):
                    # Calling an authority alias is the call-site candidate
                    # itself; the call's own value is not a callable.
                    return None
                definition, ambiguous = self.lookup_function(
                    func.id, function=function
                )
                if ambiguous:
                    return _UNRESOLVED
                if definition is not None:
                    return self._returns.get(id(definition))
            if self.call_mentions_safety(
                expression,
                scope_aliases=scope_aliases,
                function=function,
                depth=depth,
            ):
                return _UNRESOLVED
            return None
        return None

    def call_mentions_safety(
        self,
        call: ast.Call,
        *,
        scope_aliases: Mapping[str, str],
        function: _Function | None,
        depth: int = 0,
    ) -> bool:
        """Whether a call passes a safety-relevant value into the callee."""

        if depth > _RESOLVER_MAX_DEPTH:
            return False
        arguments = (
            *call.args,
            *(keyword.value for keyword in call.keywords),
        )
        for argument in arguments:
            if _mentions_safety_symbol(argument, self._names):
                return True
            value = self.value_of(
                argument,
                scope_aliases=scope_aliases,
                function=function,
                depth=depth + 1,
            )
            if value is not None and (
                value == _UNRESOLVED
                or _resolved_boundary_name(value, self._names)
            ):
                return True
        return False


def _boundary_name_symbol(
    *,
    function: _Function | None,
    name: str,
    aliases: Mapping[str, str],
    names: set[str],
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
) -> str | None:
    """Resolve a bare callee name through the finite reviewed alias grammar."""

    imported = imports.get(name)
    if imported is not None:
        return imported
    if name in aliases:
        return aliases[name]
    if function is None:
        return None
    for statement in function.body:
        for node in ast.walk(statement):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                continue
            if isinstance(node, ast.Assign):
                targets: Iterable[ast.AST] = node.targets
            else:
                targets = (node.target,)
            hits = [
                target
                for target in targets
                if isinstance(target, ast.Name) and target.id == name
            ]
            if not hits:
                continue
            if node.value is None:
                continue
            if _is_static_callee_value(
                node.value,
                names=names,
                imports=imports,
                definitions=definitions,
                module=module,
            ):
                return "__unresolved_safety_candidate"
    return None


def _is_callable_alias_reference(
    node: ast.Call,
    aliases: Mapping[str, str],
    names: set[str],
) -> bool:
    if not isinstance(node.func, ast.Name):
        return False
    identity = aliases.get(node.func.id)
    if identity is None:
        return False
    if identity in names:
        return True
    return identity.rsplit(".", 1)[-1] in names


def _known_receiver_names(
    function: _Function | None,
    module_receivers: set[str],
) -> set[str]:
    if function is None:
        return set(module_receivers)
    return module_receivers | _store_receiver_names(function.body)


def _is_local_store_binding(
    function: _Function | None,
    receiver_name: str,
) -> bool:
    if function is None:
        return False
    for statement in function.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        if statement.value is None or not _is_store_construction(statement.value):
            continue
        if isinstance(statement, ast.Assign):
            targets: Iterable[ast.AST] = statement.targets
        else:
            targets = (statement.target,)
        if any(
            isinstance(target, ast.Name) and target.id == receiver_name
            for target in targets
        ):
            return True
    return False


def _binding_for(
    function: _Function,
    name: str,
) -> tuple[ast.Assign | ast.AnnAssign, int] | None:
    bindings: list[tuple[ast.Assign | ast.AnnAssign, int]] = []
    for index, statement in enumerate(function.body):
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets: Iterable[ast.AST]
            if isinstance(statement, ast.Assign):
                targets = statement.targets
            else:
                targets = (statement.target,)
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                bindings.append((statement, index))
        for child in ast.walk(statement):
            if isinstance(child, ast.NamedExpr) and (
                isinstance(child.target, ast.Name) and child.target.id == name
            ):
                return None
    if len(bindings) != 1:
        return None
    return bindings[0]


def _binding_count(function: _Function, name: str) -> int:
    count = 0
    for statement in ast.walk(function):
        if isinstance(statement, ast.Assign):
            targets: Iterable[ast.AST] = statement.targets
        elif isinstance(
            statement, (ast.AnnAssign, ast.NamedExpr, ast.For, ast.AsyncFor)
        ):
            targets = (statement.target,)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            targets = tuple(
                item.optional_vars
                for item in statement.items
                if item.optional_vars is not None
            )
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                count += 1
    return count


def _node_root_name(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    return None


def _contains_name(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(child, ast.Name) and child.id == name for child in ast.walk(node)
    )


def _is_direct_call(
    node: ast.AST,
    identity: str,
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
) -> bool:
    return isinstance(node, ast.Call) and (
        _symbol_identity(
            node.func,
            imports=imports,
            definitions=definitions,
            module=module,
        )
        == identity
    )


def _is_link_valid(
    link: tuple[ast.Assign | ast.AnnAssign, int] | None,
    *,
    function: _Function,
    call: ast.Call,
    identity: str,
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
) -> bool:
    """B5/B5.1/C4(b): one top-level binding, exact callee, before the call."""

    if link is None:
        return False
    statement, binding_index = link
    targets: Iterable[ast.AST]
    if isinstance(statement, ast.Assign):
        targets = statement.targets
    else:
        targets = (statement.target,)
    bound_names = [
        target for target in targets if isinstance(target, ast.Name)
    ]
    if len(bound_names) != 1 or len(list(targets)) != 1:
        return False
    call_index = _statement_index(function, call)
    if call_index is None or binding_index >= call_index:
        return False
    value = statement.value
    if value is None:
        return False
    return _is_direct_call(value, identity, imports, definitions, module)


def _link_is_isolated(
    function: _Function,
    name: str,
    call: ast.Call,
    argument: ast.AST,
    link: tuple[ast.Assign | ast.AnnAssign, int] | None,
) -> bool:
    """No other use of the binding exists before or during the call."""

    if link is None:
        return False
    statement, _binding_index = link
    allowed = {
        child
        for child in ast.walk(argument)
        if isinstance(child, ast.Name) and child.id == name
    }
    for body_statement in function.body:
        if body_statement is statement:
            continue
        for candidate in ast.walk(body_statement):
            if (
                isinstance(candidate, ast.Name)
                and candidate.id == name
                and candidate not in allowed
            ):
                return False
            if isinstance(candidate, ast.Attribute) and candidate.attr == name:
                return False
    for candidate in ast.walk(function):
        if isinstance(candidate, ast.arg) and candidate.arg == name:
            return False
    return True


def _resolve_evidence_argument(
    *,
    function: _Function | None,
    call: ast.Call,
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
) -> bool:
    """Contract B3/B4/B5/B5.1: classify one evaluator operator_evidence."""

    if function is None:
        return False
    keyword = next(
        (kw for kw in call.keywords if kw.arg == "operator_evidence"),
        None,
    )
    if keyword is None:
        return False
    value = keyword.value
    if _is_direct_call(value, _HELPER, imports, definitions, module):
        return True
    if not isinstance(value, ast.Name):
        return False
    link = _binding_for(function, value.id)
    if not _is_link_valid(
        link,
        function=function,
        call=call,
        identity=_HELPER,
        imports=imports,
        definitions=definitions,
        module=module,
    ):
        return False
    return _link_is_isolated(function, value.id, call, value, link)


def _iter_candidates(tree: ast.Module) -> Iterable[ast.Call]:
    """Stage 1: syntactic candidate discovery across the whole module.

    The pre-pass deliberately runs over the entire module - module body,
    class bodies, lambdas, comprehensions and nested functions - before any
    call is resolved.  A syntactically safety-relevant candidate must never
    disappear merely because its callee cannot be resolved.
    """

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def _mentions_safety_symbol(
    node: ast.AST,
    names: frozenset[str] | set[str] = _SAFETY_NAMES,
) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in names:
            return True
        if isinstance(child, ast.Attribute) and child.attr in names:
            return True
        if (
            isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and child.value in names
        ):
            return True
    return False


def _uses_dynamic_lookup_function(node: ast.AST) -> bool:
    if _is_dynamic_lookup_name(node):
        return True
    if isinstance(node, ast.Attribute):
        return _is_dynamic_lookup_name(node.value) or (
            isinstance(node.value, ast.Name)
            and node.value.id in _DYNAMIC_LOOKUP_MODULES
        )
    return False


def _uses_dynamic_lookup(node: ast.Call) -> bool:
    func = node.func
    if _uses_dynamic_lookup_function(func):
        return True
    return isinstance(func, ast.Attribute) and _uses_dynamic_lookup_function(
        func.value
    )


def _keyed_safety_lookup(node: ast.Call, names: set[str]) -> bool:
    """getattr/__dict__/subscript lookups that name a safety symbol."""

    func = node.func
    if isinstance(func, ast.Name) and func.id == "getattr":
        if len(node.args) < 2:
            return False
        key = node.args[1]
        if isinstance(key, ast.Constant):
            return key.value in names
        return _mentions_safety_symbol(key, names)
    if isinstance(func, ast.Subscript):
        return _mentions_safety_symbol(func.value, names)
    if not isinstance(func, ast.Attribute):
        return False
    if _mentions_safety_symbol(node, names):
        return True
    if func.attr == "__dict__":
        return True
    if isinstance(func.value, ast.Call):
        inner = func.value.func
        if isinstance(inner, ast.Attribute) and inner.attr == "__dict__":
            return True
        if isinstance(inner, ast.Name) and inner.id == "getattr":
            return True
    return isinstance(func.value, ast.Subscript) and _mentions_safety_symbol(
        func.value, names
    )


def _is_unresolved_attribute_boundary(
    node: ast.Call,
    *,
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
    names: set[str],
) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in names:
        return False
    return (
        _symbol_identity(
            func,
            imports=imports,
            definitions=definitions,
            module=module,
        )
        is None
    )


def _resolves_to_dynamic_module(
    node: ast.AST,
    imports: Mapping[str, str],
) -> bool:
    identity = imports.get(node.id) if isinstance(node, ast.Name) else None
    if identity is None:
        return False
    return identity.split(".", 1)[0] in _DYNAMIC_LOOKUP_MODULES


def _is_star_ambiguous(node: ast.Call, imports: Mapping[str, str]) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        identity = imports.get(func.id)
        if identity is not None and identity.endswith("*"):
            return True
        return any(
            value.endswith("*")
            for name, value in imports.items()
            if name == func.id
        )
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        identity = imports.get(func.value.id)
        if identity is not None:
            return False
        return any(
            value.endswith("*")
            for name, value in imports.items()
            if name == func.attr
        )
    return False


def _invoked_call_nodes(tree: ast.AST) -> set[int]:
    invoked: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            invoked.add(id(node.func))
    return invoked


def _is_dynamic_getattr_callee(node: ast.Call, invoked: set[int]) -> bool:
    func = node.func
    if not (isinstance(func, ast.Name) and func.id == "getattr"):
        return False
    if len(node.args) < 2:
        return False
    key = node.args[1]
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return False
    return id(node) in invoked


def _is_unresolved_construction_candidate(
    node: ast.Call,
) -> bool:
    func = node.func
    return isinstance(func, (ast.Attribute, ast.Subscript)) and bool(
        _mentions_safety_symbol(func, _CONSTRUCTION_NAMES)
    )


def _resolves_to_partial(
    node: ast.Call,
    imports: Mapping[str, str],
) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "partial":
        return True
    return (
        isinstance(func, ast.Name)
        and imports.get(func.id) == "functools.partial"
    )


def _boundary_operand_mentions(
    node: ast.AST,
    *,
    names: set[str],
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
    resolved_names: Mapping[str, str],
) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            if child.id in names:
                return True
            resolved = resolved_names.get(child.id)
            if _resolved_boundary_name(resolved, names):
                return True
        elif isinstance(child, ast.Attribute) and child.attr in names:
            return True
    return False


def _resolved_boundary_name(
    resolved: str | None,
    names: set[str],
) -> bool:
    if resolved is None:
        return False
    if _is_unresolved_identity(resolved):
        return True
    return resolved in names or resolved.rsplit(".", 1)[-1] in names


def _is_construction_reference(identity: str | None) -> bool:
    """Whether an identity is a spelling of a reviewed construction symbol."""

    if identity is None:
        return False
    return (
        identity in _CONSTRUCTION_NAMES
        or identity.rsplit(".", 1)[-1] in _CONSTRUCTION_NAMES
    )


def _is_boundary_callee_expression(
    node: ast.Call,
    *,
    names: set[str],
    imports: Mapping[str, str],
    definitions: Mapping[str, str],
    module: str | None,
    resolved_names: Mapping[str, str],
    resolver: _ModuleResolver | None = None,
    function: _Function | None = None,
) -> bool:
    """Stage 1: does the callee syntactically target a reviewed boundary?"""

    func = node.func
    if isinstance(func, ast.Name):
        if func.id in names:
            return True
        return _resolved_boundary_name(resolved_names.get(func.id), names)
    if isinstance(func, ast.Attribute):
        identity = _symbol_identity(
            func,
            imports=imports,
            definitions=definitions,
            module=module,
        )
        if identity is not None and identity.rsplit(".", 1)[-1] in names:
            # Canonical module-attribute spellings resolve through the
            # canonical path, never through the unresolved-alias gate.
            return False
        if identity is not None and "." not in identity:
            return False
        if resolver is not None:
            resolved = resolver.value_of(
                func,
                scope_aliases=resolved_names,
                function=function,
            )
            if _resolved_boundary_name(resolved, names):
                return True
        return func.attr in names
    if isinstance(func, ast.Subscript):
        if (
            isinstance(func.slice, ast.Constant)
            and isinstance(func.slice.value, str)
            and func.slice.value in names
        ):
            # A reflection table keyed by a safety-relevant symbol name is a
            # candidate that cannot be resolved to a canonical identity.
            return True
        if resolver is not None:
            resolved = resolver.value_of(
                func,
                scope_aliases=resolved_names,
                function=function,
            )
            if _resolved_boundary_name(resolved, names):
                return True
        return _boundary_operand_mentions(
            func.value,
            names=names,
            imports=imports,
            definitions=definitions,
            module=module,
            resolved_names=resolved_names,
        )
    if isinstance(func, ast.IfExp):
        if resolver is not None:
            resolved = resolver.value_of(
                func,
                scope_aliases=resolved_names,
                function=function,
            )
            if _resolved_boundary_name(resolved, names):
                return True
        return _boundary_operand_mentions(
            func,
            names=names,
            imports=imports,
            definitions=definitions,
            module=module,
            resolved_names=resolved_names,
        )
    if isinstance(func, ast.Lambda):
        if resolver is not None:
            resolved = resolver.value_of(
                func,
                scope_aliases=resolved_names,
                function=function,
            )
            if _resolved_boundary_name(resolved, names):
                return True
        return False
    if isinstance(func, ast.Call):
        if _resolves_to_partial(func, imports):
            for argument in func.args:
                if _boundary_operand_mentions(
                    argument,
                    names=names,
                    imports=imports,
                    definitions=definitions,
                    module=module,
                    resolved_names=resolved_names,
                ):
                    return True
            for keyword in func.keywords:
                if _boundary_operand_mentions(
                    keyword.value,
                    names=names,
                    imports=imports,
                    definitions=definitions,
                    module=module,
                    resolved_names=resolved_names,
                ):
                    return True
        if resolver is not None:
            resolved = resolver.value_of(
                func,
                scope_aliases=resolved_names,
                function=function,
            )
            if _resolved_boundary_name(resolved, names):
                return True
            if resolver.call_mentions_safety(
                func,
                scope_aliases=resolved_names,
                function=function,
            ):
                return True
        return False
    if isinstance(func, ast.NamedExpr):
        if resolver is not None:
            resolved = resolver.value_of(
                func.value,
                scope_aliases=resolved_names,
                function=function,
            )
            if _resolved_boundary_name(resolved, names):
                return True
        return _boundary_operand_mentions(
            func.value,
            names=names,
            imports=imports,
            definitions=definitions,
            module=module,
            resolved_names=resolved_names,
        )
    return False


def scan_tripwire_a(
    sources: Mapping[str, str],
) -> tuple[Violation, ...]:
    violations: list[Violation] = []
    for path, source in sources.items():
        normalized = path.replace("\\", "/")
        if "/src/" not in f"/{normalized}":
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        imports = _canonical_imports(tree)
        definitions = _module_definitions(tree)
        module = _module_name(path)
        enclosing = _enclosing_functions(tree)
        resolver = _ModuleResolver(
            tree,
            names=_SAFETY_NAMES,
            imports=imports,
            definitions=definitions,
            module=module,
        )
        module_aliases = dict(resolver.module_aliases())
        alias_cache: dict[int, dict[str, str]] = {}
        scopes = _ScopeMap()
        scopes.visit(tree)
        candidate_lines: set[int] = set()
        allowed_lines: set[int] = set()
        for node in _iter_candidates(tree):
            function = _function_for(node, enclosing)
            aliases = dict(module_aliases)
            for scope in _scope_chain(function, enclosing):
                cached = alias_cache.get(id(scope))
                if cached is None:
                    cached = dict(resolver.aliases_for(scope))
                    alias_cache[id(scope)] = cached
                aliases.update(cached)
            identity = _symbol_identity(
                node.func,
                imports=imports,
                definitions=definitions,
                module=module,
            )
            if identity is not None:
                if identity != _EVIDENCE:
                    # A locally derived identity (for example a class-body
                    # alias or object attribute) is still an unresolved
                    # construction candidate and must not disappear.
                    if not _is_construction_reference(
                        identity
                    ) and _is_boundary_callee_expression(
                        node,
                        names=_CONSTRUCTION_NAMES,
                        imports=imports,
                        definitions=definitions,
                        module=module,
                        resolved_names=aliases,
                        resolver=resolver,
                        function=function,
                    ):
                        candidate_lines.add(node.lineno)
                    continue
                candidate_lines.add(node.lineno)
                qualname = scopes.qualnames.get(node, "")
                allowed = (
                    normalized.endswith("src/quantforge/frontier/models.py")
                    and qualname == "_parse_frontier_model"
                ) or (
                    normalized.endswith("src/quantforge/operator/store.py")
                    and qualname
                    in {
                        "SQLiteOperatorControlStore.produce_operator_evidence",
                        "operator_evidence_for_frontier",
                    }
                )
                if allowed:
                    allowed_lines.add(node.lineno)
                continue
            if _is_unresolved_construction_candidate(node):
                candidate_lines.add(node.lineno)
                continue
            if _is_boundary_callee_expression(
                node,
                names=_CONSTRUCTION_NAMES,
                imports=imports,
                definitions=definitions,
                module=module,
                resolved_names=aliases,
                resolver=resolver,
                function=function,
            ):
                candidate_lines.add(node.lineno)
                continue
            if _uses_dynamic_lookup(node) and _mentions_safety_symbol(
                node, _CONSTRUCTION_NAMES
            ):
                candidate_lines.add(node.lineno)
        for line in sorted(candidate_lines - allowed_lines):
            violations.append(
                Violation(
                    "TRIPWIRE-A",
                    path,
                    line,
                    "forbidden or unresolved evidence construction candidate",
                )
            )
    return tuple(violations)


def scan_tripwire_b(
    sources: Mapping[str, str],
) -> tuple[Violation, ...]:
    violations: list[Violation] = []
    for path, source in sources.items():
        normalized = path.replace("\\", "/")
        if "/src/" not in f"/{normalized}":
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        imports = _canonical_imports(tree)
        definitions = _module_definitions(tree)
        module = _module_name(path)
        resolver = _ModuleResolver(
            tree,
            names=_SAFETY_NAMES,
            imports=imports,
            definitions=definitions,
            module=module,
        )
        module_aliases = dict(resolver.module_aliases())
        enclosing = _enclosing_functions(tree)
        alias_cache: dict[int, dict[str, str]] = {}
        for node in _iter_candidates(tree):
            function = _function_for(node, enclosing)
            if _is_star_ambiguous(node, imports):
                violations.append(
                    Violation(
                        "TRIPWIRE-B",
                        path,
                        node.lineno,
                        "star import makes the evaluator identity ambiguous",
                    )
                )
                continue
            aliases = dict(module_aliases)
            for scope in _scope_chain(function, enclosing):
                cached = alias_cache.get(id(scope))
                if cached is None:
                    cached = dict(resolver.aliases_for(scope))
                    alias_cache[id(scope)] = cached
                aliases.update(cached)
            identity = _symbol_identity(
                node.func,
                imports=imports,
                definitions=definitions,
                module=module,
            )
            evaluator_callee_names = {"evaluate_frontier_eligibility"}
            direct_candidate = identity == _EVALUATOR
            dynamic_candidate = (
                identity is None
                and (
                    _uses_dynamic_lookup(node)
                    or _keyed_safety_lookup(
                        node, {"evaluate_frontier_eligibility"}
                    )
                )
                and (
                    _mentions_safety_symbol(
                        node, {"evaluate_frontier_eligibility"}
                    )
                    or (
                        isinstance(node.func, ast.Name)
                        and node.func.id == "getattr"
                        and any(
                            _mentions_safety_symbol(
                                argument,
                                {"evaluate_frontier_eligibility"},
                            )
                            for argument in node.args[1:2]
                        )
                    )
                )
            )
            boundary_callee = _is_boundary_callee_expression(
                node,
                names={"evaluate_frontier_eligibility"},
                imports=imports,
                definitions=definitions,
                module=module,
                resolved_names=aliases,
                resolver=resolver,
                function=function,
            )
            bare_evaluator_callee = (
                isinstance(node.func, ast.Name)
                and node.func.id == "evaluate_frontier_eligibility"
            )
            unresolved_boundary_alias = (
                boundary_callee
                and not bare_evaluator_callee
                and not (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "evaluate_frontier_eligibility"
                    and _symbol_identity(
                        node.func,
                        imports=imports,
                        definitions=definitions,
                        module=module,
                    )
                    == _EVALUATOR
                )
            )
            alias_candidate = _is_callable_alias_reference(
                node, aliases, evaluator_callee_names
            )
            if unresolved_boundary_alias:
                violations.append(
                    Violation(
                        "TRIPWIRE-B",
                        path,
                        node.lineno,
                        "evaluator candidate uses an unresolved alias form",
                    )
                )
                continue
            if not (
                direct_candidate
                or dynamic_candidate
                or alias_candidate
                or bare_evaluator_callee
                or boundary_callee
            ):
                continue
            if identity is None:
                violations.append(
                    Violation(
                        "TRIPWIRE-B",
                        path,
                        node.lineno,
                        "unresolved evaluator callee fails closed",
                    )
                )
                continue
            if alias_candidate:
                violations.append(
                    Violation(
                        "TRIPWIRE-B",
                        path,
                        node.lineno,
                        "callable alias of the evaluator fails closed",
                    )
                )
                continue
            keyword = next(
                (kw for kw in node.keywords if kw.arg == "operator_evidence"),
                None,
            )
            if keyword is None:
                violations.append(
                    Violation(
                        "TRIPWIRE-B",
                        path,
                        node.lineno,
                        "evaluator call has no explicit operator_evidence",
                    )
                )
                continue
            if not _resolve_evidence_argument(
                function=function,
                call=node,
                imports=imports,
                definitions=definitions,
                module=module,
            ):
                violations.append(
                    Violation(
                        "TRIPWIRE-B",
                        path,
                        node.lineno,
                        "operator_evidence is not a permitted CATEGORY 1 origin",
                    )
                )
    return tuple(violations)


def scan_tripwire_c(
    sources: Mapping[str, str],
) -> tuple[Violation, ...]:
    violations: list[Violation] = []
    for path, source in sources.items():
        normalized = path.replace("\\", "/")
        if "/src/" not in f"/{normalized}":
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        imports = _canonical_imports(tree)
        definitions = _module_definitions(tree)
        module = _module_name(path)
        resolver = _ModuleResolver(
            tree,
            names=_SAFETY_NAMES,
            imports=imports,
            definitions=definitions,
            module=module,
        )
        module_aliases = dict(resolver.module_aliases())
        module_receivers = _store_receiver_names(tree.body)
        scopes = _ScopeMap()
        scopes.visit(tree)
        enclosing = _enclosing_functions(tree)
        alias_cache: dict[int, dict[str, str]] = {}
        invoked = _invoked_call_nodes(tree)
        for node in _iter_candidates(tree):
            function = _function_for(node, enclosing)
            aliases = dict(module_aliases)
            for scope in _scope_chain(function, enclosing):
                cached = alias_cache.get(id(scope))
                if cached is None:
                    cached = dict(resolver.aliases_for(scope))
                    alias_cache[id(scope)] = cached
                aliases.update(cached)
            receiver_names = _known_receiver_names(function, module_receivers)
            dispatch_names = {"mark_dispatching", *_PRIVATE_WRITERS}
            alias_candidate = _is_callable_alias_reference(
                node, aliases, dispatch_names
            )
            func = node.func
            if (
                _is_boundary_callee_expression(
                    node,
                    names=dispatch_names,
                    imports=imports,
                    definitions=definitions,
                    module=module,
                    resolved_names=aliases,
                    resolver=resolver,
                    function=function,
                )
                and not isinstance(func, ast.Attribute)
            ):
                violations.append(
                    Violation(
                        "TRIPWIRE-C",
                        path,
                        node.lineno,
                        "indirect dispatch-boundary candidate is unresolved",
                    )
                )
                continue
            if (
                isinstance(func, ast.Attribute)
                and not _resolved_boundary_name(
                    _symbol_identity(
                        func,
                        imports=imports,
                        definitions=definitions,
                        module=module,
                    ),
                    dispatch_names,
                )
                and _resolved_boundary_name(
                    resolver.value_of(
                        func,
                        scope_aliases=aliases,
                        function=function,
                    ),
                    dispatch_names,
                )
            ):
                violations.append(
                    Violation(
                        "TRIPWIRE-C",
                        path,
                        node.lineno,
                        "indirect dispatch-boundary candidate is unresolved",
                    )
                )
                continue
            if (
                _is_dynamic_getattr_callee(node, invoked)
            ):
                violations.append(
                    Violation(
                        "TRIPWIRE-C",
                        path,
                        node.lineno,
                        "dynamic dispatch lookup cannot be resolved statically",
                    )
                )
                continue
            if alias_candidate:
                violations.append(
                    Violation(
                        "TRIPWIRE-C",
                        path,
                        node.lineno,
                        "callable alias of the dispatch boundary is unresolved",
                    )
                )
                continue
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.attr == "mark_dispatching"
                and not _receiver_is_statically_known(
                    function=function,
                    receiver=func.value,
                    scopes=scopes,
                    receiver_names=receiver_names,
                    module_receivers=module_receivers,
                    imports=imports,
                )
            ):
                violations.append(
                    Violation(
                        "TRIPWIRE-C",
                        path,
                        node.lineno,
                        "dispatch receiver cannot be statically resolved",
                    )
                )
                continue
            if (
                isinstance(func, ast.Attribute)
                and not isinstance(func.value, ast.Name)
                and func.attr
                in {
                    "mark_dispatching",
                    *_PRIVATE_WRITERS,
                }
                and not (
                    isinstance(func.value, ast.Call)
                    and _is_store_construction(func.value)
                )
            ):
                violations.append(
                    Violation(
                        "TRIPWIRE-C",
                        path,
                        node.lineno,
                        "dispatch receiver cannot be statically resolved",
                    )
                )
                continue
            if isinstance(func, ast.Subscript) and _is_dispatch_candidate(
                node, aliases
            ):
                violations.append(
                    Violation(
                        "TRIPWIRE-C",
                        path,
                        node.lineno,
                        "indirect dispatch-boundary candidate is unresolved",
                    )
                )
                continue
            if isinstance(func, ast.IfExp) and _is_dispatch_candidate(node, aliases):
                violations.append(
                    Violation(
                        "TRIPWIRE-C",
                        path,
                        node.lineno,
                        "conditional dispatch callable is unresolved",
                    )
                )
                continue
            if _is_star_ambiguous(node, imports):
                violations.append(
                    Violation(
                        "TRIPWIRE-C",
                        path,
                        node.lineno,
                        "star import makes the dispatch identity ambiguous",
                    )
                )
                continue
            identity = _symbol_identity(
                node.func,
                imports=imports,
                definitions=definitions,
                module=module,
            )
            known_receiver = (
                isinstance(func, ast.Attribute)
                and (
                    func.attr == "mark_dispatching"
                    or func.attr in _PRIVATE_WRITERS
                )
                and isinstance(func.value, ast.Name)
                and _receiver_is_statically_known(
                    function=function,
                    receiver=func.value,
                    scopes=scopes,
                    receiver_names=receiver_names,
                    module_receivers=module_receivers,
                    imports=imports,
                )
            )
            private_violation = _private_writer_violation(
                node,
                path=path,
                function=function,
                scopes=scopes,
            )
            if private_violation is not None:
                violations.append(private_violation)
                continue
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "mark_dispatching"
                and _is_unresolved_attribute_boundary(
                    node,
                    imports=imports,
                    definitions=definitions,
                    module=module,
                    names={"mark_dispatching", *_PRIVATE_WRITERS},
                )
                and not known_receiver
            ):
                violations.append(
                    Violation(
                        "TRIPWIRE-C",
                        path,
                        node.lineno,
                        "dispatch receiver cannot be statically resolved",
                    )
                )
                continue
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _PRIVATE_WRITERS
                and private_violation is None
            ):
                continue
            if identity != _DISPATCH and not known_receiver:
                if _mentions_safety_symbol(node.func, dispatch_names) or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in dispatch_names
                ):
                    violations.append(
                        Violation(
                            "TRIPWIRE-C",
                            path,
                            node.lineno,
                            "unresolved dispatch-boundary candidate",
                        )
                    )
                continue
            decision = node.args[0] if node.args else None
            if decision is None:
                decision = next(
                    (kw.value for kw in node.keywords if kw.arg == "eligibility"),
                    None,
                )
            if decision is None or function is None:
                violations.append(
                    Violation(
                        "TRIPWIRE-C",
                        path,
                        node.lineno,
                        "mark_dispatching call has no decision argument",
                    )
                )
                continue
            if _is_direct_call(
                decision, _EVALUATOR, imports, definitions, module
            ):
                assert isinstance(decision, ast.Call)
                if not _resolve_evidence_argument(
                    function=function,
                    call=decision,
                    imports=imports,
                    definitions=definitions,
                    module=module,
                ):
                    violations.append(
                        Violation(
                            "TRIPWIRE-C",
                            path,
                            node.lineno,
                            "decision evaluator does not satisfy Tripwire B",
                        )
                    )
                continue
            if not isinstance(decision, ast.Name):
                violations.append(
                    Violation(
                        "TRIPWIRE-C",
                        path,
                        node.lineno,
                        "mark_dispatching decision is not a direct evaluator result",
                    )
                )
                continue
            link = _binding_for(function, decision.id)
            if not _is_link_valid(
                link,
                function=function,
                call=node,
                identity=_EVALUATOR,
                imports=imports,
                definitions=definitions,
                module=module,
            ):
                violations.append(
                    Violation(
                        "TRIPWIRE-C",
                        path,
                        node.lineno,
                        "decision local is not a permitted evaluator binding",
                    )
                )
                continue
            if not _link_is_isolated(
                function, decision.id, node, decision, link
            ):
                violations.append(
                    Violation(
                        "TRIPWIRE-C",
                        path,
                        node.lineno,
                        "decision binding is mutated, aliased or reused",
                    )
                )
                continue
            statement, _binding_index = link  # type: ignore[misc]
            assert isinstance(statement, (ast.Assign, ast.AnnAssign))
            bound_call = statement.value
            assert isinstance(bound_call, ast.Call)
            if not _resolve_evidence_argument(
                function=function,
                call=bound_call,
                imports=imports,
                definitions=definitions,
                module=module,
            ):
                violations.append(
                    Violation(
                        "TRIPWIRE-C",
                        path,
                        node.lineno,
                        "decision evaluator does not satisfy Tripwire B",
                    )
                )
    return tuple(violations)


def execstate_topology(source: str) -> TopologyResult:
    tree = ast.parse(source)
    scopes = _ScopeMap()
    scopes.visit(tree)
    actual: set[tuple[str, str, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {
            "_update_submission_state",
            "_insert_receipt",
            "_write_audit",
        }:
            continue
        qualname = scopes.qualnames.get(node.func.value, "")
        if not qualname.startswith("SQLiteExecutionStateStore."):
            continue
        actual.add((node.func.attr, qualname, node.lineno))
    return TopologyResult(
        actual=frozenset(actual),
        approved=_APPROVED_EXECSTATE_EDGES,
    )


def scan_repository(root: Path) -> tuple[Violation, ...]:
    sources: dict[str, str] = {}
    for path in root.rglob("*.py"):
        sources[str(path)] = path.read_text(encoding="utf-8")
    return (
        scan_tripwire_a(sources)
        + scan_tripwire_b(sources)
        + scan_tripwire_c(sources)
    )


__all__ = [
    "TopologyResult",
    "Violation",
    "execstate_topology",
    "scan_repository",
    "scan_tripwire_a",
    "scan_tripwire_b",
    "scan_tripwire_c",
]
