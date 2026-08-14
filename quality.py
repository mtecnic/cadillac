"""LLM coding quality maximizers — standards, few-shot examples, anti-patterns, review prompts."""


CODING_STANDARDS = """\
## Coding Standards (apply to ALL generated code)
- Type hints on all public function signatures
- Docstrings on classes and non-trivial public functions
- Constants instead of magic numbers/strings
- Error handling at system boundaries (I/O, network, user input)
- No global mutable state — use classes or pass state explicitly
- Imports grouped: stdlib, third-party, local (separated by blank lines)
- Use pathlib or os.path consistently, not mixed
- Async code: never block the event loop with sync I/O
- Curses apps: use TERMINAL coordinates (rows ~24-50, cols ~80-200), NOT pixel coordinates (800x600). \
`addstr(row, col, text)` where row/col are character positions. Use `stdscr.getmaxyx()` to get terminal size."""


PROJECT_STRUCTURE = """\
## Project Structure Guidelines
Organize files based on project size and complexity:

**Small (1-7 files)**: Flat layout. All .py files in the project root.

**Medium (8-14 files)**: Use a package structure. Group related modules into directories.
Each directory MUST have an __init__.py that re-exports the public API.
```
main.py                   # entry point at root
config.py                 # shared config at root
models/
    __init__.py           # from .user import User
    user.py
    item.py
routes/
    __init__.py
    auth.py
    api.py
tests/
    test_models.py
    test_routes.py
```

Rules for 8+ file projects:
- Entry point (main.py) and config stay at the project root
- Group by domain: models/, routes/, services/, utils/, etc.
- Each package __init__.py re-exports its public API for clean imports
- Test files go in a tests/ directory
- Manifest paths MUST include directory prefix: "models/user.py", not "user.py"
- Imports use package paths: `from models import User`, `from routes.auth import login`

**Large (15+ files)**: Handled by modular pipeline (automatic)."""


ANTI_PATTERNS = """\
## NEVER Do These (common mistakes that waste rounds)
- NEVER name a file that shadows a Python stdlib module: io, os, sys, json, csv, re, time, typing, \
collections, abc, test, email, logging, http, socket, signal, queue, calendar, string, code, copy, \
numbers, types, operator, parser, token, stat, array, struct, random, secrets, platform, resource, \
inspect, warnings, hashlib, textwrap. Use descriptive names: file_io.py, csv_handler.py, \
app_logging.py, http_client.py, secure_random.py, app_types.py, cmd_parser.py
- NEVER use FTS5 content-sync tables in SQLite — they corrupt easily. Use simple LIKE queries instead
- NEVER start async servers without a shutdown mechanism reachable by --test (use asyncio.wait_for or signal handlers)
- NEVER start a ThreadingTCPServer without calling server.shutdown() from a SEPARATE thread in --test mode. \
Pattern: `threading.Thread(target=server.serve_forever).start()` then `server.shutdown()` to stop it cleanly
- NEVER start an Express/HTTP server in --test without calling `server.close()` and `process.exit(0)` after tests. \
Pattern: `const server = app.listen(port); /* run tests */ server.close(() => process.exit(0));`
- NEVER use a global singleton for DB connections (e.g. `db = Database()` at module level) — every CLI \
command entry point must create its own connection and close it in a finally block
- NEVER import from a file you just renamed without grep-updating ALL references across ALL files first
- NEVER use `asyncio.run()` inside Click commands without connecting the DB inside that async function
- NEVER assume the test framework (pytest) can import files that shadow stdlib modules
- NEVER dump 8+ source files in the project root with no directory structure — group related files \
into packages (models/, routes/, services/, utils/) with __init__.py files
- NEVER wrap module imports in try/except ImportError with inline class fallbacks. If `from rendering \
import Drawer` fails, that's a REAL BUG — fix the module, don't duplicate the class inline. Fallback \
blocks hide broken modules and make all tests pass against fake implementations."""


FEW_SHOT_SCAFFOLD = """\
## Example: Writing a well-structured file
When writing a file, produce COMPLETE, WORKING code with REAL LOGIC — not stubs. Example:

```
write_file("task_queue.py", '''\"\"\"Priority task queue with retry logic and dead-letter handling.\"\"\"

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from enum import Enum


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Task:
    \"\"\"A unit of work with priority and retry tracking.\"\"\"
    id: str
    payload: Dict
    priority: int = 0
    status: TaskStatus = TaskStatus.PENDING
    retries: int = 0
    max_retries: int = 3
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    @property
    def can_retry(self) -> bool:
        return self.retries < self.max_retries and self.status == TaskStatus.FAILED


class TaskQueue:
    \"\"\"Thread-safe priority queue with retry and dead-letter support.\"\"\"

    def __init__(self, max_size: int = 1000):
        self._tasks: List[Task] = []
        self._dead_letter: List[Task] = []
        self._max_size = max_size

    def enqueue(self, task: Task) -> bool:
        \"\"\"Add a task to the queue. Returns False if queue is full.\"\"\"
        if len(self._tasks) >= self._max_size:
            return False
        self._tasks.append(task)
        self._tasks.sort(key=lambda t: -t.priority)
        return True

    def dequeue(self) -> Optional[Task]:
        \"\"\"Get the highest-priority pending task.\"\"\"
        pending = [t for t in self._tasks if t.status == TaskStatus.PENDING]
        if not pending:
            return None
        task = pending[0]
        task.status = TaskStatus.RUNNING
        return task

    def complete(self, task_id: str) -> bool:
        \"\"\"Mark a task as done.\"\"\"
        task = self._find(task_id)
        if not task:
            return False
        task.status = TaskStatus.DONE
        return True

    def fail(self, task_id: str, error: str) -> None:
        \"\"\"Mark a task as failed. Retries if under limit, else dead-letters it.\"\"\"
        task = self._find(task_id)
        if not task:
            return
        task.error = error
        task.retries += 1
        if task.can_retry:
            task.status = TaskStatus.PENDING
        else:
            task.status = TaskStatus.FAILED
            self._tasks.remove(task)
            self._dead_letter.append(task)

    def _find(self, task_id: str) -> Optional[Task]:
        for t in self._tasks:
            if t.id == task_id:
                return t
        return None

    @property
    def pending_count(self) -> int:
        return sum(1 for t in self._tasks if t.status == TaskStatus.PENDING)

    @property
    def dead_letters(self) -> List[Task]:
        return list(self._dead_letter)
''')
```

Notice: enum for states, real retry logic, dead-letter queue, priority sorting, \
property accessors, type hints, docstrings. Every method has real logic — no stubs or pass."""


FEW_SHOT_MAIN_PY = """\
## Example: Correct --test flag implementation
Every main.py MUST follow this pattern for the --test flag:

```python
import sys
import click

@click.group(invoke_without_command=True)
@click.option('--test', is_flag=True, help='Run self-tests and exit')
def cli(test):
    if test:
        asyncio.run(run_tests())
        sys.exit(0)

async def run_tests():
    \"\"\"Self-test: create resources, verify, cleanup, exit.\"\"\"
    db = Database(":memory:")  # ALWAYS use in-memory DB for tests
    await db.connect()
    try:
        item = await db.create_item(name="test")
        assert item.id is not None, "Item should have an ID"
        items = await db.list_items()
        assert len(items) == 1, "Should have exactly 1 item"
        print("OK")
    finally:
        await db.close()  # ALWAYS close in finally block
```

Key rules: in-memory DB for tests, connect+close lifecycle, assert with messages, print "OK" on success."""


FEW_SHOT_DB_PATTERN = """\
## Example: Correct DB lifecycle in CLI commands
Every command that uses the DB MUST connect and close it:

```python
# CORRECT — each command manages its own DB lifecycle
@cli.command()
@click.argument('name')
def add_item(name):
    async def _run():
        db = Database()
        await db.connect()
        try:
            await db.create(name=name)
            print(f"Created: {{name}}")
        finally:
            await db.close()
    asyncio.run(_run())

# WRONG — global singleton, no connect/close in commands
db = Database()  # BAD: created at import time, never connected
@cli.command()
def add_item(name):
    asyncio.run(db.create(name=name))  # BAD: db.connect() never called
```

## Example: Click + aiosqlite async pattern
```python
# CORRECT — async context manager for aiosqlite, connection per command
import asyncio
import aiosqlite
import click

DB_PATH = "bookmarks.db"

async def get_db(path: str = DB_PATH):
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT)")
    await db.commit()
    return db

@click.group()
def cli():
    pass

@cli.command()
@click.argument("name")
def add(name):
    async def _run():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO items (name) VALUES (?)", (name,))
            await db.commit()
            click.echo(f"Added: {{name}}")
    asyncio.run(_run())

# --test entry point
if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        async def _test():
            async with aiosqlite.connect(":memory:") as db:
                await db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
                await db.execute("INSERT INTO items (name) VALUES (?)", ("test",))
                await db.commit()
                async with db.execute("SELECT * FROM items") as cur:
                    rows = await cur.fetchall()
                assert len(rows) == 1, f"Expected 1 row, got {{len(rows)}}"
            print("OK")
        asyncio.run(_test())
    else:
        cli()
```"""


FUNCTIONAL_TEST_GUIDANCE = """\
## Functional Test Requirements (CRITICAL)
Tests must exercise REAL USER FLOWS end-to-end, not just constructors.

### BAD tests (prove nothing):
```python
player = Player(5, 5)
assert player.x == 5          # Only proves __init__ works
enemy = Enemy("rat", 10, 10)
assert enemy.name == "rat"     # Only proves attributes stored
```

### GOOD tests (catch real bugs):
```python
# Test actual gameplay flow
dungeon = DungeonGenerator().generate_floor(1)
room = dungeon.rooms[0]
player = Player(room.center_x, room.center_y)
assert not dungeon.is_blocked(player.x, player.y), "Player must spawn on walkable tile"

# Test that movement actually works with the map
old_x = player.x
player.move(1, 0)  # move right
assert player.x == old_x + 1
assert not dungeon.is_blocked(player.x, player.y), "Player moved to blocked tile"

# Test combat integration (not just object creation)
enemy = Enemy("rat", player.x + 1, player.y)
result = combat_engine.resolve(player, enemy)
assert enemy.hp < enemy.max_hp, "Combat should deal damage"

# Test data flows ACROSS modules correctly
map_tiles = dungeon.tiles
assert isinstance(map_tiles[0][0], Tile), "Map tiles must be Tile objects (not bools)"
corridor_connect(map_tiles, room1, room2)
assert isinstance(map_tiles[5][5], Tile), "Corridor must NOT overwrite Tile objects"
```

Key principle: each test should create a scenario that exercises the DATA FLOW between 2+ modules. \
Constructor-only tests are worthless — they'll pass even if the actual functionality is completely broken."""


FEW_SHOT_NAMING = """\
## File naming: avoid stdlib conflicts
GOOD file names: database.py, csv_handler.py, file_io.py, app_logging.py, http_client.py, app_types.py
BAD file names (shadow stdlib): io.py, os.py, sys.py, json.py, csv.py, logging.py, test.py, email.py, \
http.py, socket.py, time.py, typing.py, collections.py, re.py, string.py, queue.py, signal.py, code.py"""


CRITIC_PROMPT = """\
You are an adversarial code critic. Your job is to FIND BUGS before the code runs. Assume the code has problems.

## Static checks — scan every file:
1. **Interface compliance**: Does the implementation match the architecture's signatures? Wrong arg names, \
missing return values, or different types all count.
2. **Import resolution**: For every `from X import Y`, does module X actually export Y? Check carefully.
3. **Runtime errors**: None access without guards, wrong types passed to functions, missing dict keys, \
index out of bounds.

## Cross-file type tracing — this catches the worst bugs:
4. **Data type contracts**: When file A passes data to file B, trace the ACTUAL TYPE through the call chain. \
Example: if Map stores tiles as `List[List[Tile]]` and passes them to a corridor function, does that function \
treat elements as Tile objects or as raw bools? Type mismatches across file boundaries are the #1 source of \
runtime crashes.
5. **Boolean/flag semantics**: When a boolean (like fog_map, is_visible, blocked) is set in one file and \
read in another, verify the semantics match. TRUE in file A must mean the SAME THING in file B. \
Inverted boolean logic is extremely common.

## Functional flow tracing — trace 3 key user actions:
6. **User flow trace**: Pick the 3 most important user-facing actions (e.g., "player moves", "user logs in", \
"item is added to cart"). For EACH action, trace the COMPLETE call chain across all files:
   - What function handles the input?
   - What does it call, in which files, with what arguments?
   - What types does each function return?
   - Does each caller correctly handle the return value?
   Report EVERY point where the chain breaks: wrong type, missing method, inverted logic, dead code path, \
   or unreachable branch.

7. **Test quality**: Are the --test checks actually testing real flows, or just constructors? \
Tests that only create objects and check attributes (e.g., `Player(5,5); assert x==5`) catch ZERO \
real bugs. Flag if tests don't exercise cross-module data flow.

If you need to verify details, use read_file to inspect specific functions. READ FILES before guessing.

Output your findings as a JSON array. Each finding MUST have these fields:
- "file": the file path
- "line": approximate line number or "~N"
- "severity": "high" (will crash), "medium" (wrong behavior), or "low" (minor issue)
- "issue": what's wrong (be specific — include the actual types or values involved)
- "fix": how to fix it (be specific — show the correct code or logic)

Example:
[{{"file": "mapgen/corridor.py", "line": "~12", "severity": "high", "issue": "create_h_corridor does tiles[y][x] = False but tiles is List[List[Tile]], overwriting Tile objects with bools. Later code calls tile.blocked which crashes on bool.", "fix": "Change to tiles[y][x].blocked = False"}}]

If no issues found, output: []

{architecture}

{code_map}"""


REVIEW_PROMPT = CRITIC_PROMPT  # Backward compat alias


ITERATE_PROMPT = """\
You are debugging an existing project. The CODE MAP below shows the full project structure \
with real line numbers. Use it as your primary reference — read_file only when you need to \
verify specific details not visible in the map.

RULES:
- Start with edit_file or write_file. Fix issues based on what you see in the code map.
- You may use read_file for focused verification (e.g. checking exact logic in a function), \
but prefer the code map for navigation and context.
- Fix ALL failing tests in one batch of edits when possible.
- After editing, run: python3 -m pytest -x --tb=short -q
- Once tests pass, run: python3 {entry_point} --test

DECIDING WHAT TO FIX — code vs test:
- Trace the test logic step by step against the actual code. If the test makes an impossible \
assertion (e.g. wrong comparison direction, calls a method that returns early, expects state \
that no code path produces), fix the TEST.
- If the code has a real bug (wrong formula, missing logic, off-by-one), fix the CODE.
- Common test bugs: wrong inequality direction (>= vs <=), not accounting for side effects in \
the method being tested, assuming state is reactive when it is a stored flag.
- Do NOT spend multiple paragraphs reasoning about a test. If after one trace you cannot see \
how the code could produce the expected result, the test is wrong — fix it.

CURRENT ISSUES:
{validation_failures}

{instruction}

{code_map}"""


ITERATE_FEATURE_PROMPT = """\
You are enhancing an existing project with new features. The CODE MAP below shows the full \
project structure with real line numbers.

WORKFLOW:
1. Study the code map to understand the current architecture.
2. Read files when the code map doesn't show enough detail for safe editing.
3. Plan your changes — identify which files need modification and what to add.
4. Implement changes using edit_file or write_file. Make related changes across multiple files in one round.
5. After editing, run: python3 {entry_point} --test
6. Then run: python3 -m pytest -x --tb=short -q
7. Iterate until all tests pass with the new feature working.

RULES:
- Read files freely to understand the codebase — you need full context for feature work.
- Make multi-file edits in a single round when the changes are related.
- Add or update tests to cover new functionality.
- Keep ALL existing tests passing — do not break what works.
- Write real implementations with full logic, not stubs.

CURRENT ISSUES:
{validation_failures}

{instruction}

{code_map}"""


