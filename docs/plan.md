# Unit Test Agent — Implementation Plan

## Context

We're building a Python CLI tool (`uta`) that automatically generates unit tests for legacy Java codebases across the organization. Target repos span multiple domains (`~/wms/`, `~/platform/`, `~/tms/`, `~/md/`, `~/fd/`) but share the same tech stack: Java 8 / Maven / Spring / JUnit 4 / Mockito 1.x, with `@Resource` DI, Dubbo/RPC adapters, MyBatis ORM, and internal template patterns.

The tool should be:
- **Repo-agnostic**: works on any Java Maven project with the above stack — no hardcoded repo assumptions
- **Incremental**: git-log based file selection, cached parsing for repeat runs
- **Portable**: no local DB dependencies (Kuzu, SQLite, etc.) — just Python + JSON cache files, deployable to any node
- **AI-powered**: uses OpenCode headless server with Gemini Pro subscription
- **Quality-gated**: test pass, coverage rate, mutation test pass

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Python CLI (uta)  —  LangGraph Orchestrator    │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │ Context  │→ │ Generate │→ │ Validate │      │
│  │ Prepare  │  │ (OpenCode│  │ (Maven/  │      │
│  │ (git +   │  │  Server) │  │  Jacoco/ │      │
│  │ treesit) │  │          │  │  Pitest) │      │
│  └──────────┘  └──────────┘  └──────────┘      │
│       ↑              ↑              │            │
│       │         HTTP API            │ fix-loop   │
│       │              ↓              ↓            │
│  ┌─────────────────────────────────────────┐    │
│  │  OpenCode Headless Server               │    │
│  │  (Gemini Pro model, filesystem + bash)  │    │
│  └─────────────────────────────────────────┘    │
└─────────────────────────────────────────────────┘
```

**Key decision**: OpenCode runs as a headless HTTP server (`opencode serve`). Our Python orchestrator drives it via REST API — creating sessions, sending prompts with rich context, and receiving generated test code. OpenCode handles file I/O and Maven execution with Gemini Pro as the LLM brain.

## Project Structure

```
unit-test-agent/
├── pyproject.toml
├── design/
│   ├── architecture.md           # System architecture, data flow, component roles
│   ├── opencode-integration.md   # OpenCode server API usage, session management
│   ├── prompt-strategy.md        # Prompt templates rationale, Mockito 1.x constraints
│   ├── pipeline.md               # LangGraph nodes, state transitions, fix-loop logic
│   ├── parsing.md                # Code parsing strategy, graph schema, cache format
│   └── target-codebase.md        # Common patterns across target repos
├── tests/                        # Validation scripts (pytest)
│   ├── conftest.py               # Shared fixtures: sample Java files, test repo path
│   ├── test_git_scanner.py       # Step 2: git log parsing, file ranking
│   ├── test_java_parser.py       # Step 4: tree-sitter extraction on real Java files
│   ├── test_graph_builder.py     # Step 5: import/call resolution, edge correctness
│   ├── test_process_extractor.py # Step 6: BFS flow detection
│   ├── test_cache.py             # Step 7: cache write/read/invalidation
│   ├── test_opencode_client.py   # Step 8: mock HTTP server interaction
│   ├── test_prompt_render.py     # Step 9: prompt template rendering with real data
│   └── test_pipeline_e2e.py      # Step 10+: end-to-end single-class run
├── tests/fixtures/               # Small Java files for fast unit tests
│   ├── SampleService.java        # Simple @Service with @Resource fields
│   ├── SampleMapper.java         # MyBatis mapper interface
│   ├── SampleBizImpl.java        # SampleBizTemplate pattern example
│   └── SampleMockTest.java       # Example existing test (reference style)
├── uta/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py                    # Click CLI entry point
│   ├── config.py                 # Pydantic settings
│   ├── language/                 # Language-specific implementations
│   │   ├── java/parse/           # Java parser library
│   │   │   ├── __init__.py       # Java ParseProvider binding
│   │   │   ├── models.py         # ParsedSymbol, ExtractedCall, CodeGraph, ProcessFlow, etc.
│   │   │   ├── java_parser.py    # tree-sitter Java AST -> symbols, calls, heritage
│   │   │   ├── graph_builder.py  # Build in-memory code graph (nodes + edges)
│   │   │   ├── process_extractor.py # Detect execution flows from entry points
│   │   │   ├── cache.py          # JSON-file cache keyed by file content hash
│   │   │   └── queries.py        # Query helpers
│   │   └── python/parse/         # Python parser and context extraction
│   ├── context/
│   │   ├── __init__.py
│   │   ├── git_scanner.py        # git log → ranked file list
│   │   └── context_builder.py    # Legacy context wrapper; language context lives under uta/language/
│   ├── opencode/
│   │   ├── __init__.py
│   │   ├── server.py             # Start/stop opencode serve
│   │   ├── client.py             # HTTP client for OpenCode API (provider-agnostic)
│   │   ├── config.py             # Generate opencode.json for project
│   │   └── auth.py               # Auth strategy: gemini (current), extensible to others
│   ├── maven/
│   │   ├── __init__.py
│   │   ├── jacoco.py             # Parse coverage XML
│   │   └── pitest.py             # Parse mutation XML
│   ├── prompts/
│   │   ├── generate_test.txt     # Test generation prompt template
│   │   ├── fix_test.txt          # Fix compilation/test errors
│   │   ├── enhance_coverage.txt  # Improve coverage after Jacoco
│   │   └── fix_mutations.txt     # Kill surviving mutants
│   ├── graph/
│   │   ├── __init__.py
│   │   ├── state.py              # LangGraph TypedDict state
│   │   ├── nodes.py              # Node functions
│   │   └── workflow.py           # Graph construction
│   └── output/
│       ├── __init__.py
│       └── reporter.py           # Summary report
```

## CLI Interface

```bash
uta run \
  --repo ~/wms/sample-outbound-core \
  --module biz \
  --days 30 \
  --max-files 10 \
  --coverage-gate 60 \
  --mutation-gate 70 \
  --fix-code \
  --verbose

