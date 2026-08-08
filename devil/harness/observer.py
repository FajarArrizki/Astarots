"""Resolve declared state references against canonical local fork projections."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from devil.core.snapshot import JsonRpcClient, SnapshotSet, keccak_hex
from devil.core.types import ChainId, GlobalState
from devil.invariant.ir import (
    BindingReduce,
    CrossChainInvariant,
    StateReference,
    StateReferenceKind,
)


class EvmBindingObserver:
    """Read getters/storage paths without guessing bindings or numeric conversions."""

    def __init__(
        self,
        invariant: CrossChainInvariant,
        snapshot_set: SnapshotSet,
        clients: Mapping[ChainId, JsonRpcClient],
        *,
        storage_layouts: Mapping[str, Mapping[str, str]] | None = None,
        transforms: Mapping[str, Callable[..., Any]] | None = None,
    ) -> None:
        self.invariant = invariant
        self.snapshot_set = snapshot_set
        self.clients = dict(clients)
        self.storage_layouts = {
            context: dict(layout) for context, layout in (storage_layouts or {}).items()
        }
        self.transforms = dict(transforms or {})

    def __call__(self, state: GlobalState) -> Mapping[str, Any]:
        observed: dict[str, Any] = {}
        keys = tuple(
            sorted(
                {
                    item.envelope.correlation_value or identity
                    for identity, item in state.pending_messages.items()
                }
            )
        )
        for binding in self.invariant.bindings:
            parameterized = any(source.arguments for source in binding.sources)
            if parameterized:
                observed[binding.id] = {
                    key: self._resolve_binding(binding, state, observed, key) for key in keys
                }
            else:
                observed[binding.id] = self._resolve_binding(binding, state, observed, None)
        return observed

    def _resolve_binding(
        self,
        binding: Any,
        state: GlobalState,
        observed: Mapping[str, Any],
        key: str | None,
    ) -> Any:
        values = [self._read_reference(source, state, observed, key) for source in binding.sources]
        if binding.reduce is BindingReduce.IDENTITY:
            result = values[0]
        elif binding.reduce is BindingReduce.SUM:
            result = sum(values)
        elif binding.reduce is BindingReduce.DIFF:
            result = values[0]
            for value in values[1:]:
                result -= value
        else:
            if binding.transform is None:
                raise ValueError(f"binding {binding.id!r} custom reducer has no transform")
            function = self.transforms.get(
                f"{binding.transform.function}@{binding.transform.version}"
            )
            if function is None:
                raise ValueError(f"binding {binding.id!r} transform is unavailable")
            result = function(*values)
        if binding.transform is not None and binding.reduce is not BindingReduce.CUSTOM:
            function = self.transforms.get(
                f"{binding.transform.function}@{binding.transform.version}"
            )
            if function is None:
                raise ValueError(f"binding {binding.id!r} transform is unavailable")
            result = function(result)
        return result

    def _read_reference(
        self,
        reference: StateReference,
        state: GlobalState,
        observed: Mapping[str, Any],
        key: str | None,
    ) -> Any:
        context = self.invariant.contexts[reference.context_id]
        chain = context.chain_id
        client = self.clients[chain]
        target = self.snapshot_set.base_fingerprints[chain].targets[reference.context_id]
        arguments = [_argument_value(name, observed, key) for name in reference.arguments]
        if reference.kind is StateReferenceKind.GETTER:
            if reference.getter is None:
                raise ValueError("getter state reference is incomplete")
            calldata = _encode_call(client, reference.getter.function_signature, arguments)
            raw = str(client.call("eth_call", [{"to": target.address, "data": calldata}, "latest"]))
        else:
            raw = self._read_storage(client, target.address, reference, arguments)
        value = _decode_word(raw, reference.value_type)
        if reference.result_path not in {None, "", "0"}:
            raise ValueError("tuple/array result_path needs an ABI decoder adapter")
        return value

    def _read_storage(
        self,
        client: JsonRpcClient,
        address: str,
        reference: StateReference,
        arguments: list[Any],
    ) -> str:
        if reference.storage_path is None:
            raise ValueError("storage state reference is incomplete")
        variable = reference.storage_path.partition("[")[0]
        slot_text = self.storage_layouts.get(reference.context_id, {}).get(variable)
        if slot_text is None:
            raise ValueError(
                f"storage path {reference.context_id}.{variable} is absent from validated layout"
            )
        slot = int(slot_text, 0)
        for argument in arguments:
            encoded = _encode_static(argument, "bytes32") + slot.to_bytes(32, "big")
            digest = keccak_hex(client, "0x" + encoded.hex())
            slot = int(str(digest), 16)
        return str(client.call("eth_getStorageAt", [address, hex(slot), "latest"]))


def _argument_value(name: str, observed: Mapping[str, Any], key: str | None) -> Any:
    if name in observed:
        value = observed[name]
        if isinstance(value, Mapping):
            if key is None or key not in value:
                raise ValueError(f"binding argument {name!r} has no current quantifier value")
            return value[key]
        return value
    if key is not None:
        return key
    if name.startswith("0x") or name.isdigit():
        return name
    raise ValueError(f"binding argument {name!r} is unresolved")


def _encode_call(client: JsonRpcClient, signature: str, arguments: list[Any]) -> str:
    name, separator, type_list = signature.partition("(")
    if not separator or not signature.endswith(")") or not name:
        raise ValueError(f"invalid canonical getter signature {signature!r}")
    types = [item.strip() for item in type_list[:-1].split(",") if item.strip()]
    if len(types) != len(arguments):
        raise ValueError(f"getter {signature!r} argument arity mismatch")
    signature_hex = "0x" + signature.encode().hex()
    selector = keccak_hex(client, signature_hex)[:10]
    encoded = b"".join(
        _encode_static(value, value_type)
        for value, value_type in zip(arguments, types, strict=True)
    )
    return selector + encoded.hex()


def _encode_static(value: Any, value_type: str) -> bytes:
    if value_type == "address":
        integer = int(str(value), 16)
    elif value_type == "bool":
        integer = int(bool(value))
    elif re.fullmatch(r"u?int(?:8|16|32|64|128|256)?", value_type):
        integer = int(str(value), 0)
    elif value_type == "bytes32":
        cleaned = str(value).removeprefix("0x")
        if len(cleaned) > 64:
            raise ValueError("bytes32 argument is too wide")
        return bytes.fromhex(cleaned.rjust(64, "0"))
    else:
        raise ValueError(f"dynamic or unsupported ABI type {value_type!r}")
    if integer < 0:
        integer %= 2**256
    return integer.to_bytes(32, "big")


def _decode_word(raw: str, value_type: str) -> Any:
    cleaned = raw.removeprefix("0x")
    if len(cleaned) < 64:
        raise ValueError("EVM state read returned less than one word")
    word = cleaned[:64]
    if value_type == "bool":
        return bool(int(word, 16))
    if value_type == "address":
        return "0x" + word[-40:]
    if value_type == "bytes32":
        return "0x" + word
    if re.fullmatch(r"uint(?:8|16|32|64|128|256)?", value_type):
        return int(word, 16)
    if re.fullmatch(r"int(?:8|16|32|64|128|256)?", value_type):
        value = int(word, 16)
        return value - 2**256 if value >= 2**255 else value
    raise ValueError(f"unsupported state value type {value_type!r}")