# ── TypeScript / JavaScript equivalents ───────────────────────────────────────

TS_CODING_STANDARDS = """\
## Coding Standards (apply to ALL generated code)
- TypeScript strict mode: tsconfig.json with "strict": true, "module": "commonjs", "esModuleInterop": true
- CRITICAL: Do NOT use "type": "module" in package.json — use CommonJS module resolution
- Use ES-style imports (import/export) but compile to CommonJS via tsconfig
- Use interfaces for data shapes, type aliases for unions/intersections
- Use async/await for all async code, never raw .then() chains
- Use const by default, let when rebinding is needed, never var
- Error handling with try/catch at system boundaries (I/O, network, user input)
- No global mutable state — use classes or pass state explicitly
- Use template literals for string interpolation
- No `any` type — use proper typing or `unknown` + type guards
- DO NOT add .js extensions to import paths — use bare paths (e.g., import { Foo } from './foo')"""

TS_PROJECT_STRUCTURE = """\
## Project Structure Guidelines
Organize files based on project size:

**Small (1-7 files)**: Flat src/ layout. All .ts files in src/.

**Medium (8-14 files)**: Group by domain.
```
package.json
tsconfig.json
src/
    index.ts              # entry point
    config.ts             # shared config
    models/
        index.ts          # re-exports: export { User } from './user'
        user.ts
        item.ts
    routes/
        index.ts
        auth.ts
        api.ts
    __tests__/
        models.test.ts
        routes.test.ts
```

Rules for 8+ file projects:
- Entry point (index.ts) at src/ root
- Group by domain: models/, routes/, services/, utils/, etc.
- Each directory has index.ts that re-exports public API
- Test files use .test.ts suffix in __tests__/ or colocated
- DO NOT use .js extensions in import paths — let tsc resolve them

**Critical setup files** that MUST be generated:
- tsconfig.json: "strict": true, "module": "commonjs", "target": "es2020", "esModuleInterop": true, "outDir": "./dist", "rootDir": "./src"
- jest.config.js: module.exports = { preset: 'ts-jest', testEnvironment: 'node' }
- package.json scripts: "test": "jest --passWithNoTests", "start": "npx ts-node src/index.ts"
- DO NOT set "type": "module" in package.json — let TypeScript compile to CommonJS

**Large (15+ files)**: Handled by modular pipeline (automatic)."""

TS_ANTI_PATTERNS = """\
## NEVER Do These (common TS/JS mistakes that waste rounds)
- NEVER use `any` type — it defeats the purpose of TypeScript. Use `unknown` with type guards.
- NEVER put "type": "module" in package.json — it breaks ts-node, Jest, and many tools. Use tsconfig "module": "commonjs" instead.
- NEVER add .js extensions to import paths in TypeScript files — use bare paths like './foo' not './foo.js'
- NEVER put node_modules in version control or generated output
- NEVER use synchronous fs methods (readFileSync) in server request handlers
- NEVER shadow Node.js built-in modules (don't name files: fs.ts, path.ts, http.ts, crypto.ts)
- NEVER use var — use const (default) or let
- NEVER start HTTP servers without a shutdown mechanism reachable by --test
- NEVER import from a file you just renamed without updating ALL references
- NEVER use `as` type assertions to hide type errors — fix the underlying type
- NEVER leave unhandled promise rejections — use try/catch in async functions
- NEVER forget jest.config.js with preset: 'ts-jest' — Jest cannot parse TypeScript without it
- NEVER modify tsconfig.json to change "module" from "commonjs" — the boilerplate is correct, do not touch it
- NEVER set "rootDir" in tsconfig.json — it breaks compilation when test files are outside the root dir
- NEVER start an Express server in --test without shutting it down cleanly: \
`const server = app.listen(port, () => {{ runTests().then(() => server.close(() => process.exit(0))) }});`"""

TS_FEW_SHOT_SCAFFOLD = """\
## Example: Writing a well-structured TypeScript file
When writing a file, produce COMPLETE, WORKING code with REAL LOGIC — not stubs. Example:

```
write_file("src/task-queue.ts", `/**
 * Priority task queue with retry logic and dead-letter handling.
 */

export enum TaskStatus {
    PENDING = "pending",
    RUNNING = "running",
    DONE = "done",
    FAILED = "failed",
}

export interface TaskOptions {
    priority?: number;
    maxRetries?: number;
}

export class Task {
    readonly id: string;
    readonly payload: Record<string, unknown>;
    readonly priority: number;
    readonly maxRetries: number;
    status: TaskStatus = TaskStatus.PENDING;
    retries: number = 0;
    error?: string;
    readonly createdAt: number = Date.now();

    constructor(id: string, payload: Record<string, unknown>, opts: TaskOptions = {}) {
        this.id = id;
        this.payload = payload;
        this.priority = opts.priority ?? 0;
        this.maxRetries = opts.maxRetries ?? 3;
    }

    get canRetry(): boolean {
        return this.retries < this.maxRetries && this.status === TaskStatus.FAILED;
    }
}

export class TaskQueue {
    private tasks: Task[] = [];
    private deadLetter: Task[] = [];
    private readonly maxSize: number;

    constructor(maxSize: number = 1000) {
        this.maxSize = maxSize;
    }

    enqueue(task: Task): boolean {
        if (this.tasks.length >= this.maxSize) return false;
        this.tasks.push(task);
        this.tasks.sort((a, b) => b.priority - a.priority);
        return true;
    }

    dequeue(): Task | undefined {
        const task = this.tasks.find(t => t.status === TaskStatus.PENDING);
        if (task) task.status = TaskStatus.RUNNING;
        return task;
    }

    complete(taskId: string): boolean {
        const task = this.tasks.find(t => t.id === taskId);
        if (!task) return false;
        task.status = TaskStatus.DONE;
        return true;
    }

    fail(taskId: string, error: string): void {
        const task = this.tasks.find(t => t.id === taskId);
        if (!task) return;
        task.error = error;
        task.retries++;
        if (task.canRetry) {
            task.status = TaskStatus.PENDING;
        } else {
            task.status = TaskStatus.FAILED;
            this.tasks = this.tasks.filter(t => t.id !== taskId);
            this.deadLetter.push(task);
        }
    }

    get pendingCount(): number {
        return this.tasks.filter(t => t.status === TaskStatus.PENDING).length;
    }

    get deadLetters(): readonly Task[] {
        return [...this.deadLetter];
    }
}
`)
```

Notice: enum for states, real retry logic, dead-letter queue, priority sorting, \
getters, proper typing, JSDoc comments. Every method has real logic — no stubs."""

TS_FEW_SHOT_MAIN = """\
## Example: Correct --test flag implementation (TypeScript)
Every index.ts MUST follow this pattern for the --test flag:

```typescript
const args = process.argv.slice(2);

async function runTests(): Promise<void> {
    // Use in-memory or temp storage for tests
    const db = new Database(":memory:");
    await db.connect();
    try {
        const item = await db.createItem({ name: "test" });
        console.assert(item.id !== undefined, "Item should have an ID");
        const items = await db.listItems();
        console.assert(items.length === 1, "Should have exactly 1 item");
        console.log("OK");
    } finally {
        await db.close();
    }
}

async function main(): Promise<void> {
    if (args.includes("--test")) {
        await runTests();
        process.exit(0);
    }
    // Normal app startup...
}

main().catch(err => {
    console.error(err);
    process.exit(1);
});
```

Key rules: in-memory DB for tests, connect+close lifecycle, console.assert with messages, print "OK" on success."""

TS_FEW_SHOT_DB_PATTERN = """\
## Example: Correct DB lifecycle in route handlers (TypeScript)
Every handler that uses the DB MUST manage its lifecycle:

```typescript
// CORRECT — each handler manages DB lifecycle
app.post("/items", async (req, res) => {
    const db = new Database();
    await db.connect();
    try {
        const item = await db.create(req.body);
        res.json(item);
    } finally {
        await db.close();
    }
});

// WRONG — global singleton, no connect/close
const db = new Database();  // BAD: never connected
app.post("/items", async (req, res) => {
    const item = await db.create(req.body);  // BAD: db not connected
    res.json(item);
});
```"""

TS_FUNCTIONAL_TEST_GUIDANCE = """\
## Functional Test Requirements (CRITICAL)
Tests must exercise REAL USER FLOWS end-to-end, not just constructors.

### BAD tests (prove nothing):
```typescript
const player = new Player(5, 5);
expect(player.x).toBe(5);           // Only proves constructor works
const enemy = new Enemy("rat", 10, 10);
expect(enemy.name).toBe("rat");     // Only proves attributes stored
```

### GOOD tests (catch real bugs):
```typescript
// Test actual gameplay flow
const dungeon = new DungeonGenerator().generateFloor(1);
const room = dungeon.rooms[0];
const player = new Player(room.centerX, room.centerY);
expect(dungeon.isBlocked(player.x, player.y)).toBe(false); // Player on walkable tile

// Test that movement actually works with the map
const oldX = player.x;
player.move(1, 0);
expect(player.x).toBe(oldX + 1);
expect(dungeon.isBlocked(player.x, player.y)).toBe(false);

// Test data flows ACROSS modules correctly
const tiles = dungeon.tiles;
expect(tiles[0][0]).toBeInstanceOf(Tile); // Map tiles must be Tile objects
```

Key principle: each test should exercise DATA FLOW between 2+ modules."""

TS_FEW_SHOT_NAMING = """\
## File naming: avoid Node.js built-in conflicts
GOOD file names: database.ts, csv-handler.ts, file-io.ts, app-logger.ts, http-client.ts, app-types.ts
BAD file names (shadow builtins): fs.ts, path.ts, os.ts, http.ts, https.ts, crypto.ts, stream.ts, \
events.ts, buffer.ts, net.ts, url.ts, util.ts"""

TS_ITERATE_PROMPT = """\
You are debugging an existing TypeScript project. The CODE MAP below shows the full project structure \
with real line numbers. Use it as your primary reference — read_file only when you need to \
verify specific details not visible in the map.

RULES:
- Start with edit_file or write_file. Fix issues based on what you see in the code map.
- You may use read_file for focused verification, but prefer the code map for navigation and context.
- Fix ALL failing tests in one batch of edits when possible.
- After editing, run: npx jest --passWithNoTests
- Once tests pass, run: npx ts-node {entry_point} --test

DECIDING WHAT TO FIX — code vs test:
- Trace the test logic step by step against the actual code. If the test makes an impossible \
assertion, fix the TEST.
- If the code has a real bug (wrong formula, missing logic, off-by-one), fix the CODE.
- Do NOT spend multiple paragraphs reasoning about a test. Fix it after one trace.

CURRENT ISSUES:
{validation_failures}

{instruction}

{code_map}"""

TS_ITERATE_FEATURE_PROMPT = """\
You are enhancing an existing TypeScript project with new features. The CODE MAP below shows the full \
project structure with real line numbers.

WORKFLOW:
1. Study the code map to understand the current architecture.
2. Read files when the code map doesn't show enough detail.
3. Plan your changes — identify which files need modification.
4. Implement changes using edit_file or write_file.
5. After editing, run: npx ts-node {entry_point} --test
6. Then run: npx jest --passWithNoTests
7. Iterate until all tests pass.

RULES:
- Read files freely to understand the codebase.
- Make multi-file edits in a single round when related.
- Add or update tests to cover new functionality.
- Keep ALL existing tests passing.
- Write real implementations with full logic, not stubs.

CURRENT ISSUES:
{validation_failures}

{instruction}

{code_map}"""

_FEATURE_KEYWORDS = {"add", "implement", "improve", "enhance", "support", "new", "create", "build", "extend", "upgrade"}
_DEBUG_KEYWORDS = {"fix", "bug", "error", "broken", "failing", "crash", "traceback", "exception"}


def _is_feature_instruction(instruction: str, has_failures: bool) -> bool:
    """Detect whether an iterate instruction is feature work vs debugging."""
    if not instruction:
        return False
    words = set(instruction.lower().split())
    feature_hits = len(words & _FEATURE_KEYWORDS)
    debug_hits = len(words & _DEBUG_KEYWORDS)
    if feature_hits > debug_hits:
        return True
    if feature_hits == debug_hits and not has_failures:
        return True  # No failures + ambiguous = feature work
    return False


DEBUG_PROMPT = """\
You are debugging a specific issue in an existing project.

TARGET ISSUE:
{target_failure}

TOOLS:
- write_file(path, content): Write a complete file.
- edit_file(path, edits): Surgical find-and-replace.
- line_edit(path, start_line, end_line, new_content): Replace lines by number.
- read_file(path, line_start?, line_end?): Read a file.
- run_command(command): Run shell commands.
- list_files(path): List files.
- search_files(pattern, file_glob?): Grep for patterns.

WORKFLOW:
1. Read relevant files to understand the bug
2. Fix with surgical edit_file calls
3. Run: {run_cmd} {entry_point} --test
4. Run: {test_cmd}

RULES:
- Fix the ROOT CAUSE, not symptoms
- Use surgical edits — do not rewrite entire files
- Keep ALL other tests passing

{manifest_summary}"""


def build_review_prompt(
    entry_point: str = "main.py",
    manifest_summary: str = "",
    architecture: str = "",
    code_map: str = "",
    lang=None,
) -> str:
    return REVIEW_PROMPT.format(
        entry_point=entry_point,
        manifest_summary=manifest_summary,
        architecture=architecture,
        code_map=code_map,
    )


def build_iterate_prompt(
    entry_point: str = "main.py",
    manifest_summary: str = "",
    validation_failures: str = "",
    instruction: str = "",
    code_map: str = "",
    lang=None,
) -> str:
    instruction_block = f"ADDITIONAL INSTRUCTIONS:\n{instruction}" if instruction else ""
    has_failures = bool(validation_failures and validation_failures.strip())

    if _is_feature_instruction(instruction, has_failures):
        template = lang.iterate_feature_prompt if lang and lang.family != "python" else ITERATE_FEATURE_PROMPT
    else:
        template = lang.iterate_prompt if lang and lang.family != "python" else ITERATE_PROMPT

    return template.format(
        entry_point=entry_point,
        code_map=code_map or manifest_summary,
        validation_failures=validation_failures,
        instruction=instruction_block,
    )


def build_debug_prompt(
    entry_point: str = "main.py",
    manifest_summary: str = "",
    target_failure: str = "",
    lang=None,
) -> str:
    if lang and lang.family != "python":
        run_cmd = lang.run_cmd
        test_cmd = " ".join(lang.test_cmd)
    else:
        run_cmd = "python3"
        test_cmd = "python3 -m pytest -x --tb=short -q"
    return DEBUG_PROMPT.format(
        entry_point=entry_point,
        manifest_summary=manifest_summary,
        target_failure=target_failure,
        run_cmd=run_cmd,
        test_cmd=test_cmd,
    )


# ── HTML/CSS/JS quality constants ────────────────────────────────────────────