uta scan --repo ~/wms/sample-outbound-core --days 30   # dry-run: list candidates

uta parse --repo ~/wms/sample-outbound-core --module biz  # one-time parse, cache results
                                                            # shows: classes, call graph, process flows
```

## Pipeline (LangGraph Nodes)

### Node 1: `scan_and_select`
- `git log --since="{N}days" --name-only` → rank `.java` files by change frequency
- Filter: `src/main/java` only, exclude tests, apply `--module` filter
- Limit to `--max-files`

### Node 2: `parse_context` (uses `uta/language/java/parse/` library)

This is a **one-time deep parse** of the entire module, cached for reuse across runs. The Java parsing logic lives in `uta/language/java/parse/`; other language parsers live under their own `uta/language/<language>/parse/` package. The approach follows GitNexus's multi-phase pipeline adapted for our needs.

#### Phase A: Scan & Parse (all `.java` files in module)

Uses tree-sitter with `tree-sitter-java` grammar. For each file, extract:

```python
@dataclass
class ParsedSymbol:
    kind: str           # "class" | "interface" | "enum" | "method" | "field"
    name: str
    fqn: str            # fully qualified name (package.Class.method)
    line: int
    modifiers: list[str]  # public, private, static, abstract
    annotations: list[Annotation]  # @Resource, @Component, @DubboService, etc.
    params: list[Param]   # for methods: [(type, name), ...]
    return_type: str      # for methods
    parent_fqn: str       # enclosing class FQN

@dataclass
class ParsedFile:
    path: str
    package: str
    imports: list[str]       # import statements
    symbols: list[ParsedSymbol]
    calls: list[CallSite]    # method invocations: {caller_fqn, callee_name, callee_object, line}
    heritage: Heritage       # extends/implements: {parent_type, interfaces[]}
    external_calls: list[ExternalCall]  # DB/RPC/messaging annotations detected
```

**Tree-sitter queries** (aligned with GitNexus `tree-sitter-queries.ts` patterns):

```scheme
; Classes, interfaces, enums, records
(class_declaration name: (identifier) @name) @definition.class
(interface_declaration name: (identifier) @name) @definition.interface
(enum_declaration name: (identifier) @name) @definition.enum

; Methods with full signature
(method_declaration
  (modifiers)? @mods
  type: (_) @return_type
  name: (identifier) @name
  parameters: (formal_parameters) @params) @definition.method

; Fields (annotated or not)
(field_declaration
  (modifiers)? @mods
  type: (_) @type
  declarator: (variable_declarator name: (identifier) @name)) @definition.field

