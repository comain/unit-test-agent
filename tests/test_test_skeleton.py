"""Tests for uta.templates.test_skeleton (K strategy — deterministic skeleton generation)."""

import pytest
from uta.templates.test_skeleton import (
    SkeletonSpec,
    generate_test_skeleton,
    spec_from_context_md,
)

SAMPLE_CONTEXT = """\
# Target Test Context

## Class
- FQN: `com.example.service.OrderService`
- Source: `src/main/java/com/example/service/OrderService.java`

## Imports
- `com.example.repo.OrderRepository`
- `com.example.repo.UserRepository`
- `java.util.List`

## Fields
- `orderRepo` : `OrderRepository`
- `userRepo` : `UserRepository`

## Public Methods
- `createOrder(Long userId, List<Item> items)`
- `cancelOrder(Long orderId)`
- `getOrder(Long orderId)`

## Dependency Types
- `OrderRepository` — `src/main/java/com/example/repo/OrderRepository.java`
- `UserRepository` — `src/main/java/com/example/repo/UserRepository.java`

## Relevant Process Flows
- (no flow extracted)
"""


def test_generate_test_skeleton_has_package():
    spec = SkeletonSpec(
        class_fqn="com.example.service.OrderService",
        package="com.example.service",
        simple_name="OrderService",
        test_simple_name="OrderServiceTest",
    )
    code = generate_test_skeleton(spec)
    assert "package com.example.service;" in code


def test_generate_includes_junit4_mockito_runner():
    spec = SkeletonSpec(
        class_fqn="com.example.FooService",
        package="com.example",
        simple_name="FooService",
        test_simple_name="FooServiceTest",
        dependency_types=["OrderRepository"],
    )
    code = generate_test_skeleton(spec)
    assert "@RunWith(MockitoJUnitRunner.class)" in code
    assert "import org.junit.Test;" in code


def test_generate_includes_inject_mocks():
    spec = SkeletonSpec(
        class_fqn="com.example.FooService",
        package="com.example",
        simple_name="FooService",
        test_simple_name="FooServiceTest",
        dependency_types=["OrderRepository"],
    )
    code = generate_test_skeleton(spec)
    assert "@InjectMocks" in code
    assert "private FooService underTest;" in code


def test_generate_mock_fields_for_deps():
    spec = SkeletonSpec(
        class_fqn="com.example.FooService",
        package="com.example",
        simple_name="FooService",
        test_simple_name="FooServiceTest",
        dependency_types=["OrderRepository", "UserRepository"],
    )
    code = generate_test_skeleton(spec)
    assert "@Mock" in code
    assert "private OrderRepository orderRepository;" in code
    assert "private UserRepository userRepository;" in code


def test_generate_skips_primitive_mock_types():
    spec = SkeletonSpec(
        class_fqn="com.example.Foo",
        package="com.example",
        simple_name="Foo",
        test_simple_name="FooTest",
        dependency_types=["String", "int", "OrderRepository"],
    )
    code = generate_test_skeleton(spec)
    assert "private String string;" not in code
    assert "private OrderRepository orderRepository;" in code


def test_generate_includes_placeholder_test():
    spec = SkeletonSpec(
        class_fqn="com.example.Foo",
        package="com.example",
        simple_name="Foo",
        test_simple_name="FooTest",
    )
    code = generate_test_skeleton(spec)
    assert "@Test" in code
    assert "assertNotNull(underTest);" in code


def test_spec_from_context_md_extracts_package():
    spec = spec_from_context_md(SAMPLE_CONTEXT, "com.example.service.OrderService")
    assert spec.package == "com.example.service"
    assert spec.simple_name == "OrderService"
    assert spec.test_simple_name == "OrderServiceTest"


def test_spec_from_context_md_extracts_dependencies():
    spec = spec_from_context_md(SAMPLE_CONTEXT, "com.example.service.OrderService")
    assert "OrderRepository" in spec.dependency_types
    assert "UserRepository" in spec.dependency_types


def test_spec_from_context_md_extracts_imports():
    spec = spec_from_context_md(SAMPLE_CONTEXT, "com.example.service.OrderService")
    fqns = [i if "." in i else "" for i in spec.imports]
    assert any("OrderRepository" in i for i in fqns)


def test_full_pipeline_produces_valid_structure():
    spec = spec_from_context_md(SAMPLE_CONTEXT, "com.example.service.OrderService")
    code = generate_test_skeleton(spec)
    assert "package com.example.service;" in code
    assert "class OrderServiceTest" in code
    assert "@InjectMocks" in code
    assert "MockitoJUnitRunner" in code