HTML_CODING_STANDARDS = """\
## Coding Standards (apply to ALL generated code)
- Semantic HTML5 elements (<header>, <nav>, <main>, <section>, <article>, <footer>)
- CSS: use CSS custom properties (variables) for colors, spacing, fonts
- JavaScript: use const/let (never var), strict equality (===), arrow functions
- All interactive elements must be accessible (aria labels, keyboard navigation)
- No inline styles — all styling in .css files
- No inline scripts — all JavaScript in .js files
- Use data attributes for JS hooks, not classes (data-action="add-to-cart")
- Event delegation for dynamic content
- Form validation on both client side (UX) and structure (required attributes)
- Responsive design with mobile-first media queries"""

HTML_PROJECT_STRUCTURE = """\
## Project Structure Guidelines
Organize files for a static HTML/CSS/JS site:

**Small (1-5 pages)**: Flat layout with shared CSS/JS.
```
index.html            # main page
about.html            # about page
css/
    style.css         # shared styles
    components.css    # component-specific styles
js/
    app.js            # main application logic
    utils.js          # shared utility functions
images/               # static assets
```

**Medium (6+ pages)**: Group by feature.
```
index.html
pages/
    products.html
    cart.html
    checkout.html
css/
    style.css
    products.css
    cart.css
js/
    app.js
    products.js
    cart.js
    utils.js
images/
```

CRITICAL: entry_point is ALWAYS "index.html" at the project root."""

HTML_ANTI_PATTERNS = """\
## Anti-patterns to AVOID
- Do NOT use any build tools (webpack, vite, parcel, etc.)
- Do NOT use npm, node_modules, or package.json
- Do NOT use TypeScript — write plain .js files only
- Do NOT use JSX or any templating that requires compilation
- Do NOT use ES module import/export syntax in browser scripts (use <script src="..."> tags)
- Do NOT use require() — this is for Node.js, not browsers
- Do NOT put CSS in JavaScript or JavaScript in HTML
- Do NOT use frameworks (React, Vue, Angular, Svelte, etc.)
- Do NOT use CSS preprocessors (Sass, Less, etc.)
- Never use document.write()
- Avoid excessive DOM manipulation in loops — batch operations"""

HTML_FEW_SHOT_SCAFFOLD = """\
### Example scaffold for a web application:
```
write_file(path="index.html", content="<!DOCTYPE html>\\n<html lang=\\"en\\">\\n<head>\\n  <meta charset=\\"UTF-8\\">\\n  <meta name=\\"viewport\\" content=\\"width=device-width, initial-scale=1.0\\">\\n  <title>App</title>\\n  <link rel=\\"stylesheet\\" href=\\"css/style.css\\">\\n</head>\\n<body>\\n  <header>...</header>\\n  <main id=\\"app\\">...</main>\\n  <footer>...</footer>\\n  <script src=\\"js/app.js\\"></script>\\n</body>\\n</html>")
write_file(path="css/style.css", content=":root {\\n  --primary: #2563eb;\\n  --bg: #f8fafc;\\n  --text: #1e293b;\\n}\\n\\n* { margin: 0; padding: 0; box-sizing: border-box; }\\nbody { font-family: system-ui, sans-serif; background: var(--bg); color: var(--text); }")
write_file(path="js/app.js", content="\\"use strict\\";\\n\\ndocument.addEventListener(\\"DOMContentLoaded\\", () => {\\n  // App initialization\\n});")
```"""

HTML_FEW_SHOT_MAIN = """\
### Example index.html:
```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>My App</title>
  <link rel="stylesheet" href="css/style.css">
</head>
<body>
  <header>
    <nav>
      <a href="index.html">Home</a>
      <a href="pages/products.html">Products</a>
    </nav>
  </header>
  <main id="app">
    <h1>Welcome</h1>
  </main>
  <footer>
    <p>&copy; 2024 My App</p>
  </footer>
  <script src="js/app.js"></script>
</body>
</html>
```"""

HTML_FUNCTIONAL_TEST_GUIDANCE = """\
## Test File (test.js — runs with Node.js, NOT in browser)
The test file validates data structures and logic functions that don't depend on DOM.
It runs via `node test.js` and must exit 0 on success, non-zero on failure.

```javascript
"use strict";

// Test utility
let passed = 0, failed = 0;
function assert(condition, msg) {
  if (condition) { passed++; console.log(`✓ ${msg}`); }
  else { failed++; console.error(`✗ ${msg}`); }
}

// Example tests
assert(typeof formatPrice === "function", "formatPrice exists");
assert(formatPrice(1999) === "$19.99", "formatPrice formats cents");

// Summary
console.log(`\\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
```

IMPORTANT: The test file must be self-contained. Use require() or inline the functions to test.
DOM-dependent code cannot be tested this way — test pure logic only."""

HTML_FEW_SHOT_NAMING = """\
### File naming conventions:
- HTML pages: lowercase, hyphens (product-detail.html, shopping-cart.html)
- CSS files: lowercase, hyphens (main-style.css, cart-page.css)
- JS files: lowercase, hyphens or camelCase (app.js, cartManager.js, utils.js)
- Image assets: lowercase, hyphens (hero-banner.jpg, logo.png)
- Test file: test.js at project root"""

HTML_ITERATE_PROMPT = """\
Fix the issues described. Rules:
- Only modify files needed to fix the error
- Keep all HTML semantic and accessible
- Keep all JS in .js files, all CSS in .css files
- Do NOT introduce any build tools, npm, or frameworks
- Run `node test.js` to verify after changes"""

HTML_ITERATE_FEATURE_PROMPT = """\
Implement the requested feature. Rules:
- Keep the existing HTML/CSS/JS architecture
- Do NOT introduce any build tools, npm, or frameworks
- Add new pages/styles/scripts as separate files
- Update navigation links if adding new pages
- Run `node test.js` to verify after changes"""


# ── React quality constants ──────────────────────────────────────────────────

REACT_CODING_STANDARDS = """\
## Coding Standards (apply to ALL generated code)
- TypeScript strict mode: tsconfig.json with "strict": true
- Functional components only — NO class components
- Use React hooks: useState, useEffect, useCallback, useMemo, useRef, useContext
- Custom hooks for shared logic (useFetch, useLocalStorage, etc.)
- Props interfaces on every component: interface FooProps { ... }
- Use template literals for string interpolation
- const by default, let when rebinding needed, never var
- No `any` type — use proper typing or `unknown` + type guards
- Error handling with try/catch at async boundaries
- CSS Modules or scoped styles — no global CSS leaking between components"""

REACT_PROJECT_STRUCTURE = """\
## Project Structure Guidelines (Vite + React + TypeScript)

**Small (1-7 components)**:
```
package.json
tsconfig.json
vite.config.ts
index.html                 # Vite entry HTML
src/
    main.tsx               # ReactDOM.createRoot entry
    App.tsx                # Root component
    App.css
    components/
        Header.tsx
        Footer.tsx
    types.ts               # Shared types
```

**Medium (8+ components)**:
```
src/
    main.tsx
    App.tsx
    components/
        ui/                # Reusable UI primitives
            Button.tsx
            Input.tsx
        layout/
            Header.tsx
            Sidebar.tsx
    hooks/
        useFetch.ts
        useAuth.ts
    pages/
        Home.tsx
        Dashboard.tsx
    services/
        api.ts
    types/
        index.ts
    __tests__/
        App.test.tsx
```

**Critical setup files** that MUST be generated:
- vite.config.ts: `import react from '@vitejs/plugin-react'; export default defineConfig({ plugins: [react()] })`
- tsconfig.json: "strict": true, "jsx": "react-jsx", "module": "ESNext", "moduleResolution": "bundler"
- index.html: `<div id="root"></div>` + `<script type="module" src="/src/main.tsx"></script>`

**Large (15+ files)**: Handled by modular pipeline (automatic)."""

REACT_ANTI_PATTERNS = """\
## NEVER Do These (common React mistakes)
- NEVER use class components — functional components with hooks only
- NEVER mutate state directly — use setter from useState or produce new objects
- NEVER use `any` type — defeats TypeScript's purpose
- NEVER manipulate DOM directly (document.getElementById) — use refs
- NEVER forget key props on mapped lists: `items.map(i => <Item key={i.id} />)`
- NEVER call hooks inside conditionals, loops, or nested functions
- NEVER use index as key for dynamic lists
- NEVER use useEffect for derived state — compute during render instead
- NEVER create components inside other components — define at module level
- NEVER leave async operations without cleanup in useEffect
- NEVER use inline function definitions in JSX for expensive operations — use useCallback"""

REACT_FEW_SHOT_SCAFFOLD = """\
## Example: Writing a React component
```
write_file("src/components/TaskList.tsx", `import { useState, useCallback } from 'react';

interface Task {
    id: string;
    title: string;
    completed: boolean;
}

interface TaskListProps {
    initialTasks?: Task[];
    onTaskComplete?: (id: string) => void;
}

export function TaskList({ initialTasks = [], onTaskComplete }: TaskListProps) {
    const [tasks, setTasks] = useState<Task[]>(initialTasks);
    const [newTitle, setNewTitle] = useState('');

    const addTask = useCallback(() => {
        if (!newTitle.trim()) return;
        setTasks(prev => [...prev, {
            id: crypto.randomUUID(),
            title: newTitle.trim(),
            completed: false,
        }]);
        setNewTitle('');
    }, [newTitle]);

    const toggleTask = useCallback((id: string) => {
        setTasks(prev => prev.map(t =>
            t.id === id ? { ...t, completed: !t.completed } : t
        ));
        onTaskComplete?.(id);
    }, [onTaskComplete]);

    return (
        <div>
            <div>
                <input value={newTitle} onChange={e => setNewTitle(e.target.value)} placeholder="New task" />
                <button onClick={addTask}>Add</button>
            </div>
            <ul>
                {tasks.map(task => (
                    <li key={task.id} style={{ textDecoration: task.completed ? 'line-through' : 'none' }}>
                        <label>
                            <input type="checkbox" checked={task.completed} onChange={() => toggleTask(task.id)} />
                            {task.title}
                        </label>
                    </li>
                ))}
            </ul>
        </div>
    );
}
`)
```

Notice: typed props interface, hooks for state, useCallback for handlers, proper key on list, \
immutable state updates. Every handler has real logic — no stubs."""

REACT_FEW_SHOT_MAIN = """\
## Example: Correct main.tsx and App.tsx
```tsx
// src/main.tsx
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';

createRoot(document.getElementById('root')!).render(
    <StrictMode>
        <App />
    </StrictMode>
);

// src/App.tsx
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { Home } from './pages/Home';
import { Dashboard } from './pages/Dashboard';

export default function App() {
    return (
        <BrowserRouter>
            <Routes>
                <Route path="/" element={<Home />} />
                <Route path="/dashboard" element={<Dashboard />} />
            </Routes>
        </BrowserRouter>
    );
}
```"""

REACT_FEW_SHOT_DB_PATTERN = """\
## Example: API service pattern for React
```typescript
// src/services/api.ts
const API_BASE = '/api';

export async function fetchItems(): Promise<Item[]> {
    const res = await fetch(`${API_BASE}/items`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
}

// src/hooks/useItems.ts
import { useState, useEffect } from 'react';
import { fetchItems } from '../services/api';

export function useItems() {
    const [items, setItems] = useState<Item[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<Error | null>(null);

    useEffect(() => {
        let cancelled = false;
        fetchItems()
            .then(data => { if (!cancelled) setItems(data); })
            .catch(err => { if (!cancelled) setError(err); })
            .finally(() => { if (!cancelled) setLoading(false); });
        return () => { cancelled = true; };
    }, []);

    return { items, loading, error };
}
```"""

REACT_FUNCTIONAL_TEST_GUIDANCE = """\
## Functional Test Requirements (Vitest + React Testing Library)
Tests must exercise real component behavior, not just rendering.

### BAD tests:
```tsx
render(<App />);
expect(screen.getByText('Home')).toBeDefined();  // Only proves it renders

// BAD: getByText with text that appears multiple times throws "Found multiple elements"
expect(screen.getByText(/\\$35/i)).toBeDefined();

// BAD: HTMLElement.value is not typed — TypeScript error
const input = screen.getByLabelText('Email');
expect(input.value).toBe('a@b.com');
```

### GOOD tests:
```tsx
import { render, screen, fireEvent, within } from '@testing-library/react';
import { TaskList } from '../components/TaskList';

test('adds and completes a task', () => {
    render(<TaskList />);
    const input = screen.getByPlaceholderText('New task');
    fireEvent.change(input, { target: { value: 'Buy milk' } });
    fireEvent.click(screen.getByRole('button', { name: /add/i }));
    expect(screen.getByText('Buy milk')).toBeDefined();

    const checkbox = screen.getByRole('checkbox');
    fireEvent.click(checkbox);
    expect(checkbox).toBeChecked();
});

// GOOD: scope queries with within() when text repeats across sections
test('shows price in services section', () => {
    render(<App />);
    const services = screen.getByRole('region', { name: /services/i });
    expect(within(services).getByText(/\\$35/i)).toBeDefined();
});

// GOOD: getAllByText when intentionally matching multiple elements
test('renders all 6 service cards', () => {
    render(<App />);
    expect(screen.getAllByText(/\\$/).length).toBeGreaterThanOrEqual(6);
});

// GOOD: cast HTMLElement to HTMLInputElement for .value access
const input = screen.getByLabelText('Email') as HTMLInputElement;
expect(input.value).toBe('a@b.com');

// GOOD: use toHaveValue() to avoid the cast entirely
expect(screen.getByLabelText('Email')).toHaveValue('a@b.com');
```

### Query priority (use the FIRST that works):
1. `getByRole(role, { name })` — best for accessibility + uniqueness
2. `getByLabelText` — for form fields
3. `getByPlaceholderText` — for inputs without labels
4. `getByText` — last resort for unique text
5. `getByTestId` — when nothing else works (add `data-testid` to JSX)

### Mocking external deps:
```tsx
import { vi } from 'vitest';
vi.mock('../services/api', () => ({
    fetchItems: vi.fn().mockResolvedValue([{ id: 1, name: 'test' }]),
}));
```

Test real user interactions: click, type, submit, navigate. NEVER use plain `getByText` for prices, numbers, or any text that may appear multiple times."""

REACT_FEW_SHOT_NAMING = """\
## File naming conventions:
- Components: PascalCase (TaskList.tsx, UserProfile.tsx)
- Hooks: camelCase with 'use' prefix (useAuth.ts, useFetch.ts)
- Services/utils: camelCase (api.ts, formatDate.ts)
- Types: camelCase or PascalCase (types.ts, UserTypes.ts)
- Tests: ComponentName.test.tsx
- Styles: ComponentName.module.css or ComponentName.css"""

REACT_ITERATE_PROMPT = """\
You are debugging an existing React + TypeScript project. The CODE MAP shows the full structure.

RULES:
- Fix issues using edit_file or write_file
- After editing, run: npx vitest run
- Then run: npx vite build (must compile cleanly)

DECIDING WHAT TO FIX:
- Trace the component tree and data flow
- Check hooks are called correctly (not in conditionals)
- Check prop types match between parent and child

CURRENT ISSUES:
{validation_failures}

{instruction}

{code_map}"""

REACT_ITERATE_FEATURE_PROMPT = """\
You are adding features to a React + TypeScript project.

WORKFLOW:
1. Study the code map
2. Plan your changes
3. Implement with edit_file or write_file
4. Run: npx vitest run
5. Run: npx vite build
6. Iterate until all tests pass

RULES:
- Use functional components with hooks
- Add tests for new components
- Keep existing tests passing

CURRENT ISSUES:
{validation_failures}

{instruction}

{code_map}"""


# ── Vue quality constants ────────────────────────────────────────────────────