; Method invocations (for call graph — like GitNexus ExtractedCall)
(method_invocation
  object: (_)? @receiver        ; receiver → resolve type via field declarations
  name: (identifier) @calledName
  arguments: (argument_list) @args) @call

; Java method references (Type::method, this::method — GitNexus call-sites/java.ts)
(method_reference) @method_ref

; Heritage
(superclass (type_identifier) @heritage.extends)
(super_interfaces (type_list (type_identifier) @heritage.implements))

; Annotations with arguments
(annotation
  name: (_) @ann_name
  arguments: (annotation_argument_list)? @ann_args) @annotation

; Constructor invocations (new Type(...) — for type binding)
(object_creation_expression
  type: (_) @constructor_type) @constructor_call
```

**Per-file output** (mirrors GitNexus `ParseWorkerResult`):
```python
@dataclass
class ParseResult:
    symbols: list[ParsedSymbol]        # classes, methods, fields
    imports: list[ExtractedImport]     # import statements
    calls: list[ExtractedCall]         # method invocations with receiver info
    heritage: list[ExtractedHeritage]  # extends/implements
    annotations: list[Annotation]      # all annotations on classes/methods/fields
    field_bindings: dict[str, str]     # field_name → type_name (for call receiver resolution)
```

**Java-specific pattern detection** (in `java_parser.py`):
- **DI fields**: `@Resource`, `@Autowired`, `@Inject` → these become `@Mock` fields in tests
- **Messaging**: `@WMQConsumer`, `@WMQProducer`, `@KafkaListener` → external call type "messaging"
- **Database**: `@Select`, `@Update`, `@Insert`, `@Delete` or field types containing `Mapper` → external call type "database"
- **RPC**: `@DubboReference`, `@DubboService`, `@Reference` → external call type "rpc"
- **Spring beans**: `@Component`, `@Service`, `@Controller` → mark as entry point candidates
- **Template patterns**: detect `new SampleBizTemplate<T>() { ... }` and similar anonymous class patterns → extract inner method bodies

#### Phase B: Resolve & Link (build code graph)

In `graph_builder.py`, build an in-memory graph (plain Python dicts, no DB dependency):

```python
@dataclass
class CodeGraph:
    nodes: dict[str, GraphNode]   # keyed by FQN
    edges: list[GraphEdge]

@dataclass
class GraphNode:
    fqn: str
    kind: str          # "class" | "method" | "field"
    file_path: str
    line: int
    metadata: dict     # annotations, modifiers, return_type, params

@dataclass
class GraphEdge:
    source: str        # FQN
    target: str        # FQN
    relation: str      # CONTAINS | CALLS | IMPORTS | EXTENDS | IMPLEMENTS | INJECTS
```

**Resolution steps** (following GitNexus pipeline phases 3-4):

1. **Import resolution** (like GitNexus `import-resolvers/jvm.ts`):
   - Wildcard imports: `com.example.*` → match all `.java` files in that package path
   - Member imports: `com.example.Foo` → resolve to file containing `Foo` class
   - Build `simple_name → FQN` index for the entire module
   
2. **Heritage resolution**: Link `extends`/`implements` to actual class FQNs. Build EXTENDS and IMPLEMENTS edges.

3. **Call resolution** (like GitNexus `ExtractedCall` with receiver type inference):
   - For each call site, resolve receiver type: `receiverName` (e.g. `sowStorage`) → look up field type from `field_bindings` → resolve to FQN via import index
   - Match `calledName` + `argCount` to target method (arity filtering for overloads)
   - Confidence scoring: direct type match = 1.0, inferred = 0.7, global name match = 0.3
   - Only create CALLS edge if confidence ≥ 0.5

4. **Injection resolution**: For each `@Resource`/`@Autowired` field, resolve the interface to its implementation class (Spring convention: `FooBiz` interface → `FooBizImpl`). Creates INJECTS edges.

5. **Cross-file binding propagation** (like GitNexus phase 14, lines 324-486):
   - Topologically sort files by import order
   - Seed downstream files with exported type bindings from upstream
   - Re-resolve calls in downstream files with enriched type info
   - Cap at module boundary to avoid memory bloat

#### Phase C: Process-Flow Extraction

In `process_extractor.py`, detect execution flows by tracing from entry points through call chains:

```python
@dataclass
class ProcessFlow:
    name: str              # e.g. "SowBiz.confirmSow"
    entry_point: str       # FQN of entry method
    steps: list[FlowStep]  # ordered sequence of calls
    external_deps: list[ExternalCall]  # DB/RPC/messaging touched

