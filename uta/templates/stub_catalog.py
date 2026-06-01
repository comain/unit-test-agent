"""Static Mockito stub-pattern catalog keyed by collaborator FQN (token_opt_phase2 strategy J).

Provides pre-written stub patterns for the most common Spring/Java collaborator types
so the model doesn't re-derive them in every compile-fix turn.

Auto-seeded by L3 project-level summary when run frequency data is available.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Built-in stub patterns for well-known collaborator types.
# Key: short type name (case-sensitive). Value: list of Mockito stub lines.
# ---------------------------------------------------------------------------
_BUILTIN_CATALOG: Dict[str, List[str]] = {
    "JdbcTemplate": [
        "when(jdbcTemplate.queryForObject(anyString(), any(RowMapper.class), any())).thenReturn(/* expected */);",
        "when(jdbcTemplate.queryForList(anyString(), any(RowMapper.class))).thenReturn(Collections.emptyList());",
        "when(jdbcTemplate.update(anyString(), any())).thenReturn(1);",
    ],
    "RestTemplate": [
        "when(restTemplate.getForObject(anyString(), any(Class.class))).thenReturn(/* expected */);",
        "when(restTemplate.postForObject(anyString(), any(), any(Class.class))).thenReturn(/* expected */);",
        "when(restTemplate.exchange(anyString(), any(HttpMethod.class), any(), any(Class.class))).thenReturn(ResponseEntity.ok(/* expected */));",
    ],
    "RedisTemplate": [
        "when(redisTemplate.opsForValue()).thenReturn(valueOperations);",
        "when(valueOperations.get(anyString())).thenReturn(/* expected */);",
        "doNothing().when(valueOperations).set(anyString(), any());",
    ],
    "StringRedisTemplate": [
        "when(stringRedisTemplate.opsForValue()).thenReturn(valueOps);",
        "when(valueOps.get(anyString())).thenReturn(\"cachedValue\");",
    ],
    "ApplicationEventPublisher": [
        "doNothing().when(eventPublisher).publishEvent(any());",
    ],
    "MessageSource": [
        "when(messageSource.getMessage(anyString(), any(), any(Locale.class))).thenReturn(\"message\");",
    ],
    "HttpServletRequest": [
        "when(request.getParameter(anyString())).thenReturn(\"value\");",
        "when(request.getHeader(anyString())).thenReturn(\"header-value\");",
        "when(request.getAttribute(anyString())).thenReturn(null);",
    ],
    "HttpServletResponse": [
        "doNothing().when(response).setStatus(anyInt());",
        "doNothing().when(response).setHeader(anyString(), anyString());",
    ],
    "ObjectMapper": [
        "when(objectMapper.writeValueAsString(any())).thenReturn(\"{}\");",
        "when(objectMapper.readValue(anyString(), any(Class.class))).thenReturn(/* expected */);",
    ],
}

# ---------------------------------------------------------------------------
# Short-name to FQN prefix mapping for enriching catalog entries.
# ---------------------------------------------------------------------------
_FQN_PREFIXES: Dict[str, str] = {
    "JdbcTemplate": "org.springframework.jdbc.core",
    "RestTemplate": "org.springframework.web.client",
    "RedisTemplate": "org.springframework.data.redis.core",
    "StringRedisTemplate": "org.springframework.data.redis.core",
    "ApplicationEventPublisher": "org.springframework.context",
    "MessageSource": "org.springframework.context",
    "HttpServletRequest": "javax.servlet.http",
    "HttpServletResponse": "javax.servlet.http",
    "ObjectMapper": "com.fasterxml.jackson.databind",
}


def get_stub_patterns(short_name: str) -> List[str]:
    """Return built-in stub patterns for ``short_name``, or empty list if unknown."""
    return list(_BUILTIN_CATALOG.get(short_name, []))


def get_stub_patterns_for_deps(dep_types: List[str]) -> Dict[str, List[str]]:
    """Return stub patterns for each dep type that has built-in patterns."""
    result: Dict[str, List[str]] = {}
    for dep in dep_types:
        short = dep.split(".")[-1]
        patterns = get_stub_patterns(short)
        if patterns:
            result[short] = patterns
    return result


def format_stub_catalog_md(dep_types: List[str]) -> str:
    """Format relevant stub patterns as Markdown for injection into a generation prompt."""
    catalog = get_stub_patterns_for_deps(dep_types)
    if not catalog:
        return ""
    lines = ["## Pre-Built Stub Patterns", ""]
    for type_name, patterns in catalog.items():
        lines.append(f"### `{type_name}`")
        for p in patterns:
            lines.append(f"```java\n{p}\n```")
        lines.append("")
    return "\n".join(lines)


def seed_from_project_summary(summary: Dict) -> Dict[str, List[str]]:
    """Extend the in-memory catalog with top symbols from an L3 project summary.

    Only adds entries for symbols that already have built-in patterns (enriches
    frequency metadata rather than creating new stub content).

    Returns the set of short names that were already known (no new entries added
    for unknown types — stub content requires human/LLM authoring).
    """
    known: Dict[str, List[str]] = {}
    for sym in summary.get("top_resolved_symbols") or []:
        short = sym.get("short_name", "")
        if short in _BUILTIN_CATALOG:
            known[short] = _BUILTIN_CATALOG[short]
    return known