VUE_CODING_STANDARDS = """\
## Coding Standards (apply to ALL generated code)
- Vue 3 Composition API with <script setup lang="ts"> — NO Options API
- TypeScript strict mode in tsconfig.json
- defineProps<T>() and defineEmits<T>() for type-safe component interfaces
- Use ref() and reactive() for state, computed() for derived values
- Use composables (use*.ts) for shared logic
- const by default, let when rebinding needed, never var
- No `any` type — use proper typing
- Error handling with try/catch at async boundaries
- Scoped styles with <style scoped> — no global CSS leaking"""

VUE_PROJECT_STRUCTURE = """\
## Project Structure (Vite + Vue 3 + TypeScript)

```
package.json
tsconfig.json
vite.config.ts
index.html
src/
    main.ts                # createApp entry
    App.vue                # Root component
    components/
        Header.vue
        Footer.vue
    composables/
        useFetch.ts
        useAuth.ts
    pages/
        Home.vue
        Dashboard.vue
    services/
        api.ts
    types/
        index.ts
    __tests__/
        App.test.ts
```

**Critical setup files**:
- vite.config.ts: `import vue from '@vitejs/plugin-vue'; export default defineConfig({ plugins: [vue()] })`
- tsconfig.json: "strict": true, "jsx": "preserve", "module": "ESNext", "moduleResolution": "bundler"
- index.html: `<div id="app"></div>` + `<script type="module" src="/src/main.ts"></script>`"""

VUE_ANTI_PATTERNS = """\
## NEVER Do These (common Vue mistakes)
- NEVER use Options API — use Composition API with <script setup> only
- NEVER mutate props — emit events to parent instead
- NEVER use this.$refs for data flow — use props/emits/provide/inject
- NEVER use `any` type
- NEVER forget key on v-for: `<div v-for="item in items" :key="item.id">`
- NEVER modify reactive objects from outside the component that owns them
- NEVER use v-html with user input (XSS risk)
- NEVER create watchers for derived state — use computed() instead"""

VUE_FEW_SHOT_SCAFFOLD = """\
## Example: Writing a Vue component
```
write_file("src/components/TaskList.vue", `<script setup lang="ts">
import { ref, computed } from 'vue';

interface Task {
    id: string;
    title: string;
    completed: boolean;
}

const props = defineProps<{ initialTasks?: Task[] }>();
const emit = defineEmits<{ complete: [id: string] }>();

const tasks = ref<Task[]>(props.initialTasks ?? []);
const newTitle = ref('');
const pendingCount = computed(() => tasks.value.filter(t => !t.completed).length);

function addTask() {
    if (!newTitle.value.trim()) return;
    tasks.value.push({ id: crypto.randomUUID(), title: newTitle.value.trim(), completed: false });
    newTitle.value = '';
}

function toggleTask(id: string) {
    const task = tasks.value.find(t => t.id === id);
    if (task) { task.completed = !task.completed; emit('complete', id); }
}
</script>

<template>
    <div>
        <input v-model="newTitle" placeholder="New task" @keyup.enter="addTask" />
        <button @click="addTask">Add</button>
        <p>{{ pendingCount }} pending</p>
        <ul>
            <li v-for="task in tasks" :key="task.id">
                <input type="checkbox" :checked="task.completed" @change="toggleTask(task.id)" />
                {{ task.title }}
            </li>
        </ul>
    </div>
</template>

<style scoped>
li { list-style: none; }
</style>
`)
```"""

VUE_FEW_SHOT_MAIN = """\
## Example: Correct main.ts and App.vue
```typescript
// src/main.ts
import { createApp } from 'vue';
import App from './App.vue';
createApp(App).mount('#app');
```
```vue
<!-- src/App.vue -->
<script setup lang="ts">
import { RouterView } from 'vue-router';
</script>
<template>
    <RouterView />
</template>
```"""

VUE_FEW_SHOT_DB_PATTERN = """\
## Example: Composable for API data
```typescript
// src/composables/useItems.ts
import { ref, onMounted } from 'vue';

export function useItems() {
    const items = ref<Item[]>([]);
    const loading = ref(true);
    const error = ref<Error | null>(null);

    onMounted(async () => {
        try {
            const res = await fetch('/api/items');
            items.value = await res.json();
        } catch (e) {
            error.value = e as Error;
        } finally {
            loading.value = false;
        }
    });

    return { items, loading, error };
}
```"""

VUE_FUNCTIONAL_TEST_GUIDANCE = """\
## Functional Test Requirements (Vitest + Vue Test Utils)
```typescript
import { mount } from '@vue/test-utils';
import TaskList from '../components/TaskList.vue';

test('adds and completes a task', async () => {
    const wrapper = mount(TaskList);
    await wrapper.find('input').setValue('Buy milk');
    await wrapper.find('button').trigger('click');
    expect(wrapper.text()).toContain('Buy milk');

    await wrapper.find('input[type="checkbox"]').trigger('change');
    expect(wrapper.find('input[type="checkbox"]').element.checked).toBe(true);
});
```"""

VUE_FEW_SHOT_NAMING = """\
## File naming conventions:
- Components: PascalCase (TaskList.vue, UserProfile.vue)
- Composables: camelCase with 'use' prefix (useAuth.ts, useFetch.ts)
- Services/utils: camelCase (api.ts, formatDate.ts)
- Tests: ComponentName.test.ts
- Pages: PascalCase (Home.vue, Dashboard.vue)"""

VUE_ITERATE_PROMPT = """\
Fix the issues described. Rules:
- Use Vue 3 Composition API with <script setup lang="ts">
- After editing, run: npx vitest run
- Then run: npx vite build

CURRENT ISSUES:
{validation_failures}

{instruction}

{code_map}"""

VUE_ITERATE_FEATURE_PROMPT = """\
Implement the requested feature. Rules:
- Use Vue 3 Composition API with <script setup lang="ts">
- Add tests for new components
- Run: npx vitest run && npx vite build

CURRENT ISSUES:
{validation_failures}

{instruction}

{code_map}"""


# ── Angular quality constants ────────────────────────────────────────────────

ANGULAR_CODING_STANDARDS = """\
## Coding Standards (apply to ALL generated code)
- Angular 17+ with standalone components (NO NgModule)
- TypeScript strict mode
- Signals for state management (signal, computed, effect)
- inject() for dependency injection instead of constructor injection
- Use Angular CLI conventions (ng generate patterns)
- RxJS for async streams, async/await for one-shot operations
- Reactive forms for form handling
- const by default, never var
- No `any` type"""

ANGULAR_PROJECT_STRUCTURE = """\
## Project Structure (Angular CLI)

```
package.json
tsconfig.json
angular.json
src/
    main.ts                # bootstrapApplication entry
    app/
        app.component.ts
        app.component.html
        app.component.css
        app.routes.ts
        components/
            header/
                header.component.ts
                header.component.html
        services/
            api.service.ts
        models/
            item.model.ts
```

**Critical setup**: Use `ng new` patterns. standalone: true on all components."""

ANGULAR_ANTI_PATTERNS = """\
## NEVER Do These (Angular mistakes)
- NEVER use NgModule — use standalone components only
- NEVER use constructor injection — use inject()
- NEVER subscribe manually without unsubscribing — use async pipe or takeUntilDestroyed
- NEVER use `any` type
- NEVER manipulate DOM directly — use template bindings and directives
- NEVER forget trackBy on *ngFor"""

ANGULAR_FEW_SHOT_SCAFFOLD = """\
## Example: Writing an Angular standalone component
```
write_file("src/app/components/task-list/task-list.component.ts", 'import { Component, signal, computed } from "@angular/core";\\n\
import { FormsModule } from "@angular/forms";\\n\\n\
interface Task { id: string; title: string; completed: boolean; }\\n\\n\
@Component({\\n\
    selector: "app-task-list",\\n\
    standalone: true,\\n\
    imports: [FormsModule],\\n\
    template: `<input [(ngModel)]="newTitle" placeholder="New task" /><button (click)="addTask()">Add</button>`,\\n\
})\\n\
export class TaskListComponent {\\n\
    tasks = signal<Task[]>([]);\\n\
    newTitle = "";\\n\
    pendingCount = computed(() => this.tasks().filter(t => !t.completed).length);\\n\\n\
    addTask() {\\n\
        if (!this.newTitle.trim()) return;\\n\
        this.tasks.update(ts => [...ts, { id: crypto.randomUUID(), title: this.newTitle.trim(), completed: false }]);\\n\
        this.newTitle = "";\\n\
    }\\n\\n\
    toggle(id: string) {\\n\
        this.tasks.update(ts => ts.map(t => t.id === id ? { ...t, completed: !t.completed } : t));\\n\
    }\\n\
}')
```

Notice: standalone component, signals for state, computed for derived values, FormsModule imported."""

ANGULAR_FEW_SHOT_MAIN = """\
## Example: Correct main.ts
```typescript
// src/main.ts
import { bootstrapApplication } from '@angular/platform-browser';
import { AppComponent } from './app/app.component';
import { provideRouter } from '@angular/router';
import { routes } from './app/app.routes';

bootstrapApplication(AppComponent, {
    providers: [provideRouter(routes)],
}).catch(err => console.error(err));
```"""

ANGULAR_FEW_SHOT_DB_PATTERN = """\
## Example: Angular service for API access
```typescript
import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

@Injectable({ providedIn: 'root' })
export class ApiService {
    private http = inject(HttpClient);
    getItems(): Observable<Item[]> { return this.http.get<Item[]>('/api/items'); }
}
```"""

ANGULAR_FUNCTIONAL_TEST_GUIDANCE = """\
## Functional Test Requirements (Jest/Karma)
```typescript
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { TaskListComponent } from './task-list.component';

describe('TaskListComponent', () => {
    let fixture: ComponentFixture<TaskListComponent>;
    beforeEach(async () => {
        await TestBed.configureTestingModule({ imports: [TaskListComponent] }).compileComponents();
        fixture = TestBed.createComponent(TaskListComponent);
    });

    it('should add a task', () => {
        fixture.componentInstance.newTitle = 'Buy milk';
        fixture.componentInstance.addTask();
        expect(fixture.componentInstance.tasks().length).toBe(1);
    });
});
```"""

ANGULAR_FEW_SHOT_NAMING = """\
## File naming conventions (Angular CLI):
- Components: kebab-case.component.ts (task-list.component.ts)
- Services: kebab-case.service.ts (api.service.ts)
- Models: kebab-case.model.ts (item.model.ts)
- Tests: *.spec.ts (task-list.component.spec.ts)"""

ANGULAR_ITERATE_PROMPT = """\
Fix the issues described. Rules:
- Use Angular 17+ standalone components
- After editing, run: npx ng test --watch=false
- Then run: npx ng build

CURRENT ISSUES:
{validation_failures}

{instruction}

{code_map}"""

ANGULAR_ITERATE_FEATURE_PROMPT = """\
Implement the requested feature. Rules:
- Use Angular 17+ standalone components with signals
- Add tests for new components
- Run: npx ng test --watch=false && npx ng build

CURRENT ISSUES:
{validation_failures}

{instruction}

{code_map}"""


# ── Electron (main + renderer) ───────────────────────────────────────────────
#
# Electron wraps a React renderer in a native window owned by a Node.js main
# process. Two processes, two security contexts. The hard-won lessons here
# are about keeping that boundary clean: contextIsolation on, nodeIntegration
# off, IPC only through a preload script's contextBridge — the same pattern
# every shipped electron app uses, and the one the LLM keeps trying to
# bypass with `webPreferences: { nodeIntegration: true }` because it's
# easier in the short term and ships a remote-code-execution hole.

ELECTRON_CODING_STANDARDS = """\
## Coding Standards (apply to ALL generated code)
- Two processes: MAIN (Node.js + electron APIs) and RENDERER (Chromium + React).
  They share NOTHING by default. The only bridge is a preload script.
- TypeScript strict mode: tsconfig.json with "strict": true
- Functional React components only in the renderer
- Main process code lives in src/main/ — never imported by renderer code
- Renderer code lives in src/renderer/ — never imports `electron` directly
- IPC: main process exposes handlers via ipcMain.handle(channel, fn);
  preload (src/main/preload.ts) wraps them in contextBridge.exposeInMainWorld('api', {...});
  renderer calls them as `window.api.foo()`. Type the surface in src/renderer/global.d.ts.
- BrowserWindow webPreferences MUST set: contextIsolation: true, nodeIntegration: false,
  sandbox: true, preload: <path to compiled preload.js>
- Use app.getPath('userData') for any persisted state — NEVER hardcode paths
- electron-builder config goes in package.json under "build": { ... }
- Use vite-plugin-electron in vite.config.ts so dev and prod use the same build pipeline"""

ELECTRON_PROJECT_STRUCTURE = """\
## Project Structure Guidelines (Electron + React + TypeScript via vite-plugin-electron)

**Standard layout**:
```
package.json              # "main": "dist-electron/main.js", electron-builder "build" config
tsconfig.json             # strict, jsx: react-jsx, module: ESNext
vite.config.ts            # imports electron from 'vite-plugin-electron', main+preload entries
index.html                # renderer entry HTML, references /src/renderer/main.tsx
src/
    main/
        main.ts           # app.whenReady, BrowserWindow, IPC handlers
        preload.ts        # contextBridge.exposeInMainWorld
    renderer/
        main.tsx          # ReactDOM.createRoot
        App.tsx
        global.d.ts       # `interface Window { api: { ... } }` for the IPC surface
        components/
        styles.css
__tests__/                # vitest tests for renderer components only
```

**Critical setup files**:
- `package.json`: `"main": "dist-electron/main.js"`, devDeps include
  `electron`, `electron-builder`, `vite-plugin-electron`, `vite`,
  `@vitejs/plugin-react`, `react`, `react-dom`, `typescript`, `vitest`,
  `@testing-library/react`, `@testing-library/jest-dom`, `jsdom`.
  Scripts: `"dev": "vite"`, `"build": "vite build"`,
  `"package": "electron-builder --win --publish=never"`,
  `"test": "vitest run"`. Add `"build": { "appId": "...", "productName": "...",
  "win": { "target": "nsis" }, "files": ["dist/**/*", "dist-electron/**/*"] }`.
- `vite.config.ts`: `import electron from 'vite-plugin-electron';
  export default defineConfig({ plugins: [react(), electron([
    { entry: 'src/main/main.ts' },
    { entry: 'src/main/preload.ts', onstart(args) { args.reload(); } },
  ])] });`
- `tsconfig.json`: standard React strict config. Module resolution "bundler".

**Larger projects** (15+ files): handled by modular pipeline."""

ELECTRON_ANTI_PATTERNS = """\
## NEVER Do These (electron + react + IPC mistakes)
- NEVER set `nodeIntegration: true` in webPreferences — this exposes Node.js
  to remote content. Always use a preload + contextBridge instead.
- NEVER set `contextIsolation: false` — same reason. Both default-on now.
- NEVER `import { app, ipcRenderer } from 'electron'` in renderer code —
  the renderer must NOT import the electron module. Use `window.api.*`.
- NEVER hardcode filesystem paths. Use `app.getPath('userData' | 'documents' | 'temp')`.
- NEVER forget the macOS quit gate: `app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit(); });`
- NEVER ship a build without `"main"` set in package.json — electron won't know what to launch.
- NEVER use `require('electron')` inside the renderer — same as the import rule.
- NEVER mutate React state directly; standard React rules still apply in the renderer
- NEVER skip the preload `contextBridge.exposeInMainWorld` step — direct
  ipcRenderer access from the renderer is blocked when contextIsolation is on (good)."""