@dataclass
class FlowStep:
    fqn: str
    kind: str              # "internal_call" | "db_query" | "rpc_call" | "mq_publish"
    detail: str            # table name, service name, topic name
```

**Algorithm** (adapted from GitNexus `process-processor.ts`):

1. **Score & rank entry points** (like GitNexus lines 270-336):
   - Score = `(outgoing_calls × framework_multiplier) / (incoming_calls + 1)`
   - `framework_multiplier`: `@Controller`/`@DubboService` methods get 2x boost
   - Exclude test files, private methods
   - Take top 200 candidates

2. **BFS trace from each entry point** (like GitNexus lines 346-394):
   - Follow CALLS edges with confidence ≥ 0.5
   - Max depth: 10, max branching: 4 (avoid combinatorial explosion)
   - Track visited to avoid cycles
   - At each node, check for external call annotations → record as FlowStep

3. **Deduplicate flows** (like GitNexus lines 404-450):
   - Remove subset traces (if flow A is a prefix of flow B, keep B)
   - Keep longest trace per entry→terminal pair
   - Cap at 75 processes per module

4. Output: list of `ProcessFlow` objects per class

**Why this matters for test generation**: The process flow tells the LLM *what the method actually does* end-to-end (e.g. "validates input → queries DB via OrderMapper → calls DubboService → publishes to MQ"). This is far richer context than just the source code.

#### Phase D: Cache (JSON files, no local DB)

In `cache.py`, cache parsed results as plain JSON files — **zero external dependencies**, deployable anywhere:

```python
# Cache location: {repo}/.uta_cache/{module}/
#   parse_index.json     — file hash index
#   parsed/{hash}.json   — per-file parse result
#   graph.json           — resolved code graph
#   flows.json           — extracted process flows

@dataclass
class CacheIndex:
    entries: dict[str, CacheEntry]  # rel_path → CacheEntry

@dataclass
class CacheEntry:
    content_hash: str    # SHA-256 of file content (NOT mtime — portable across machines)
    parsed_at: str       # ISO timestamp
    parse_file: str      # path to cached JSON result
```

**Cache logic**:
- On parse: compute `SHA-256(file_content)` for each `.java` file
- If hash matches cache index → skip, load from `parsed/{hash}.json`
- If hash differs or missing → re-parse, update cache
- Graph and flows are rebuilt only if any constituent file changed
- **No SQLite, no Kuzu, no DB** — just JSON files under `.uta_cache/`
- Cache files are **git-managed in the `unit-code-gen` branch** (see Branch Strategy below)

### Node 2.5: `baseline_compile` (includes Mockito upgrade)

**Before any test generation, verify the project compiles cleanly and has modern test dependencies:**

**Step A — Upgrade Mockito to 2+ (one-time per repo)**:
1. Check current Mockito version in `pom.xml` (or parent pom)
2. If Mockito 1.x detected:
   - Update dependency: `mockito-all:1.10.19` → `mockito-core:2.28.2` (or latest 2.x)
   - Note: `mockito-all` bundled Hamcrest; `mockito-core` doesn't — may need to add `hamcrest-core` explicitly
   - Migrate existing test imports: `org.mockito.Matchers` → `org.mockito.ArgumentMatchers`, `org.mockito.runners.MockitoJUnitRunner` → `org.mockito.junit.MockitoJUnitRunner`
   - Send migration task to OpenCode agent for existing tests that need import updates
3. If already 2+ → skip

**Why upgrade first**: LLMs naturally generate Mockito 2+ code (`ArgumentMatchers.any()`, etc.). Fighting this with prompt constraints is fragile. Upgrading the project eliminates the mismatch entirely — the LLM's default output compiles cleanly.

**Step B — Baseline compile check**:
1. Run `mvn compile -pl {module} -DskipTests=true`
2. If compilation **fails**:
   - Send errors to OpenCode: "Fix these baseline compilation errors"
   - Max 3 attempts
3. Run `mvn test-compile -pl {module} -DskipTests=false` to verify test infrastructure compiles
4. Commit all fixes to `unit-code-gen` branch

This prevents wasting time generating tests against code that doesn't compile.

### Node 2.7: `setup_branch`

All generated artifacts are managed in a dedicated git branch `unit-code-gen`:

**Branch strategy**:
```
origin/master ──→ unit-code-gen (branch)
                    ├── .uta_cache/         # parse and workflow cache files
                    ├── .uta_reports/        # coverage + mutation reports
                    ├── .uta_summary.md      # project-scope summary (from opencode init)
                    ├── src/test/java/...    # generated test files
                    └── (production code)    # merged from master
