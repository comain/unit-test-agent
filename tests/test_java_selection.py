from types import SimpleNamespace

import pytest

from uta.language.java.selection import (
    _has_listener_registration_hint,
    is_accessor_like_method_name,
    is_testable_class,
)


def _graph(
    fqn="com.example.Service",
    *,
    kind="class",
    path="/repo/src/main/java/com/example/Service.java",
    annotations=None,
    modifiers=None,
    methods=("run",),
):
    nodes = {
        fqn: SimpleNamespace(
            kind=kind,
            file_path=path,
            metadata={
                "annotations": list(annotations or []),
                "modifiers": list(modifiers or []),
            },
        )
    }
    for name in methods:
        nodes[f"{fqn}.{name}"] = SimpleNamespace(
            kind="method",
            fqn=f"{fqn}.{name}",
            metadata={
                "parent_fqn": fqn,
                "modifiers": ["public"],
                "complexity": {},
                "annotations": [],
            },
        )
    return SimpleNamespace(nodes=nodes)


@pytest.mark.parametrize(
    "fqn,kwargs",
    [
        ("com.example.NotAClass", {"kind": "method"}),
        ("com.example.Api", {"annotations": ["RestController"]}),
        ("com.example.RemoteAdapter", {}),
        ("com.example.ContestLogic", {}),
        ("com.example.Worker", {"path": "/repo/src/main/java/com/example/adapter/Worker.java"}),
        ("com.example.AbstractService", {"modifiers": ["abstract"]}),
        ("com.example.EmptyService", {"methods": ()}),
    ],
)
def test_java_selection_rejects_each_low_signal_class_shape(fqn, kwargs):
    assert not is_testable_class(fqn, _graph(fqn, **kwargs))


def test_java_selection_handles_object_methods_and_annotation_driven_registrations():
    assert is_accessor_like_method_name("equals")
    method = SimpleNamespace(metadata={"annotations": ["com.example.WMQConsumer"]})
    assert _has_listener_registration_hint("ConsumerHooks", [method])