ELECTRON_FEW_SHOT_SCAFFOLD = """\
## Example: writing the main process + preload + renderer-side typing

```
write_file("src/main/main.ts", `import { app, BrowserWindow, ipcMain } from 'electron';
import { join } from 'path';

const isDev = !app.isPackaged;

function createWindow() {
    const win = new BrowserWindow({
        width: 1024,
        height: 768,
        webPreferences: {
            preload: join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
            sandbox: true,
        },
    });
    if (isDev) {
        win.loadURL('http://localhost:5173');
    } else {
        win.loadFile(join(__dirname, '../dist/index.html'));
    }
}

ipcMain.handle('app:getVersion', () => app.getVersion());

app.whenReady().then(() => {
    createWindow();
    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
});

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
});
`)

write_file("src/main/preload.ts", `import { contextBridge, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('api', {
    getVersion: (): Promise<string> => ipcRenderer.invoke('app:getVersion'),
});
`)

write_file("src/renderer/global.d.ts", `export interface ElectronAPI {
    getVersion: () => Promise<string>;
}

declare global {
    interface Window {
        api: ElectronAPI;
    }
}
`)
```

The renderer then calls `await window.api.getVersion()` like any async function — no
`require`, no `electron` import."""

ELECTRON_FEW_SHOT_MAIN = """\
## Example: src/renderer/main.tsx (renderer entry — same as a vite-plugin-react app)
```typescript
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import './styles.css';

const root = document.getElementById('root');
if (!root) throw new Error('Root element #root not found in index.html');

createRoot(root).render(
    <StrictMode>
        <App />
    </StrictMode>,
);
```

## Example: src/renderer/App.tsx (uses the IPC surface typed in global.d.ts)
```typescript
import { useEffect, useState } from 'react';

export function App() {
    const [version, setVersion] = useState<string>('');
    useEffect(() => {
        window.api.getVersion().then(setVersion);
    }, []);
    return <div className="app"><h1>v{version}</h1></div>;
}
```"""

ELECTRON_FEW_SHOT_DB_PATTERN = """\
## Persistence in Electron — main process owns it, renderer asks via IPC

Two common choices:

### Settings / small JSON state — `electron-store`
```typescript
// src/main/main.ts
import Store from 'electron-store';
const store = new Store<{ theme: 'light' | 'dark' }>({ defaults: { theme: 'light' } });

ipcMain.handle('settings:get', (_e, key: string) => store.get(key));
ipcMain.handle('settings:set', (_e, key: string, value: unknown) => store.set(key, value));
```

### Real SQL — `better-sqlite3` (synchronous, fast, in main process only)
```typescript
// src/main/db.ts
import Database from 'better-sqlite3';
import { app } from 'electron';
import { join } from 'path';

const db = new Database(join(app.getPath('userData'), 'data.db'));
db.exec(`CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, body TEXT)`);

export function listNotes() {
    return db.prepare('SELECT id, body FROM notes ORDER BY id DESC').all();
}
```

Renderer NEVER touches the DB directly — exposes `notes:list`, `notes:create`
via IPC and the renderer calls `window.api.listNotes()`.

NEVER bundle better-sqlite3 in the renderer build — it's a native module
that lives in the main process only."""

ELECTRON_FUNCTIONAL_TEST_GUIDANCE = """\
## Renderer testing (vitest + @testing-library/react)
Same rules as a regular React+Vite project. Mock `window.api` per test:

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { App } from './App';

beforeEach(() => {
    (window as unknown as { api: { getVersion: () => Promise<string> } }).api = {
        getVersion: vi.fn().mockResolvedValue('1.2.3'),
    };
});

describe('App', () => {
    it('renders the version from the IPC bridge', async () => {
        render(<App />);
        await waitFor(() => expect(screen.getByText('v1.2.3')).toBeInTheDocument());
    });
});
```

Use `within(container).getByText(...)` to disambiguate when multiple matches.
Prefer `getByRole` > `getByLabelText` > `getByText` for stable queries.
Cast input refs as `HTMLInputElement` when reading `.value`.

Main-process tests are out of scope here — they require an electron runtime
(electron-mocha) which the autobuilder doesn't currently set up."""

ELECTRON_FEW_SHOT_NAMING = """\
## Naming conventions
- Components: PascalCase, one component per file matching the filename: TaskList.tsx, Header.tsx
- Hooks: camelCase prefixed `use`: useNotes.ts, useSettings.ts
- IPC channel names: namespaced kebab-case: 'notes:list', 'settings:get', 'app:getVersion'
- Files in src/main/: lowercase, role-named: main.ts, preload.ts, db.ts, ipc.ts
- Tests: ComponentName.test.tsx in __tests__/ or alongside the component
- The IPC surface type lives in src/renderer/global.d.ts and is named `ElectronAPI`."""

ELECTRON_ITERATE_PROMPT = """\
You are iterating on an Electron + React + TypeScript project.

Tools available: edit_file, write_file, line_edit, run_command, read_file.

Rules:
- Make the smallest change that fixes the failure
- After editing, run `npx tsc --noEmit` and `npx vitest run` to verify
- Renderer changes must NOT introduce direct electron imports — go through
  the preload IPC bridge and `window.api.*`
- Main process changes go in src/main/, renderer changes in src/renderer/
- If the IPC surface changes, update both src/main/preload.ts and
  src/renderer/global.d.ts in the same pass

CURRENT ISSUES:
{validation_failures}

{code_map}"""

ELECTRON_ITERATE_FEATURE_PROMPT = """\
Implement the requested feature. Rules:
- Renderer-only feature: edit src/renderer/, add tests with vitest
- Feature touching the OS / filesystem / native: add an ipcMain.handle in
  src/main/main.ts, expose via contextBridge in src/main/preload.ts,
  type in src/renderer/global.d.ts, call from React in src/renderer/
- After: `npx tsc --noEmit && npx vitest run` must pass
- Keep contextIsolation: true and nodeIntegration: false. Always.

CURRENT ISSUES:
{validation_failures}

{instruction}

{code_map}"""


# ── Go ───────────────────────────────────────────────────────────────────────

GO_CODING_STANDARDS = """\
## Coding Standards (apply to ALL generated code)
- Standard layout: cmd/<binary>/main.go for binaries; pkg/ for exported,
  internal/ for unexported library code
- go.mod at repo root; use modules (NOT GOPATH-style)
- One package per directory; package name matches the dir
- Errors are values: return `(T, error)`. NEVER `panic` outside `init` or truly
  unrecoverable startup failure. Wrap with `fmt.Errorf("...: %w", err)`.
- gofmt on every file — no exceptions
- Exported names start with capital. Use them sparingly; prefer narrow public surface.
- context.Context as first arg for any function that does I/O or can be cancelled
- Avoid global state. Pass dependencies through structs/interfaces.
- Use `t.Run(name, func(t *testing.T) {...})` subtests over giant table-driven
  loops when setup differs per case.
- Concurrency: goroutines must have a clear lifecycle (started by X, stopped
  by Y). NEVER `go f()` without a way to wait for it or cancel via context."""

GO_PROJECT_STRUCTURE = """\
## Project Structure Guidelines (Go modules)

**CLI tool / single binary**:
```
go.mod                           # module declaration
go.sum
main.go                          # if simple — just package main + main()
cmd/<binary>/main.go             # if multi-binary or growing
internal/<feature>/<feature>.go  # private library code
internal/<feature>/<feature>_test.go
```

**Service (HTTP/gRPC)**:
```
go.mod
cmd/server/main.go               # bootstrapping only — config + server.New()
internal/server/server.go        # http handlers, wiring
internal/<domain>/...            # business logic per domain
internal/storage/                # DB layer (sqlx, gorm, raw database/sql)
```

**Test files** live alongside their target: `foo.go` + `foo_test.go` in the
same package. Use `package foo_test` for black-box tests, `package foo` for
white-box tests that need internals.

**go.mod** must declare the module path (e.g., `module example.com/myapp`)
and a Go version (`go 1.22`). Dependencies are added via `go get` or by
editing the require block."""

GO_ANTI_PATTERNS = """\
## NEVER Do These (Go mistakes)
- NEVER use `panic` outside `init()` or unrecoverable startup. Return errors.
- NEVER ignore errors with `_, _ = foo()` or bare `foo()`. Handle every error.
- NEVER start a goroutine without a way to stop it (context cancel, channel close)
- NEVER use `time.Sleep` in tests as a "wait for goroutine" mechanism — use channels
- NEVER share a `sync.WaitGroup` by value — pass `*sync.WaitGroup`
- NEVER mutate a map concurrently without sync.Mutex / sync.RWMutex / sync.Map
- NEVER call `os.Exit` from a library — only from `main`
- NEVER use the empty interface `interface{}` (or `any`) as a return type
  if you can return a concrete type; it forces type-switches at every call site
- NEVER capture loop variables by reference in goroutines:
  `for _, v := range xs { go func() { use(v) }() }` — bind explicitly:
  `for _, v := range xs { v := v; go func() { use(v) }() }`"""

GO_FEW_SHOT_SCAFFOLD = """\
## Example: writing a Go package with tests
```
write_file("internal/counter/counter.go", `package counter

import "sync"

type Counter struct {
    mu sync.Mutex
    n  int
}

func New() *Counter { return &Counter{} }

func (c *Counter) Inc() {
    c.mu.Lock()
    defer c.mu.Unlock()
    c.n++
}

func (c *Counter) Value() int {
    c.mu.Lock()
    defer c.mu.Unlock()
    return c.n
}
`)

write_file("internal/counter/counter_test.go", `package counter

import (
    "sync"
    "testing"
)

func TestInc(t *testing.T) {
    c := New()
    c.Inc(); c.Inc(); c.Inc()
    if got := c.Value(); got != 3 { t.Fatalf("want 3, got %d", got) }
}

func TestConcurrentInc(t *testing.T) {
    c := New()
    var wg sync.WaitGroup
    for i := 0; i < 100; i++ {
        wg.Add(1)
        go func() { defer wg.Done(); c.Inc() }()
    }
    wg.Wait()
    if got := c.Value(); got != 100 { t.Fatalf("want 100, got %d", got) }
}
`)
```"""

GO_FEW_SHOT_MAIN = """\
## Example: cmd/<binary>/main.go (entry point)
```go
package main

import (
    "context"
    "flag"
    "fmt"
    "log"
    "os"
    "os/signal"
    "syscall"

    "example.com/myapp/internal/server"
)

func main() {
    test := flag.Bool("test", false, "run smoke test then exit")
    addr := flag.String("addr", ":8080", "listen address")
    flag.Parse()

    if *test {
        fmt.Println("smoke ok")
        return
    }

    ctx, cancel := signal.NotifyContext(context.Background(),
        os.Interrupt, syscall.SIGTERM)
    defer cancel()

    if err := server.Run(ctx, *addr); err != nil {
        log.Fatalf("server: %v", err)
    }
}
```"""

GO_FEW_SHOT_DB_PATTERN = """\
## Persistence in Go — common patterns

### SQLite via `mattn/go-sqlite3` and `database/sql`
```go
import (
    "database/sql"
    _ "github.com/mattn/go-sqlite3"
)

db, err := sql.Open("sqlite3", "data.db")
if err != nil { return err }
defer db.Close()

if _, err := db.Exec(`CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, body TEXT)`); err != nil {
    return err
}

rows, err := db.QueryContext(ctx, "SELECT id, body FROM notes WHERE id=?", id)
```

Always use parameter placeholders (`?`) — NEVER concatenate user input into SQL."""

GO_FUNCTIONAL_TEST_GUIDANCE = """\
## Go testing (`go test`)
- Test files end in `_test.go`, in the same package as the code under test
- Test functions: `func TestXxx(t *testing.T)` — start with capital `Test`
- `t.Fatalf` to fail and stop; `t.Errorf` to fail and continue
- Subtests: `t.Run("name", func(t *testing.T) {...})`
- Table-driven tests for many cases:
  ```go
  for _, tc := range []struct {
      name string; in int; want int
  }{
      {"zero", 0, 0}, {"one", 1, 2},
  } {
      t.Run(tc.name, func(t *testing.T) {
          if got := double(tc.in); got != tc.want { t.Fatalf("...") }
      })
  }
  ```
- Race detection: `go test -race ./...` — run for any concurrent code
- Don't `t.Parallel()` tests that share global state"""

GO_FEW_SHOT_NAMING = """\
## Naming conventions
- Packages: lowercase, single word: `auth`, `storage`, `server`. NOT `myAuth` or `auth_pkg`.
- Exported types/functions: PascalCase: `Counter`, `NewServer`
- Unexported: camelCase: `bufferSize`, `parseConfig`
- Files: lowercase, underscores OK for clarity: `user_repo.go`, `counter_test.go`
- Interfaces: usually `-er` suffix when single-method: `Reader`, `Closer`, `Stringer`
- Receivers: short, consistent: `c *Counter`, `s *Server`. Same name in every method.
- Errors: variables prefixed `Err`: `ErrNotFound`. Sentinel errors at package level."""

GO_ITERATE_PROMPT = """\
You are iterating on a Go project.

Tools: edit_file, write_file, line_edit, run_command, read_file.

Rules:
- Run `go build ./...` after edits to catch compile errors
- Run `go test ./...` to verify; `go test -race ./...` for concurrent code
- Run `go vet ./...` — silence each warning legitimately, don't suppress
- Errors are values: every err must be checked, wrapped with %w if rethrown
- gofmt the result (or use `goimports`)

CURRENT ISSUES:
{validation_failures}

{code_map}"""

GO_ITERATE_FEATURE_PROMPT = """\
Implement the requested feature in the existing Go module.
- Keep the package layout (cmd/, internal/, pkg/) consistent
- Add tests in <feature>_test.go — at least one happy path + one error path
- After: `go build ./... && go test ./... && go vet ./...` must pass

CURRENT ISSUES:
{validation_failures}

{instruction}

{code_map}"""


# ── Rust ─────────────────────────────────────────────────────────────────────

RUST_CODING_STANDARDS = """\
## Coding Standards (apply to ALL generated code)
- Cargo.toml at repo root; src/main.rs (binary) or src/lib.rs (library) or both
- 2024 edition; pin a recent stable rustc version
- Use `Result<T, E>` for fallible operations. NEVER `unwrap()` outside tests
  or "this can't fail because ..." with a comment proving it
- Errors: use `thiserror` for library error types, `anyhow` for application
  binaries. Wrap lower-level errors with `.context("doing X")` (anyhow) or
  `#[from]` (thiserror).
- Ownership first: prefer `&str` over `String`, `&[T]` over `Vec<T>` in
  function signatures. Take ownership (`String`, `Vec<T>`) only when the
  function needs it.
- `#[derive(Debug)]` on every public struct/enum. `Clone` only when needed.
- Lifetimes: name them when they convey intent (`'a` for "lives as long as
  the input"). Don't fight the borrow checker — restructure.
- async: use `tokio` (most common) and `async fn`. Don't mix `tokio::spawn`
  with non-tokio runtimes.
- Tests in same file under `#[cfg(test)] mod tests { ... }` for unit tests,
  `tests/` directory for integration tests.
- `cargo fmt` and `cargo clippy -- -D warnings` on every commit."""