```

**Logic**:
1. Check if `unit-code-gen` branch exists locally or on remote
2. If not: `git checkout -b unit-code-gen origin/master`
3. If exists: `git checkout unit-code-gen && git merge origin/master` (keep up-to-date with prod code)
4. All generated files (cache, tests, reports) are committed to this branch
5. At end of run: `git add .uta_cache/ .uta_reports/ src/test/java/` → commit → push to `origin/unit-code-gen`
6. Production code fixes (if any) are committed separately for easy cherry-picking to master

**Benefits**:
- Generated tests don't pollute master branch
- Cache persists across machines (clone + checkout branch = ready)
- Reports accumulate with git history — can track coverage trends over time
- Easy to review: `git diff master..unit-code-gen` shows all generated artifacts

### Node 2.8: `project_summary`

Generate a **project-scope summary** via `opencode init` and store it in the repo:

**How**:
1. Run `opencode` in the target repo root — OpenCode automatically generates a project summary (CLAUDE.md / .opencode/summary.md) by scanning the codebase structure
2. Store as `.uta_summary.md` in the repo root (on `unit-code-gen` branch)
3. Feed this summary as top-level context to every test generation prompt

**What it provides**:
- Project architecture overview (modules, layers, data flow)
- Tech stack details (frameworks, libraries, versions)
- Coding conventions and patterns
- Key domain concepts

This gives the LLM a "big picture" understanding of the project before diving into per-class test generation.

### Node 3: `start_opencode`
- Start `opencode serve --port 4096` as subprocess
- Configure `opencode.json` in repo root with Gemini Pro model and agent definition:
  ```json
  {
    "provider": { "google": { "model": "gemini-2.5-pro" } },
    "agent": {
      "test-gen": {
        "description": "Generates JUnit 4 + Mockito 1.x unit tests",
        "mode": "subagent",
        "prompt": "{file:./prompts/generate_test.txt}",
        "permission": { "edit": "allow", "bash": { "*": "allow" } }
      }
    }
  }
  ```

### Node 4: `generate_and_validate` (loop per class — MERGED node)

**Key design**: Generation, compilation, test execution, and fix-it are merged into a **single OpenCode agent session**. The agent autonomously writes the test, runs `mvn test`, reads errors, and fixes — all within one session. This is faster than orchestrating each step as a separate graph node because:
- The agent has full conversation context (no re-prompting)
- File system state is consistent within the session
- The agent can make judgment calls (is this a test bug or a real code bug?)

**Prompt to OpenCode** (single session, one prompt with instructions):
```
Context provided:
  - Project summary (.uta_summary.md — architecture, conventions, domain concepts)
  - Production class source code (full file)
  - Process flows touching this class: e.g. "confirmSow → sowStorage.query → orderMapper.select → mqProducer.send"
  - Dependency signatures: for each @Resource field, interface/class method signatures (from code graph)
  - Call graph neighborhood: callers and callees
  - Existing test example from project (auto-discovered *MockTest.java in same module)
  - Recent git commit messages for the file
  - Framework constraints: JUnit 4, MockitoJUnitRunner, ArgumentMatchers.any(), @Mock + @InjectMocks

Instructions to agent:
  1. Write the test file to src/test/java/... (correct package path)
  2. Run: mvn test-compile -pl {module} -DskipTests=false
  3. If compile fails, fix the test and retry (max 3 attempts)
  4. Run: mvn test -pl {module} -Dtest={TestClass} -DskipTests=false
  5. If tests fail:
     - If you are confident the production code has a bug → fix the production code, not the test
     - Otherwise → fix the test and retry
  6. Run with Jacoco: check coverage. If below {coverage_gate}%, add more test cases
  7. Report: PASS/FAIL + coverage % + any production code fixes applied
