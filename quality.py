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