RUST_PROJECT_STRUCTURE = """\
## Project Structure Guidelines (Cargo)

**Binary**:
```
Cargo.toml
Cargo.lock
src/
    main.rs                  # binary entry — clap parsing + call into lib
    lib.rs                   # public API (if also publishing as lib)
    <module>.rs              # OR <module>/mod.rs — pick one and stick to it
    <module>/<sub>.rs
tests/
    integration_test.rs      # tests against the public lib API
```

**Library only**:
```
Cargo.toml
src/lib.rs                   # `pub use` the things you mean to export
src/internal.rs              # private modules (no `pub`)
tests/
```

**WHERE TESTS GO** (this is the most common LLM mistake — get it right):
- **Unit tests** for module `foo` live INSIDE `src/foo.rs` (or `src/foo/mod.rs`)
  in a `#[cfg(test)] mod tests { use super::*; ... }` block at the bottom of
  the file. They test the module's private + public API.
- **Integration tests** live in `tests/<name>.rs` at the workspace root.
  They test the crate's public API only (no access to internals).
- **DO NOT create a nested cargo crate per module** — `src/foo/src/lib.rs`
  is wrong. Cargo will not run tests in those nested crates from `cargo test`
  at the workspace root, and you'll get "running 0 tests" with no clue why.
- **DO NOT put unit tests in a separate `tests.rs` next to the module file**
  unless you explicitly include it via `#[cfg(test)] mod tests;` — they
  won't run otherwise.

**Cargo.toml minimum**:
```toml
[package]
name = "myapp"
version = "0.1.0"
edition = "2021"

[dependencies]
# add via `cargo add <crate>` or here directly with version pins

[dev-dependencies]
# only used in tests/benches
```"""

RUST_ANTI_PATTERNS = """\
## NEVER Do These (Rust mistakes)
- NEVER `unwrap()` / `expect()` on a Result/Option in production code paths.
  In tests it's fine; in main code, propagate with `?` or handle the None/Err.
- NEVER `clone()` to silence the borrow checker — restructure ownership instead
- NEVER block on async code with `block_on` in async contexts (deadlocks runtime)
- NEVER use `unsafe` without a comment explaining the invariant being upheld
- NEVER hold a `MutexGuard` across an `.await` — use `tokio::sync::Mutex` or release before awaiting
- NEVER `Box<dyn Trait>` reflexively — prefer concrete types or generics with bounds
- NEVER `String::from_utf8_unchecked` on input you didn't validate
- NEVER ignore `#[must_use]` warnings (they exist because the value matters)
- NEVER write `.unwrap_or_default()` without thinking — sometimes silently swallowing an error is wrong
- NEVER create a nested `src/<module>/src/lib.rs` per module — that's a
  workspace-style sub-crate that `cargo test` will not pick up from the
  root. Modules live in `src/<module>.rs` (or `src/<module>/mod.rs`)
  with `#[cfg(test)] mod tests` blocks inside the module file itself.
- NEVER put a module's unit tests in a separate file like
  `src/foo/tests.rs` unless the module file has a matching
  `#[cfg(test)] mod tests;` declaration to pull it in. Without that
  declaration the tests are invisible to `cargo test`."""

RUST_FEW_SHOT_SCAFFOLD = """\
## Example: writing a Rust module with tests
```
write_file("src/counter.rs", `use std::sync::Mutex;

pub struct Counter {
    inner: Mutex<i64>,
}

impl Counter {
    pub fn new() -> Self {
        Self { inner: Mutex::new(0) }
    }

    pub fn inc(&self) {
        let mut n = self.inner.lock().expect("counter mutex poisoned");
        *n += 1;
    }

    pub fn value(&self) -> i64 {
        *self.inner.lock().expect("counter mutex poisoned")
    }
}

impl Default for Counter {
    fn default() -> Self { Self::new() }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn inc_increments() {
        let c = Counter::new();
        c.inc(); c.inc(); c.inc();
        assert_eq!(c.value(), 3);
    }

    #[test]
    fn default_is_zero() {
        let c = Counter::default();
        assert_eq!(c.value(), 0);
    }
}
`)
```"""

RUST_FEW_SHOT_MAIN = """\
## Example: src/main.rs (binary entry)
```rust
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|a| a == "--test") {
        println!("smoke ok");
        return ExitCode::SUCCESS;
    }
    if let Err(e) = run(&args) {
        eprintln!("error: {e:#}");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}

fn run(_args: &[String]) -> anyhow::Result<()> {
    // ...
    Ok(())
}
```"""

RUST_FEW_SHOT_DB_PATTERN = """\
## Persistence in Rust — common choices

### SQLite via `rusqlite`
```rust
use rusqlite::{Connection, params};

let conn = Connection::open("data.db")?;
conn.execute(
    "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, body TEXT)",
    [],
)?;
conn.execute("INSERT INTO notes (body) VALUES (?1)", params![body])?;

// Always use parameter placeholders. NEVER format!() user input into SQL.
```

### Async + Postgres via `sqlx`
```rust
let pool = sqlx::postgres::PgPoolOptions::new().connect(&url).await?;
let row: (i64,) = sqlx::query_as("SELECT id FROM users WHERE email = $1")
    .bind(email)
    .fetch_one(&pool).await?;
```"""

RUST_FUNCTIONAL_TEST_GUIDANCE = """\
## Rust testing (`cargo test`)
- Unit tests: `#[cfg(test)] mod tests { use super::*; #[test] fn it_works() {} }`
- Integration tests: separate files in `tests/` — they import the lib's public API only
- `assert_eq!`, `assert!`, `assert_ne!` for assertions
- `#[should_panic(expected = "msg")]` for panics
- `#[ignore]` to skip slow tests; run with `cargo test -- --ignored`
- Concurrent code: `cargo test --release` to validate under optimizer; consider
  `loom` for systematic concurrency testing of small primitives
- `proptest` / `quickcheck` for property-based tests when the domain has
  invariants (e.g., serialize/deserialize roundtrip, sort idempotence)"""

RUST_FEW_SHOT_NAMING = """\
## Naming conventions (idiomatic Rust)
- Modules / files: snake_case: `user_repo.rs`, `mod parser`
- Types / traits: PascalCase: `Counter`, `Display`, `IntoIterator`
- Functions / variables / methods: snake_case: `fn parse_input`, `let max_size`
- Constants: SCREAMING_SNAKE_CASE: `const MAX_BUFFER: usize = 1024;`
- Lifetimes: lowercase, often single letter: `'a`, `'src`
- Crate names: kebab-case in Cargo.toml (`my-crate`), snake_case in code (`my_crate`)"""

RUST_ITERATE_PROMPT = """\
You are iterating on a Rust project.

Tools: edit_file, write_file, line_edit, run_command, read_file.

Rules:
- Run `cargo build` to catch compile errors first
- Run `cargo test` to verify
- Run `cargo clippy -- -D warnings` — fix every clippy lint legitimately
- Use `?` for error propagation, not `.unwrap()`
- If the borrow checker fights you, restructure ownership; don't add `.clone()` reflexively

CURRENT ISSUES:
{validation_failures}

{code_map}"""

RUST_ITERATE_FEATURE_PROMPT = """\
Implement the requested feature in the existing Cargo project.
- Keep module layout consistent (lib.rs / module structure)
- Add tests: unit tests in #[cfg(test)] mod tests, integration in tests/
- Update Cargo.toml [dependencies] if you need a new crate (use cargo add)
- After: `cargo build && cargo test && cargo clippy -- -D warnings` must pass

CURRENT ISSUES:
{validation_failures}

{instruction}

{code_map}"""


# ── WordPress plugin ─────────────────────────────────────────────────────────

WORDPRESS_CODING_STANDARDS = """\
## Coding Standards (apply to ALL generated code)
- WordPress requires the plugin's main PHP file to start with a header
  block (Plugin Name, Description, Version, Author, License, Text Domain).
  Without it WP refuses to install the plugin.
- Prefix EVERY function, class, constant, option key, hook with a unique
  slug — e.g., `mp_` for "MyPlugin". WP plugins share global namespace.
  `function init()` will collide with another plugin's `init()`.
- Hook into WP via `add_action()` / `add_filter()`. Do not output anything
  on plugin load — only when hooks fire.
- Sanitize input on the way IN: `sanitize_text_field`, `sanitize_email`,
  `sanitize_url`, `wp_kses_post`. Escape on the way OUT: `esc_html`,
  `esc_attr`, `esc_url`, `wp_kses_post`.
- Nonce-protect all admin form submissions: `wp_nonce_field` on render,
  `check_admin_referer` on submit. Same for AJAX: `wp_create_nonce` /
  `check_ajax_referer`.
- Capability checks before privileged actions: `current_user_can('...')`.
- Use the WP DB abstraction `$wpdb` with `$wpdb->prepare()` — NEVER
  concatenate input into SQL.
- Internationalize strings: `__('text', 'mp-textdomain')`,
  `_e('text', 'mp-textdomain')`. Load text domain in
  `plugins_loaded` hook.
- PHP requires: 7.4+ generally fine, 8.0+ for typed properties / match.
  Add `Requires PHP: 7.4` to header."""

WORDPRESS_PROJECT_STRUCTURE = """\
## Project Structure (WP plugin)

```
<plugin-slug>.php             # Main plugin file with WP plugin header.
                              # The filename matches the slug. WP recognizes
                              # the plugin by this file. Keep it small —
                              # delegate logic to includes/.
includes/
    class-mp-core.php         # Bootstrap class, registers hooks.
    class-mp-admin.php        # Admin UI logic.
    class-mp-rest-api.php     # REST API endpoints (if any).
    class-mp-db.php           # $wpdb wrappers.
admin/
    settings-page.php         # Admin UI templates.
    css/admin.css
    js/admin.js
public/
    css/public.css
    js/public.js
languages/
    <textdomain>.pot          # Generated by `wp i18n make-pot` (optional).
README.txt                    # WP-specific format with Description /
                              # Installation / Changelog / FAQ sections.
uninstall.php                 # Cleanup on plugin removal.
```

**Required header in main plugin file**:
```php
<?php
/**
 * Plugin Name: My Plugin
 * Description: Short one-liner shown on the Plugins page.
 * Version:     1.0.0
 * Author:      Author Name
 * License:     GPL-2.0-or-later
 * License URI: https://www.gnu.org/licenses/gpl-2.0.html
 * Text Domain: mp-textdomain
 * Requires PHP: 7.4
 */

if ( ! defined( 'ABSPATH' ) ) { exit; } // No direct access.
```"""

WORDPRESS_ANTI_PATTERNS = """\
## NEVER Do These (WordPress plugin mistakes)
- NEVER name a function/class without a plugin prefix — collides with WP core
  or other plugins (`init`, `setup`, `User`, `Cache`).
- NEVER skip the `if ( ! defined( 'ABSPATH' ) ) { exit; }` guard — direct
  HTTP requests to your PHP files become a vulnerability.
- NEVER concatenate user input into SQL — always `$wpdb->prepare()`.
- NEVER `echo` user-supplied or DB-fetched values without escaping
  (`esc_html`, `esc_attr`, `esc_url`, `wp_kses_post`).
- NEVER run privileged code without `current_user_can()` + nonce check.
- NEVER load CSS/JS via raw `<script>` / `<link>` tags — use `wp_enqueue_style`
  / `wp_enqueue_script` so WP can manage versions/dependencies/admin-vs-public.
- NEVER fire side-effecting code at file load time. Use the right hook:
  `init`, `admin_init`, `wp_loaded`, `plugins_loaded`, etc.
- NEVER hard-code DB table names. Use `$wpdb->prefix . 'mp_things'`.
- NEVER trust `$_GET`, `$_POST`, `$_REQUEST`, `$_SERVER` without sanitizing
  + nonce-checking.
- NEVER include `wp-config.php` or `wp-load.php` from a plugin — load order
  is wrong; use proper hooks instead."""

WORDPRESS_FEW_SHOT_SCAFFOLD = """\
## Example: writing a small WP plugin

```
write_file("my-plugin.php", `<?php
/**
 * Plugin Name: My Plugin
 * Description: Adds a simple shortcode [mp-greeting] for testing.
 * Version:     1.0.0
 * Author:      Test Author
 * License:     GPL-2.0-or-later
 * Text Domain: mp-plugin
 * Requires PHP: 7.4
 */

if ( ! defined( 'ABSPATH' ) ) { exit; }

require_once plugin_dir_path( __FILE__ ) . 'includes/class-mp-core.php';

add_action( 'plugins_loaded', array( 'MP_Core', 'init' ) );
`)

write_file("includes/class-mp-core.php", `<?php
if ( ! defined( 'ABSPATH' ) ) { exit; }

class MP_Core {
    public static function init() {
        load_plugin_textdomain( 'mp-plugin', false,
            dirname( plugin_basename( __FILE__ ) ) . '/../languages' );
        add_shortcode( 'mp-greeting', array( __CLASS__, 'render_greeting' ) );
    }

    public static function render_greeting( $atts ) {
        $atts = shortcode_atts(
            array( 'name' => 'world' ),
            $atts,
            'mp-greeting'
        );
        return '<p>' . esc_html( sprintf(
            __( 'Hello, %s!', 'mp-plugin' ),
            $atts['name']
        ) ) . '</p>';
    }
}
`)
```"""

WORDPRESS_FEW_SHOT_MAIN = """\
## Example: a complete settings page (admin UI)

```php
<?php
// admin/settings-page.php
if ( ! defined( 'ABSPATH' ) ) { exit; }

class MP_Settings {
    const OPTION_NAME = 'mp_plugin_options';
    const NONCE_ACTION = 'mp_save_settings';

    public static function init() {
        add_action( 'admin_menu', array( __CLASS__, 'add_menu' ) );
        add_action( 'admin_init', array( __CLASS__, 'register_settings' ) );
    }

    public static function add_menu() {
        add_options_page(
            __( 'My Plugin Settings', 'mp-plugin' ),
            __( 'My Plugin', 'mp-plugin' ),
            'manage_options',
            'mp-plugin-settings',
            array( __CLASS__, 'render_page' )
        );
    }

    public static function register_settings() {
        register_setting( 'mp_plugin_group', self::OPTION_NAME, array(
            'sanitize_callback' => array( __CLASS__, 'sanitize' ),
        ) );
    }

    public static function sanitize( $input ) {
        return array(
            'api_key' => sanitize_text_field( $input['api_key'] ?? '' ),
            'enabled' => ! empty( $input['enabled'] ),
        );
    }

    public static function render_page() {
        if ( ! current_user_can( 'manage_options' ) ) { return; }
        $opts = get_option( self::OPTION_NAME, array() );
        ?>
        <div class="wrap">
            <h1><?php esc_html_e( 'My Plugin', 'mp-plugin' ); ?></h1>
            <form method="post" action="options.php">
                <?php settings_fields( 'mp_plugin_group' ); ?>
                <input name="mp_plugin_options[api_key]" type="text"
                       value="<?php echo esc_attr( $opts['api_key'] ?? '' ); ?>" />
                <?php submit_button(); ?>
            </form>
        </div>
        <?php
    }
}
```"""

WORDPRESS_FEW_SHOT_DB_PATTERN = """\
## Persistence in WordPress — three real options

### 1. Settings (small, structured): `register_setting` + `get_option` / `update_option`
```php
register_setting( 'mp_group', 'mp_plugin_options' );
$opts = get_option( 'mp_plugin_options', array() );
update_option( 'mp_plugin_options', array( 'api_key' => '...' ) );
```

### 2. Custom table via $wpdb (heavier data: events, logs, custom CRUD)
```php
function mp_create_table() {
    global $wpdb;
    $table = $wpdb->prefix . 'mp_events';
    $charset = $wpdb->get_charset_collate();
    $sql = "CREATE TABLE IF NOT EXISTS {$table} (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        user_id BIGINT UNSIGNED NOT NULL,
        kind VARCHAR(64) NOT NULL,
        payload LONGTEXT,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        KEY user_id (user_id)
    ) {$charset};";
    require_once ABSPATH . 'wp-admin/includes/upgrade.php';
    dbDelta( $sql );
}
register_activation_hook( __FILE__, 'mp_create_table' );

// Query — ALWAYS prepare:
$rows = $wpdb->get_results( $wpdb->prepare(
    "SELECT id, kind FROM {$wpdb->prefix}mp_events WHERE user_id = %d",
    $user_id
) );
```

### 3. Post meta (per-post key/value) for content extensions
```php
update_post_meta( $post_id, 'mp_score', 42 );
$score = get_post_meta( $post_id, 'mp_score', true );
```"""