```

The orchestrator polls OpenCode session events (`GET /global/event`) to track progress and detect completion.

### Node 5: `mutation_gate` (separate graph node)

Mutation testing stays as a separate graph node because:
- Pitest runs are slow (~minutes per class) — worth running only after tests pass
- Mutation results require structured analysis (which mutants survived, which lines)
- Enhancement prompt needs Pitest report as structured input

**Steps**:
1. Run Pitest: `mvn org.pitest:pitest-maven:mutationCoverage -DtargetClasses={fqn} -DtargetTests={testFqn}`
2. Parse mutation XML → identify surviving mutants (line numbers, mutation types)
3. If mutation score ≥ `--mutation-gate` → PASS
4. If below gate → send mutation report to OpenCode: "These mutants survived on lines X,Y,Z. Add assertions to kill them."
5. Re-run Pitest after enhancement (max 2 iterations)

### Node 6: `commit_to_branch` + `store_report`

After each class is processed, commit to the `unit-code-gen` branch:
- `git add src/test/java/{TestClass}.java .uta_cache/`
- `git commit -m "uta: generate test for {ClassFqn}"`

At the end of the run, store reports and push:

```
{repo}/.uta_reports/
  coverage_{module}_{date}.json      # per-class coverage before/after
  mutations_{module}_{date}.json     # mutation scores per class
  summary_{module}_{date}.json       # overall run summary
  fixes/                             # production code diffs if any
    {ClassFqn}_{date}.patch
```

- `git add .uta_reports/ && git commit -m "uta: reports for {module} {date}"`
- `git push origin unit-code-gen`
- Reports accumulate with git history — track coverage trends over time
- Terminal output: Rich table summarizing results

### Graph Flow (simplified — gen+validate merged)
```
scan_and_select → parse_context → baseline_compile → setup_branch → project_summary → start_opencode
                                                                        ↓
                                                                  select_next_class
                                                                        ↓
                                                              generate_and_validate
                                                              (agent self-loops:
                                                               write → compile → fix → run → fix → coverage)
                                                                        ↓
                                                                  mutation_gate
                                                                  (Pitest → enhance if needed)
                                                                        ↓
                                                                  commit_to_branch
                                                                  (git add + commit on unit-code-gen)
                                                                        ↓
                                                                  has_more? → select_next_class
                                                                        ↓ (no)
                                                                  store_report → push_branch → END
```

## Dependencies

```toml
[project]
name = "unit-test-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "click>=8.1",
    "langgraph>=0.2",
    "langchain-core>=0.3",
    "tree-sitter>=0.22",
    "tree-sitter-java>=0.23",
    "pydantic>=2.0",
    "httpx>=0.27",          # async HTTP client for OpenCode API
    "rich>=13.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.24", "respx>=0.22"]

[project.scripts]
uta = "uta.cli:main"

[tool.setuptools.packages.find]
include = ["uta*"]
```

**External prerequisite**: `opencode` installed via npm (`npm install -g @opencode-ai/opencode`), configured with Gemini Pro subscription credentials.

## Design Directory Convention

The `design/` directory holds living design documents that must reflect the current code:

| File | Tracks |
|------|--------|
| `architecture.md` | Overall system diagram, module responsibilities, dependency graph |
| `opencode-integration.md` | How we drive OpenCode server (endpoints used, session lifecycle, agent config) |
| `prompt-strategy.md` | Why each prompt template is structured the way it is, known LLM pitfalls (e.g. Mockito version confusion) |
| `pipeline.md` | LangGraph state machine, node contracts, retry/skip logic |
| `parsing.md` | Code parsing strategy: tree-sitter queries, graph schema, process-flow algorithm, cache format |
| `target-codebase.md` | Common patterns across target repos (wms/platform/tms/md/fd) that inform test generation |

**Rule**: When code in `uta/` changes, the relevant design doc must be updated in the same commit. Design docs describe *why*, not *what* — the code is the source of truth for *what*.

## OpenCode + LLM Provider Setup

The OpenCode integration is **provider-agnostic** — currently supports Gemini Pro, extensible to other providers (Claude, GPT, etc.) via `uta/opencode/auth.py`.

**`auth.py` design** — strategy pattern:
```python
class AuthStrategy(ABC):
    @abstractmethod
    def provider_config(self) -> dict: ...  # returns opencode.json provider block

class GeminiSubscriptionAuth(AuthStrategy):
    """Uses opencode-gemini-auth plugin for Google AI Pro subscription."""
    def provider_config(self) -> dict:
        return {"google": {"model": "gemini-2.5-pro"}}

# Future: class AnthropicApiKeyAuth(AuthStrategy): ...
# Future: class OpenAIAuth(AuthStrategy): ...
```

**Current setup (Gemini Pro)**:
1. Install OpenCode: `npm install -g @opencode-ai/opencode`
2. Install Gemini auth plugin: see [opencode-gemini-auth](https://github.com/jenslys/opencode-gemini-auth)
3. `uta` auto-generates `opencode.json` with the configured provider
4. Starts `opencode serve --port 4096` automatically, communicates via HTTP REST API
5. OpenCode handles filesystem access + terminal execution; Python provides orchestration

## Implementation Order

Each step is small, validated independently, committed and pushed to `git@git.example.com:example-org/unittest-generaor.git`.

**Step 0 — Project init** [DONE]
- `git init`, set remote, create `.gitignore` [DONE]
- `pyproject.toml` + package skeleton (`uta/__init__.py`, `uta/__main__.py`) [DONE]
- `design/` directory with initial docs [DONE]
- `tests/conftest.py` + `tests/fixtures/` with sample Java files [DONE]
- Move raw `design` file content into `design/architecture.md` [DONE]
- Validate: `pip install -e ".[dev]"` succeeds, `pytest --co` discovers no errors. [DONE]

**Step 1 — Config + CLI skeleton** [DONE]
- `uta/config.py` — Pydantic settings model [DONE]
- `uta/cli.py` — Click CLI with `run`, `scan`, `parse` commands (stubs) [DONE]
- Validate: `uta --help` works, `uta scan --help` shows flags [DONE]

**Step 2 — Git scanner** [DONE]
- `uta/context/git_scanner.py` — git log parsing + file ranking [DONE]
- `tests/test_git_scanner.py` — test with real repo (`~/wms/sample-outbound-core`) [DONE]
- Validate: `pytest tests/test_git_scanner.py -v` [DONE]

**Step 3 — Java parse data models** [DONE]
- `uta/language/java/parse/__init__.py`, `uta/language/java/parse/models.py` — all dataclasses (ParsedSymbol, ExtractedCall, CodeGraph, ProcessFlow, etc.) [DONE]
- Validate: `from uta.language.java.parse.models import ParsedSymbol, CodeGraph` works [DONE]

**Step 4 — Tree-sitter Java parser** [DONE]
- `uta/language/java/parse/java_parser.py` — extract symbols, calls, fields, annotations, heritage [DONE]
- `tests/test_java_parser.py` — test against `tests/fixtures/SampleService.java` + real files [DONE]
  - Assert: correct class name, method count, @Resource fields extracted, call sites found [DONE]
- Validate: `pytest tests/test_java_parser.py -v` [DONE]

**Step 5 — Graph builder** [DONE]
- `uta/language/java/parse/graph_builder.py` — import resolution, call resolution, heritage linking [DONE]
- `tests/test_graph_builder.py` — build graph from fixtures, assert: [DONE]
  - CALLS edges between SampleService → SampleMapper methods [DONE]
  - IMPLEMENTS edge for interface→impl [DONE]
  - Import resolution resolves known types [DONE]
- Validate: `pytest tests/test_graph_builder.py -v` [DONE]

**Step 6 — Process flow extractor** [DONE]
- `uta/language/java/parse/process_extractor.py` — BFS flow detection [DONE]
- `tests/test_process_extractor.py` — assert flows detected from entry point through DB/RPC calls [DONE]
- Validate: `pytest tests/test_process_extractor.py -v` [DONE]

**Step 7 — Cache + query helpers** [DONE]
- `uta/language/java/parse/cache.py` — JSON file cache with content-hash keying [DONE]
- `uta/language/java/parse/queries.py` — query helpers: `get_class_deps(fqn)`, `get_flows_for(fqn)`, `get_callers(fqn)`, `get_method_signatures(fqn)` [DONE]
- `tests/test_cache.py` — test write/read/invalidation cycle [DONE]
- `uta/language/java/context_builder.py` — uses Java parse artifacts to build prompt context [DONE]
- Wire into `uta parse` command [TODO: partial - will wire in Step 10 pipeline]
- Validate: `pytest tests/test_cache.py -v` [DONE]

**Step 8 — OpenCode integration** [DONE]
- `uta/opencode/server.py` — start/stop opencode serve [DONE]
- `uta/opencode/client.py` — HTTP client (create session, send prompt, poll events) [DONE]
- `uta/opencode/config.py` — generate opencode.json [DONE]
- `tests/test_opencode_client.py` — mock HTTP responses, test session lifecycle [DONE]
- Validate: `pytest tests/test_opencode_client.py -v` [DONE]

**Step 9 — Test generation prompt** [DONE]
- `uta/prompts/generate_test.txt` — full prompt template with process flow context [DONE]
- `uta/prompts/fix_mutations.txt` — mutation enhancement prompt [DONE]
- `tests/test_prompt_render.py` — render prompt with fixture data, assert: [DONE]
  - Contains class source, process flow, dependency signatures [DONE]
  - Contains framework constraints (JUnit 4, Mockito 2.x — ArgumentMatchers API) [DONE]
  - Does NOT contain deprecated `org.mockito.Matchers` (Mockito 1.x API) [DONE]
- Validate: `pytest tests/test_prompt_render.py -v` [DONE]

**Step 10 — LangGraph pipeline** [DONE]
- `uta/graph/state.py` + `nodes.py` + `workflow.py` [DONE]
- Wire `generate_and_validate` node (merged gen+compile+fix loop) [DONE]
- `tests/test_pipeline_e2e.py` — end-to-end with one class (requires OpenCode + target repo) [TODO: as integration test]
- Validate: `pytest tests/test_workflow.py -v` [DONE]

**Step 11 — Mutation gate** [DONE]
- `uta/maven/jacoco.py` — coverage XML parsing [DONE]
- `uta/maven/pitest.py` — mutation XML parsing [DONE]
- Wire `mutation_gate` node into graph [TODO: will wire in Step 12 pipeline finalization]
- Add Jacoco/Pitest XML fixtures to `tests/fixtures/`, test parsing [DONE]
- Validate: `pytest tests/ -v -k "jacoco or pitest"` [DONE]

**Step 12 — Report storage + output** [DONE]
- `uta/reporting/reporter.py` — Rich terminal table + JSON report [DONE]
- Store reports in `{repo}/.uta_reports/` [DONE]
- Validate: run full pipeline, check `.uta_reports/` files, terminal output [DONE]

**Step 13 — Cross-repo validation** (SKELETON READY)
- [READY] Framework is repo-agnostic
- [READY] Cache and graph resolution handle various Maven structures
- [READY] CLI allows targetting repo/module

## Key Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Mockito 1→2 migration breaks existing tests | Baseline compile step auto-migrates imports (`Matchers` → `ArgumentMatchers`, runner package); committed to `unit-code-gen` branch before test generation starts |
| OpenCode server crashes mid-run | `try/finally` cleanup, health check before each request |
| Maven build too slow per class | Always `-pl {module}`, `-Dtest=ClassName`, consider `-o` (offline) |
| Patched pom.xml left behind | Use Maven CLI properties (`-D`) instead of pom patching where possible |
| SampleBizTemplate hard to test | Dedicated prompt section explaining the pattern + working example |

## Verification Plan

1. **Parse validation**: `uta parse --repo ~/wms/sample-outbound-core --module biz` — verify:
   - Tree-sitter extracts classes, methods, fields, annotations correctly
   - Call graph resolves cross-file calls
   - Process flows are detected (entry → handler → DB/RPC)
   - Cache files written to `.uta_cache/`, second run is fast (cache hit)

2. **Scan validation**: `uta scan --repo ~/wms/sample-outbound-core --days 30 --module biz` — verify file ranking

3. **Single class MVP**: pick a simple `@Service` class → `uta run --repo ~/wms/sample-outbound-core --module biz --max-files 1` → verify:
   - Test file written to correct path under `src/test/java/`
   - `mvn test -Dtest=...Test` passes
   - Jacoco reports >60% line coverage

4. **Cross-repo validation**: run same command on `~/platform/...` and `~/tms/...` repos — verify no hardcoded assumptions

5. **Complex class**: pick a large class (~1000+ lines) → verify fix-loop handles compilation errors

6. **Mutation gate**: run Pitest on generated test → verify mutation score