WORDPRESS_FUNCTIONAL_TEST_GUIDANCE = """\
## WordPress plugin testing
Two realistic options:

1. **PHPUnit + WP test suite** (`wp scaffold plugin-tests`). Heavy setup
   (needs MySQL, downloads WP core into a tests dir). Best for plugins
   intended for the WP.org repo.
2. **Pure-PHP unit tests** for non-WP-API functions: extract pure-logic
   helpers (sanitization, parsing, transformations) into testable
   functions. Test with `phpunit` against just those.

Cadillac scaffolds option 2 by default — pure logic in `includes/utils.php`,
tested with `phpunit tests/`. WP-integration tests are out of scope here:
they require a running WordPress install which the autobuilder can't
provide on this box.

Test file layout:
```
tests/
    bootstrap.php          # require composer autoload + plugin classes
    UtilsTest.php
phpunit.xml.dist           # bootstrap=tests/bootstrap.php
composer.json              # require-dev: phpunit/phpunit
```"""

WORDPRESS_FEW_SHOT_NAMING = """\
## Naming conventions (WordPress)
- Plugin slug: lowercase-kebab: `my-plugin`. Used in plugin dir name and main file name.
- Function prefix: 2-4 letters of the slug + underscore: `mp_`, `mpl_`. EVERY function.
- Class prefix: PascalCase with prefix: `MP_Core`, `MP_Settings`, `MP_Admin`.
- Constants: SCREAMING_SNAKE with prefix: `MP_VERSION`, `MP_PLUGIN_DIR`.
- Option keys: prefixed snake_case: `mp_plugin_options`, `mp_api_key`.
- Hook tags (custom actions/filters): prefixed: `mp_before_save`, `mp_filter_data`.
- Text domain: matches plugin slug: `'mp-plugin'`. Same string everywhere.
- DB tables: `$wpdb->prefix . 'mp_things'`. NEVER hardcode `wp_mp_things`."""

WORDPRESS_ITERATE_PROMPT = """\
You are iterating on a WordPress plugin.

Tools: edit_file, write_file, line_edit, run_command, read_file.

Rules:
- Run `php -l <file>` after each PHP file edit to check syntax
- All output through `esc_html` / `esc_attr` / `esc_url` / `wp_kses_post`
- All input through sanitize_*_field; nonce + capability checks for forms
- Use `$wpdb->prepare()` for any SQL — never concatenate input
- All function/class names prefixed with the plugin slug
- Activation/deactivation/uninstall hooks must clean up after themselves

CURRENT ISSUES:
{validation_failures}

{code_map}"""

WORDPRESS_ITERATE_FEATURE_PROMPT = """\
Implement the requested feature in the existing WordPress plugin.
- Keep the file layout (main.php / includes/ / admin/ / public/)
- Hook in via add_action / add_filter — never run on bare include
- Add admin UI under existing settings page or wp-admin menu
- After: `php -l` clean on every edited PHP file. Pure-PHP tests pass.

CURRENT ISSUES:
{validation_failures}

{instruction}

{code_map}"""


# ── Browser extension (Chrome MV3) ───────────────────────────────────────────

BROWSER_EXT_CODING_STANDARDS = """\
## Coding Standards (apply to ALL generated code)
- Manifest V3 only — V2 is deprecated and Chrome refuses MV2 uploads.
  manifest.json with `"manifest_version": 3`.
- Service worker (background script) instead of MV2's persistent
  background page: `"background": { "service_worker": "background.js" }`.
- TypeScript strict mode with @types/chrome (or @types/webextension-polyfill
  for cross-browser).
- Build via **`@crxjs/vite-plugin@^2`** (preferred — actively maintained,
  cleaner Vite 5 + Node 18 support) OR `vite-plugin-web-extension@^4.4`
  if you need cross-browser. Either works; @crxjs is less likely to hit
  the CJS-loading bug we caught in past builds. Pin the major version
  in package.json; don't use `*`.
- Permissions: principle of least privilege. Don't request `<all_urls>`
  if you actually need just `https://*.example.com/*`. List permissions
  exactly: `"permissions": ["storage", "activeTab"]`.
- Content scripts can NOT access background's variables directly —
  communicate via `chrome.runtime.sendMessage` / `onMessage`.
- Storage: `chrome.storage.local` (large) / `chrome.storage.sync` (small,
  syncs across user's Chrome installs). Both async, return Promises.
- CSP: MV3 forbids `eval` and remote scripts. All JS must be bundled."""

BROWSER_EXT_PROJECT_STRUCTURE = """\
## Project Structure (Chrome MV3 + Vite + TypeScript)

```
package.json              # devDeps: vite ^5, @crxjs/vite-plugin ^2 (or
                          # vite-plugin-web-extension ^4.4),
                          # @types/chrome, vitest ^1, typescript
vite.config.ts            # imports the extension plugin; ref to manifest
manifest.json             # MV3 manifest — entry point Chrome loads
public/icons/             # 16/32/48/128 PNG icons referenced by manifest
src/
    background.ts         # service worker — listen for runtime/storage events
    content/
        content.ts        # injected into matched pages
    popup/
        popup.html        # popup UI shown when toolbar icon clicked
        popup.tsx         # popup React entry (if using React)
        popup.css
    options/
        options.html
        options.tsx
    types/
        index.ts          # message-shape types shared between scripts
__tests__/                # vitest unit tests for pure-logic helpers
```

**Manifest skeleton**:
```json
{
  "manifest_version": 3,
  "name": "My Extension",
  "version": "1.0.0",
  "description": "Short description.",
  "action": {
    "default_popup": "src/popup/popup.html",
    "default_icon": { "16": "icons/16.png", "48": "icons/48.png" }
  },
  "background": { "service_worker": "src/background.ts", "type": "module" },
  "content_scripts": [{
    "matches": ["https://*.example.com/*"],
    "js": ["src/content/content.ts"]
  }],
  "permissions": ["storage", "activeTab"],
  "host_permissions": ["https://*.example.com/*"],
  "options_ui": { "page": "src/options/options.html", "open_in_tab": true },
  "icons": { "16": "icons/16.png", "48": "icons/48.png", "128": "icons/128.png" }
}
```"""

BROWSER_EXT_ANTI_PATTERNS = """\
## NEVER Do These (Chrome MV3 mistakes)
- NEVER write to manifest_version: 2 — Chrome rejects new MV2 uploads.
- NEVER use `eval` / `new Function()` / `setTimeout(string)` — MV3 CSP
  blocks all string-eval. Bundle everything statically.
- NEVER load remote scripts (`<script src="https://other-cdn.com/...">`)
  in popups/options — MV3 CSP blocks; bundle deps with vite.
- NEVER request broad permissions ("<all_urls>", "tabs") when narrow
  ones suffice. Chrome Web Store review will reject overbroad permissions.
- NEVER assume the service worker stays alive — MV3 service workers
  shut down after ~30s idle. Persist state via chrome.storage, not
  module-level variables.
- NEVER send DOM nodes via runtime.sendMessage — only structured-clone
  serializable data (JSON-ish). Convert to plain objects first.
- NEVER use `chrome.runtime.connect` long-lived ports for things that
  could be one-shot messages — they keep the worker alive needlessly.
- NEVER store secrets in the extension code; users can unzip the .crx
  and read every line. If the extension needs a per-user token, use
  OAuth via chrome.identity.launchWebAuthFlow."""

BROWSER_EXT_FEW_SHOT_SCAFFOLD = """\
## Example: minimal MV3 extension with React popup

```
write_file("manifest.json", `{
    "manifest_version": 3,
    "name": "TabCounter",
    "version": "1.0.0",
    "description": "Counts open tabs and shows the number on the toolbar icon.",
    "action": {
        "default_popup": "src/popup/popup.html",
        "default_icon": { "16": "icons/16.png", "48": "icons/48.png" }
    },
    "background": { "service_worker": "src/background.ts", "type": "module" },
    "permissions": ["tabs"],
    "icons": { "16": "icons/16.png", "48": "icons/48.png", "128": "icons/128.png" }
}
`)

write_file("src/background.ts", `chrome.tabs.onCreated.addListener(updateBadge);
chrome.tabs.onRemoved.addListener(updateBadge);

async function updateBadge(): Promise<void> {
    const tabs = await chrome.tabs.query({});
    const count = String(tabs.length);
    await chrome.action.setBadgeText({ text: count });
    await chrome.action.setBadgeBackgroundColor({ color: '#1976d2' });
}

void updateBadge();
`)

write_file("src/popup/popup.tsx", `import { createRoot } from 'react-dom/client';
import { useEffect, useState } from 'react';

function Popup() {
    const [n, setN] = useState<number>(0);
    useEffect(() => {
        chrome.tabs.query({}).then(tabs => setN(tabs.length));
    }, []);
    return <div style={{ padding: 12 }}>Open tabs: <strong>{n}</strong></div>;
}

const root = document.getElementById('root');
if (!root) throw new Error('#root not found');
createRoot(root).render(<Popup />);
`)
```"""

BROWSER_EXT_FEW_SHOT_MAIN = """\
## Example: messaging between content script and background
```typescript
// src/types/index.ts
export type Msg =
    | { kind: 'getCount' }
    | { kind: 'setCount'; value: number };

export type MsgResponse =
    | { ok: true; value: number }
    | { ok: false; error: string };

// src/content/content.ts
async function reportFromPage(): Promise<void> {
    const value = document.querySelectorAll('article').length;
    const res: MsgResponse = await chrome.runtime.sendMessage(
        { kind: 'setCount', value }
    );
    if (!res.ok) console.error('extension:', res.error);
}
void reportFromPage();

// src/background.ts
import type { Msg, MsgResponse } from './types';
chrome.runtime.onMessage.addListener(
    (msg: Msg, _sender, sendResponse: (r: MsgResponse) => void) => {
        if (msg.kind === 'setCount') {
            chrome.storage.local.set({ count: msg.value })
                .then(() => sendResponse({ ok: true, value: msg.value }))
                .catch(e => sendResponse({ ok: false, error: String(e) }));
            return true; // keep channel open for async response
        }
        if (msg.kind === 'getCount') {
            chrome.storage.local.get('count')
                .then(({ count }) => sendResponse({ ok: true, value: count ?? 0 }))
                .catch(e => sendResponse({ ok: false, error: String(e) }));
            return true;
        }
        return false;
    }
);
```"""

BROWSER_EXT_FEW_SHOT_DB_PATTERN = """\
## Persistence in a Chrome extension
Use `chrome.storage` — NOT localStorage (sync API blocks the worker, and
service-worker context is unreliable for it).

```typescript
// Write
await chrome.storage.local.set({ user: { id: 1, name: 'a' } });

// Read
const { user } = await chrome.storage.local.get('user');

// Listen for changes (across popup/content/background)
chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local' && changes.user) {
        console.log('user changed:', changes.user.newValue);
    }
});
```

`storage.sync` — same API but limited to ~100KB total, syncs across the
user's signed-in Chrome installs. Use for prefs, NOT bulk data."""

BROWSER_EXT_FUNCTIONAL_TEST_GUIDANCE = """\
## Browser-extension testing (vitest + jsdom)
Mock `chrome.*` APIs since vitest doesn't ship a Chrome runtime:

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest';

beforeEach(() => {
    (globalThis as unknown as { chrome: any }).chrome = {
        tabs: { query: vi.fn().mockResolvedValue([{ id: 1 }, { id: 2 }]) },
        action: {
            setBadgeText: vi.fn().mockResolvedValue(undefined),
            setBadgeBackgroundColor: vi.fn().mockResolvedValue(undefined),
        },
        runtime: {
            sendMessage: vi.fn(),
            onMessage: { addListener: vi.fn() },
        },
        storage: {
            local: { get: vi.fn(), set: vi.fn().mockResolvedValue(undefined) },
            onChanged: { addListener: vi.fn() },
        },
    };
});

describe('updateBadge', () => {
    it('writes the tab count to the badge', async () => {
        const { updateBadge } = await import('../src/background');
        await updateBadge();
        expect(chrome.action.setBadgeText)
            .toHaveBeenCalledWith({ text: '2' });
    });
});
```

End-to-end testing in a real Chromium (`puppeteer-core` with `--load-extension`)
is possible but heavy — out of scope for the autobuilder. We validate that
the bundle compiles and the unit-level logic is sound."""

BROWSER_EXT_FEW_SHOT_NAMING = """\
## Naming conventions (browser extension)
- File names: lowercase-kebab in directories named after their role:
  `src/popup/popup.tsx`, `src/content/content.ts`, `src/background.ts`
- Message kinds: kebab-case strings: `'get-count'`, `'set-options'`.
  Tag with a discriminant `kind: '...'` field for switch-typing.
- Storage keys: snake_case strings: `'user_settings'`, `'last_seen_at'`.
- Permissions in manifest: exact strings from chrome docs:
  `'storage'`, `'activeTab'`, `'tabs'`, `'scripting'`."""

BROWSER_EXT_ITERATE_PROMPT = """\
You are iterating on a Chrome MV3 browser extension.

Tools: edit_file, write_file, line_edit, run_command, read_file.

Rules:
- Run `npx tsc --noEmit` and `npx vitest run` after edits
- Run `npx vite build` to confirm the manifest bundles cleanly
- Service worker code must NOT rely on module-level state surviving idle
  shutdown — persist via chrome.storage
- Manifest changes: keep manifest_version: 3, narrow permissions,
  no remote scripts
- Messages: structured-clone serializable only. No DOM nodes.

CURRENT ISSUES:
{validation_failures}

{code_map}"""

BROWSER_EXT_ITERATE_FEATURE_PROMPT = """\
Implement the requested feature in the existing browser extension.
- Add to the right script: background (events / cross-tab state),
  content (page DOM), popup (toolbar UI), options (preferences page)
- Update manifest.json permissions if the feature needs a new API
- Wire messages through chrome.runtime.sendMessage with typed payloads
- Add vitest unit tests for any pure-logic helpers introduced
- After: tsc clean, vitest pass, vite build clean

CURRENT ISSUES:
{validation_failures}

{instruction}

{code_map}"""


# ── PyTorch / GPU ML ─────────────────────────────────────────────────────────

PYTORCH_CODING_STANDARDS = """\
## Coding Standards (apply to ALL generated code)
- Device discipline: pick a device once at the top
  (`device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')`)
  and `.to(device)` every tensor / model that flows through training.
  Mixing CPU and GPU tensors mid-loop is a runtime crash, not a warning.
- Reproducibility: seed everything that matters at the start of train.py:
  `torch.manual_seed`, `np.random.seed`, `random.seed`,
  `torch.cuda.manual_seed_all`, `torch.backends.cudnn.deterministic = True`
  (only when reproducibility > speed).
- Model state: `model.train()` before training, `model.eval()` before
  validation/inference. Forgetting this leaves dropout/batchnorm in the
  wrong mode — silent accuracy drop.
- Gradient discipline: `optimizer.zero_grad()` BEFORE every forward pass,
  not after. `loss.backward()` then `optimizer.step()`. If you skip
  zero_grad, gradients accumulate from prior batches.
- No-grad context for inference: `with torch.no_grad(): ...` or
  `@torch.inference_mode()` decorator. Saves memory and makes intent clear.
- DataLoader: `num_workers > 0` for real datasets (pickling caveat on
  Windows / spawn-mode). `pin_memory=True` when transferring to GPU.
- Mixed precision: `torch.cuda.amp.autocast()` + `GradScaler` when training
  large models on CUDA — 1.5-2x throughput, half the memory.
- Save BOTH model state_dict AND optimizer state_dict for checkpoints
  (resuming training requires both). Save `config` / `epoch` too.
- Type-annotate tensor shapes in comments or use jaxtyping/torchtyping
  for non-trivial functions. `(B, C, H, W)` is convention; document deviations."""

PYTORCH_PROJECT_STRUCTURE = """\
## Project Structure (PyTorch training/inference project)

```
requirements.txt                # torch, torchvision, etc. — pin major versions
train.py                        # entry point: argparse, seed, train loop
eval.py                         # entry point: load checkpoint, run on test set
infer.py                        # entry point: load checkpoint, single-input demo
src/
    model.py                    # nn.Module subclass(es)
    data.py                     # Dataset + DataLoader factories
    losses.py                   # custom losses (if any)
    optim.py                    # optimizer + scheduler factory
    utils.py                    # seeding, logging, checkpoint I/O
configs/
    base.yaml                   # OmegaConf / hydra defaults
    experiment_a.yaml
checkpoints/                    # gitignored — produced at runtime
    best.pt
    last.pt
runs/                           # tensorboard logs, gitignored
tests/
    test_model.py               # forward-pass shape, gradient-flow tests
    test_data.py                # dataset __len__, __getitem__ shape/dtype
    test_overfit.py             # the canonical "1-batch overfit" test
```

**Critical setup**:
- requirements.txt should pin torch with the right CUDA/CPU variant
  (`torch==2.x.y --index-url https://download.pytorch.org/whl/cu121` or similar
  in install instructions, not requirements.txt — pip can't follow that).
- config-driven: never hardcode lr/batch/epochs in train.py. Read from
  YAML / argparse so experiments are reproducible from disk."""

PYTORCH_ANTI_PATTERNS = """\
## NEVER Do These (PyTorch mistakes)
- NEVER `.cuda()` without `if torch.cuda.is_available()` — your training
  script will crash on a CPU-only laptop where you're trying to debug it.
- NEVER mix CPU and GPU tensors. Symptoms: `Expected all tensors to be on
  the same device`. Fix: `.to(device)` every input + every model.
- NEVER call `loss.backward()` twice on the same graph without
  `retain_graph=True` — the autograd graph is consumed on first backward.
  And don't pass `retain_graph=True` reflexively; it pins memory.
- NEVER mutate parameters in-place during training (`.data = ...`)
  unless you really know why. Use `optimizer.step()`.
- NEVER forget `model.eval()` before validation — dropout/batchnorm misbehave.
- NEVER run gradient updates inside `torch.no_grad()` — gradients are
  silently zero, training appears to run but loss never moves.
- NEVER preload an entire dataset into memory in `__init__`. Subclass
  `torch.utils.data.Dataset` and load lazily in `__getitem__`.
- NEVER `print` from the inner training loop on every batch — slows
  training to a crawl on fast iterations. Log every N steps instead.
- NEVER plot a `tensor` with `.requires_grad=True` directly — `.detach()`
  first, `.cpu().numpy()` after, or matplotlib fails confusingly.
- NEVER load a checkpoint and forget to call `model.eval()` before
  inference / `model.train()` before resuming.
- NEVER train without a smoke "overfit a single batch" test. If the model
  can't drive that loss to ~0, training won't ever work — and you'll
  burn an hour of GPU time finding out."""

PYTORCH_FEW_SHOT_SCAFFOLD = """\
## Example: a minimal training loop with all the right pieces

```
write_file("src/model.py", `import torch
import torch.nn as nn

class TinyMLP(nn.Module):
    def __init__(self, in_dim: int, hidden: int, out_dim: int) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(in_dim, hidden),
            nn.ReLU(),
            nn.Linear(hidden, out_dim),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, in_dim) -> (B, out_dim)
        return self.net(x)
`)

write_file("train.py", `import argparse, random
import numpy as np
import torch
from torch.utils.data import DataLoader, TensorDataset
from src.model import TinyMLP

def seed_everything(s: int) -> None:
    random.seed(s); np.random.seed(s); torch.manual_seed(s)
    if torch.cuda.is_available(): torch.cuda.manual_seed_all(s)

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--epochs", type=int, default=5)
    p.add_argument("--batch", type=int, default=32)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--test", action="store_true",
                   help="run a 1-batch overfit smoke and exit")
    args = p.parse_args()

    seed_everything(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Synthetic linearly-separable data
    x = torch.randn(256, 8)
    y = (x.sum(dim=1) > 0).long()
    loader = DataLoader(TensorDataset(x, y), batch_size=args.batch,
                        shuffle=True, pin_memory=device.type == "cuda")

    model = TinyMLP(8, 32, 2).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    loss_fn = torch.nn.CrossEntropyLoss()

    for epoch in range(args.epochs):
        model.train()
        running = 0.0
        for xb, yb in loader:
            xb, yb = xb.to(device), yb.to(device)
            opt.zero_grad()
            logits = model(xb)
            loss = loss_fn(logits, yb)
            loss.backward()
            opt.step()
            running += loss.item()
        print(f"epoch {epoch} loss {running / len(loader):.4f}")
        if args.test: break

    torch.save({"model": model.state_dict(), "opt": opt.state_dict(),
                "epoch": args.epochs}, "checkpoints/last.pt")

if __name__ == "__main__":
    main()
`)
```"""

PYTORCH_FEW_SHOT_MAIN = """\
## Example: the canonical "overfit a single batch" smoke test

```python
# tests/test_overfit.py
import torch
from src.model import TinyMLP

def test_overfits_single_batch():
    \"\"\"If the model can't drive loss to ~0 on ONE batch in 200 steps,
    nothing about training will work. This is the cheapest signal that
    the architecture + loss + optimizer are wired correctly.\"\"\"
    torch.manual_seed(0)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = TinyMLP(8, 32, 2).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=1e-2)
    loss_fn = torch.nn.CrossEntropyLoss()

    x = torch.randn(16, 8, device=device)
    y = torch.randint(0, 2, (16,), device=device)

    losses = []
    model.train()
    for _ in range(200):
        opt.zero_grad()
        loss = loss_fn(model(x), y)
        loss.backward()
        opt.step()
        losses.append(loss.item())

    assert losses[-1] < 0.1, (
        f"model couldn't overfit a single batch in 200 steps; "
        f"final loss = {losses[-1]:.4f}. Architecture or training loop is broken."
    )
```"""

PYTORCH_FEW_SHOT_DB_PATTERN = """\
## Datasets and DataLoaders — the real shape of data IO

### Custom Dataset
```python
import torch
from torch.utils.data import Dataset
from PIL import Image
from pathlib import Path

class ImageLabelDataset(Dataset):
    def __init__(self, root: str, transform=None) -> None:
        self.paths = sorted(Path(root).glob("**/*.jpg"))
        self.transform = transform

    def __len__(self) -> int:
        return len(self.paths)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, int]:
        path = self.paths[idx]
        img = Image.open(path).convert("RGB")
        label = int(path.parent.name)
        if self.transform is not None:
            img = self.transform(img)
        return img, label
```

### DataLoader configured for GPU training
```python
loader = DataLoader(
    dataset,
    batch_size=64,
    shuffle=True,
    num_workers=4,        # parallel data loading (set to 0 if Windows or debugging)
    pin_memory=True,      # faster CPU→GPU transfer
    persistent_workers=True,  # avoid worker startup cost each epoch
    drop_last=True,       # avoid uneven last batch in BatchNorm models
)
```

### Stream from disk if dataset doesn't fit in RAM
Don't preload the whole thing in __init__. PyTorch's Dataset is designed
for lazy `__getitem__` — keep it that way."""

PYTORCH_FUNCTIONAL_TEST_GUIDANCE = """\
## PyTorch testing — the three tests that catch 80% of training bugs

1. **Shape test**: a forward pass with a known input shape produces the
   expected output shape. Catches: missing reshape, wrong layer dims.
   ```python
   def test_forward_shape():
       m = MyModel()
       y = m(torch.randn(4, 3, 224, 224))  # (B, C, H, W)
       assert y.shape == (4, 10), f"got {y.shape}"
   ```

2. **Gradient flow test**: every parameter that should be trainable HAS
   a gradient after one backward pass. Catches: detach() in the wrong
   place, frozen layers that shouldn't be frozen.
   ```python
   def test_all_params_get_grads():
       m = MyModel()
       y = m(torch.randn(4, 3, 224, 224))
       y.sum().backward()
       for name, p in m.named_parameters():
           if p.requires_grad:
               assert p.grad is not None, f"no grad for {name}"
               assert p.grad.abs().sum() > 0, f"zero grad for {name}"
   ```

3. **Overfit-single-batch test** (see PYTORCH_FEW_SHOT_MAIN). Catches:
   wrong loss, broken optimizer, learning rate ~0, wrong device, model
   in eval() mode during training.

These are pytest tests, not training runs. They should complete in <30s
each. If they need a GPU and CI doesn't have one, gate with
`@pytest.mark.skipif(not torch.cuda.is_available(), reason='needs GPU')`."""

PYTORCH_FEW_SHOT_NAMING = """\
## Naming conventions (PyTorch projects)
- Modules / classes: PascalCase: `TinyMLP`, `ResNet18`, `ImageDataset`
- Functions: snake_case: `train_one_epoch`, `seed_everything`, `load_checkpoint`
- Tensor variables: convey shape in name when non-trivial:
  `images_bchw` (batch, channels, height, width), `logits_bc` (batch, classes)
- Loss: just `loss` per step; `running_loss` / `epoch_loss` for accumulators
- Device: `device` (single torch.device), never separate cpu/gpu vars
- Files: `model.py`, `data.py`, `train.py`, `eval.py`, `infer.py`,
  `losses.py`, `utils.py` — flat, descriptive, no clever abbreviations
- Configs: `configs/<experiment_name>.yaml` — name describes what it tests"""

PYTORCH_ITERATE_PROMPT = """\
You are iterating on a PyTorch project.

Tools: edit_file, write_file, line_edit, run_command, read_file.

Rules:
- After model edits: run the shape test, the grad-flow test, and the
  overfit-single-batch test (in that order). They're cheap and they
  pinpoint where training is broken.
- Keep .to(device) discipline. CPU and GPU tensors don't mix.
- model.train() during training; model.eval() during validation/inference.
- Do not pin retain_graph=True without a comment explaining why.
- If a parameter mysteriously stops learning, check requires_grad,
  check if you're inside torch.no_grad(), check the optimizer's param_groups.

CURRENT ISSUES:
{validation_failures}

{code_map}"""

PYTORCH_ITERATE_FEATURE_PROMPT = """\
Implement the requested feature in the existing PyTorch project.
- Match the layout: model code in src/model.py, data in src/data.py,
  entry points at top level (train.py / eval.py / infer.py)
- Update tests/ if you add a new component — at minimum a shape test
- Use config (argparse / YAML) — never hardcode lr/batch/epochs
- After: pytest tests/ passes, the shape + gradient-flow + overfit
  smoke tests all pass

CURRENT ISSUES:
{validation_failures}

{instruction}

{code_map}"""


# ── Web-game / PWA addenda ────────────────────────────────────────────────────
#
# Keyword-conditional addenda concatenated onto REACT/VUE/HTML `anti_patterns`
# by languages.detect_language() when the task matches. They cover pitfalls
# that don't come up in generic form/CRUD web apps and that unit tests can't
# catch: leaked rAF loops across scene transitions, fixed-step game loops
# that double-speed on 120Hz monitors, per-frame sprite reloads, service
# workers that silently fail to register.

WEB_GAME_ANTI_PATTERNS = """\

## Anti-patterns to AVOID (canvas / game loop)
- Do NOT use `setInterval(update, 16)` for the game loop — use \
`requestAnimationFrame`. Timers stack under tab-throttling; rAF pauses \
cleanly and gives you delta-time.
- Do NOT compute movement as `x += 5` per frame. Use delta-time: \
`x += velocity_px_per_sec * dt`. Frame-based movement runs at 2× speed \
on a 120Hz monitor and stutters when the browser drops frames.
- Do NOT forget to `cancelAnimationFrame(handle)` on component unmount / \
scene transition. Leaked rAF loops keep firing forever, doubling on \
route change, until the tab dies.
- Do NOT load a sprite / texture inside the frame loop. Preload ONCE in \
init and reuse. `new Image(); img.src = "..."` inside `update()` blocks \
until decode.
- Do NOT attach `addEventListener("keydown", ...)` in a component without \
a matching `removeEventListener` in cleanup. Every mount adds one; the \
handler count grows unbounded.
- Do NOT read `ctx.getImageData()` per frame — it forces a GPU→CPU \
readback and tanks perf. Cache to an offscreen canvas.
- Do NOT use `alert()` / `confirm()` inside the loop — they pause the \
rAF cycle and produce jank.
- Do NOT block the main thread with a physics step over 8ms — split it \
across frames or move to a Worker.

## Anti-patterns to AVOID (PWA)
- Do NOT register a service worker without a `.catch()` — silent \
registration failures leave PWAs broken with no signal.
- Do NOT cache the app shell without a version cache-name — you'll ship \
a fresh version and users will keep hitting the old one until the SW's \
TTL expires.
- Do NOT rely on `beforeinstallprompt` being available in production \
without a user gesture — Chrome throttles it aggressively.
- Do NOT put `Cache-Control: no-store` on the service worker file — it's \
already never cached longer than 24h by browsers; setting no-store on \
top forces the update path through re-download.
- Do NOT reference assets in the manifest that don't exist. A missing \
icon breaks the install prompt silently.
"""


# Task keywords that trigger the web-game addendum.
_WEB_GAME_KEYWORDS = frozenset({
    "phaser", "three.js", "threejs", "pixi", "pixijs", "babylon", "babylonjs",
    "canvas game", "webgl", "game loop", "html5 game", "browser game",
    "arcade game", "shooter", "platformer", "roguelike browser",
    "web game", "pwa game", "html canvas",
})

# Task keywords that trigger the PWA addendum (superset can be either).
_PWA_KEYWORDS = frozenset({
    "pwa", "progressive web app", "service worker", "installable web app",
    "offline-capable web", "web app manifest",
})


def is_web_game_task(task: str) -> bool:
    """True when the task suggests a canvas / WebGL / browser-game surface."""
    t = task.lower()
    return any(kw in t for kw in _WEB_GAME_KEYWORDS)


def is_pwa_task(task: str) -> bool:
    """True when the task suggests a PWA (service worker, offline, install prompt)."""
    t = task.lower()
    return any(kw in t for kw in _PWA_KEYWORDS)


def web_game_addendum(task: str) -> str:
    """Return the anti-pattern addendum for game/PWA tasks, or empty string.

    Concatenated onto a Language's `anti_patterns` string by
    `languages.detect_language()` when the task matches. Empty string means
    "no addendum needed" — the base language block is sufficient.
    """
    if is_web_game_task(task) or is_pwa_task(task):
        return WEB_GAME_ANTI_PATTERNS
    return ""
