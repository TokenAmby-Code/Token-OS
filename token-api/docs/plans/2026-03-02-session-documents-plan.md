# Session Documents Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add persistent Obsidian session documents as first-class entities in Token-API — linked to instances, updated by Claude agents and Minimax swarms, with TUI quick-note input.

**Architecture:** New `session_documents` DB table with absolute file paths. Many-to-one relationship from `claude_instances` via FK. Central merge primitive (LLM-powered) handles all document updates. Minimax swarm fires on stop hooks for activity logging and verification.

**Tech Stack:** FastAPI (existing), aiosqlite (existing), httpx + Minimax Anthropic-compatible API (existing pattern in `post_run_graph.py`), Rich TUI (existing)

**Design Doc:** `docs/plans/2026-03-02-session-documents-design.md`

---

## Task 1: DB Migration — session_documents Table + FK Column

**Files:**
- Modify: `init_db.py:196-197` (after guard_runs CREATE TABLE)
- Modify: `init_db.py:60-62` (after last ALTER TABLE migration)
- Test: Manual — run `python3 init_db.py` and verify schema

**Step 1: Add session_documents CREATE TABLE to init_db.py**

After the `guard_runs` table creation (line 197), add:

```python
    # Create session_documents table (persistent Obsidian notes linked to instances)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_documents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path   TEXT NOT NULL UNIQUE,
            title       TEXT,
            project     TEXT,
            status      TEXT DEFAULT 'active',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
```

**Step 2: Add session_doc_id migration to claude_instances**

After the `tts_mode` migration (line 62), add:

```python
    if 'session_doc_id' not in columns:
        cursor.execute("ALTER TABLE claude_instances ADD COLUMN session_doc_id INTEGER")
```

Note: SQLite ALTER TABLE does not support REFERENCES constraints, so the FK is enforced at the application level.

**Step 3: Verify migration**

Run:
```bash
cd ~/Scripts/token-api && python3 -c "
import sqlite3, os
conn = sqlite3.connect(os.path.expanduser('~/.claude/agents.db'))
c = conn.cursor()
# Check session_documents table exists
c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='session_documents'\")
print('session_documents table:', 'EXISTS' if c.fetchone() else 'MISSING')
# Check session_doc_id column exists
c.execute('PRAGMA table_info(claude_instances)')
cols = [col[1] for col in c.fetchall()]
print('session_doc_id column:', 'EXISTS' if 'session_doc_id' in cols else 'MISSING')
conn.close()
"
```
Expected: Both print EXISTS

**Step 4: Also add migration in main.py startup**

The main.py `startup` function also runs migrations. Find the migration block in main.py (search for `PRAGMA table_info(claude_instances)`) and add the same `session_doc_id` migration there, plus the `CREATE TABLE IF NOT EXISTS session_documents` statement.

**Step 5: Commit**

```bash
git add init_db.py main.py
git commit -m "feat: add session_documents table and session_doc_id FK column"
```

---

## Task 2: Session Document CRUD Endpoints

**Files:**
- Modify: `main.py` — add Pydantic models + 6 endpoints in a new section
- Test: Manual curl tests

**Step 1: Add Pydantic models**

Near the other request models (around line 201 in main.py, after `InstanceRegisterRequest`), add:

```python
class SessionDocCreateRequest(BaseModel):
    title: str
    project: Optional[str] = None
    file_path: Optional[str] = None  # Absolute path; auto-generated if omitted

class SessionDocUpdateRequest(BaseModel):
    title: Optional[str] = None
    project: Optional[str] = None
    status: Optional[str] = None  # active | completed | archived
```

**Step 2: Add DEFAULT_SESSIONS_DIR config**

Near the top of main.py with other path constants:

```python
DEFAULT_SESSIONS_DIR = Path.home() / "Token-ENV" / "Sessions"
```

**Step 3: Add helper to generate session doc file**

```python
def create_session_doc_file(file_path: Path, title: str, doc_id: int, project: str = None) -> None:
    """Create the markdown file for a session document."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    agents_yaml = "[]"
    project_line = f"\nproject: {project}" if project else ""
    content = f"""---
session_doc_id: {doc_id}
created: {today}{project_line}
agents: {agents_yaml}
status: active
---

# Session: {title}

## Plan

_No plan defined yet._

## Activity Log

"""
    file_path.write_text(content)
```

**Step 4: Add the 6 CRUD endpoints**

Add a new endpoint section in main.py (after the task endpoints around line 5600):

```python
# ============ Session Document Endpoints ============

@app.post("/api/session-docs")
async def create_session_doc(request: SessionDocCreateRequest):
    """Create a new session document."""
    if request.file_path:
        fp = Path(request.file_path)
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        slug = request.title.lower().replace(" ", "-")[:50]
        fp = DEFAULT_SESSIONS_DIR / f"{today}-{slug}.md"

    if fp.exists():
        raise HTTPException(400, f"File already exists: {fp}")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO session_documents (file_path, title, project) VALUES (?, ?, ?)",
            (str(fp), request.title, request.project)
        )
        doc_id = cursor.lastrowid
        await db.commit()

    create_session_doc_file(fp, request.title, doc_id, request.project)
    await log_event("session_doc_created", details={"doc_id": doc_id, "title": request.title, "file_path": str(fp)})
    return {"id": doc_id, "file_path": str(fp), "title": request.title}


@app.get("/api/session-docs")
async def list_session_docs(status: str = None, project: str = None):
    """List session documents with optional filters."""
    query = "SELECT * FROM session_documents WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if project:
        query += " AND project = ?"
        params.append(project)
    query += " ORDER BY updated_at DESC"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@app.get("/api/session-docs/{doc_id}")
async def get_session_doc(doc_id: int):
    """Get session doc metadata + linked instances."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM session_documents WHERE id = ?", (doc_id,))
        doc = await cursor.fetchone()
        if not doc:
            raise HTTPException(404, "Session document not found")

        cursor = await db.execute(
            "SELECT id, tab_name, status, working_dir FROM claude_instances WHERE session_doc_id = ?",
            (doc_id,)
        )
        instances = await cursor.fetchall()
    return {**dict(doc), "instances": [dict(i) for i in instances]}


@app.get("/api/session-docs/{doc_id}/content")
async def get_session_doc_content(doc_id: int):
    """Read the actual file content of a session document."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT file_path FROM session_documents WHERE id = ?", (doc_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Session document not found")

    fp = Path(row[0])
    if not fp.exists():
        raise HTTPException(404, f"File not found: {fp}")
    return {"file_path": str(fp), "content": fp.read_text()}


@app.patch("/api/session-docs/{doc_id}")
async def update_session_doc(doc_id: int, request: SessionDocUpdateRequest):
    """Update session doc metadata."""
    updates = []
    params = []
    for field in ("title", "project", "status"):
        val = getattr(request, field)
        if val is not None:
            updates.append(f"{field} = ?")
            params.append(val)
    if not updates:
        raise HTTPException(400, "No fields to update")

    updates.append("updated_at = ?")
    params.append(datetime.now().isoformat())
    params.append(doc_id)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE session_documents SET {', '.join(updates)} WHERE id = ?", params)
        await db.commit()
    return {"status": "updated", "doc_id": doc_id}


@app.delete("/api/session-docs/{doc_id}")
async def delete_session_doc(doc_id: int, hard: bool = False):
    """Archive or hard-delete a session document."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT file_path FROM session_documents WHERE id = ?", (doc_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Session document not found")

        if hard:
            await db.execute("UPDATE claude_instances SET session_doc_id = NULL WHERE session_doc_id = ?", (doc_id,))
            await db.execute("DELETE FROM session_documents WHERE id = ?", (doc_id,))
            fp = Path(row[0])
            if fp.exists():
                fp.unlink()
        else:
            await db.execute(
                "UPDATE session_documents SET status = 'archived', updated_at = ? WHERE id = ?",
                (datetime.now().isoformat(), doc_id)
            )
        await db.commit()
    return {"status": "deleted" if hard else "archived", "doc_id": doc_id}
```

**Step 5: Test with curl**

```bash
# Create
curl -s -X POST http://localhost:7777/api/session-docs \
  -H "Content-Type: application/json" \
  -d '{"title": "test-session", "project": "token-api"}' | jq .

# List
curl -s http://localhost:7777/api/session-docs | jq .

# Get by ID
curl -s http://localhost:7777/api/session-docs/1 | jq .

# Read content
curl -s http://localhost:7777/api/session-docs/1/content | jq .content
```

Expected: All return valid JSON with correct data.

**Step 6: Commit**

```bash
git add main.py
git commit -m "feat: add session document CRUD endpoints"
```

---

## Task 3: Instance-Doc Linking Endpoints

**Files:**
- Modify: `main.py` — add 3 linking endpoints after the CRUD endpoints

**Step 1: Add assign-doc endpoint**

```python
@app.post("/api/instances/{instance_id}/assign-doc")
async def assign_session_doc(instance_id: str, doc_id: int):
    """Assign an instance to an existing session document."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Verify doc exists
        cursor = await db.execute("SELECT id, title, file_path FROM session_documents WHERE id = ?", (doc_id,))
        doc = await cursor.fetchone()
        if not doc:
            raise HTTPException(404, "Session document not found")

        # Verify instance exists
        cursor = await db.execute("SELECT id, tab_name, session_doc_id FROM claude_instances WHERE id = ?", (instance_id,))
        instance = await cursor.fetchone()
        if not instance:
            raise HTTPException(404, "Instance not found")

        old_doc_id = instance[2]

        # Update instance
        await db.execute(
            "UPDATE claude_instances SET session_doc_id = ? WHERE id = ?",
            (doc_id, instance_id)
        )

        # Update agents list in doc frontmatter
        tab_name = instance[1] or instance_id[:12]
        await _update_doc_agents_list(db, doc_id)
        await db.commit()

    # Handle orphan cleanup for old doc if needed
    if old_doc_id and old_doc_id != doc_id:
        await _handle_orphan_doc(old_doc_id)

    await log_event("session_doc_assigned", instance_id=instance_id,
                    details={"doc_id": doc_id, "title": doc[1]})
    return {"status": "assigned", "doc_id": doc_id, "file_path": doc[2]}
```

**Step 2: Add create-doc endpoint**

```python
@app.post("/api/instances/{instance_id}/create-doc")
async def create_and_assign_doc(instance_id: str, request: SessionDocCreateRequest):
    """Create a new session doc and assign it to this instance."""
    # Verify instance exists
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id, tab_name, session_doc_id FROM claude_instances WHERE id = ?", (instance_id,))
        instance = await cursor.fetchone()
        if not instance:
            raise HTTPException(404, "Instance not found")
        old_doc_id = instance[2]

    # Create the doc (reuse CRUD endpoint logic)
    result = await create_session_doc(request)
    doc_id = result["id"]

    # Assign to instance
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE claude_instances SET session_doc_id = ? WHERE id = ?",
            (doc_id, instance_id)
        )
        await _update_doc_agents_list(db, doc_id)
        await db.commit()

    if old_doc_id:
        await _handle_orphan_doc(old_doc_id)

    await log_event("session_doc_created_assigned", instance_id=instance_id,
                    details={"doc_id": doc_id, "title": request.title})
    return {**result, "instance_id": instance_id}
```

**Step 3: Add unassign-doc endpoint**

```python
@app.delete("/api/instances/{instance_id}/unassign-doc")
async def unassign_session_doc(instance_id: str):
    """Unlink an instance from its session document."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT session_doc_id FROM claude_instances WHERE id = ?", (instance_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Instance not found")
        old_doc_id = row[0]
        if not old_doc_id:
            return {"status": "no_doc_assigned"}

        await db.execute(
            "UPDATE claude_instances SET session_doc_id = NULL WHERE id = ?",
            (instance_id,)
        )
        await _update_doc_agents_list(db, old_doc_id)
        await db.commit()

    await _handle_orphan_doc(old_doc_id)
    return {"status": "unassigned", "old_doc_id": old_doc_id}
```

**Step 4: Add helper functions**

```python
async def _update_doc_agents_list(db, doc_id: int) -> None:
    """Update the agents list in a session doc's YAML frontmatter."""
    cursor = await db.execute(
        "SELECT tab_name FROM claude_instances WHERE session_doc_id = ? AND status IN ('processing', 'idle')",
        (doc_id,)
    )
    rows = await cursor.fetchall()
    agents = [r[0] for r in rows if r[0]]

    cursor = await db.execute("SELECT file_path FROM session_documents WHERE id = ?", (doc_id,))
    doc_row = await cursor.fetchone()
    if not doc_row:
        return

    fp = Path(doc_row[0])
    if not fp.exists():
        return

    content = fp.read_text()
    # Update agents list in YAML frontmatter
    import re
    content = re.sub(
        r'^agents:.*$',
        f'agents: [{", ".join(agents)}]',
        content,
        count=1,
        flags=re.MULTILINE
    )
    fp.write_text(content)


SESSION_DOC_TEMPLATE_HASH = None  # Set on first doc creation for comparison

async def _handle_orphan_doc(doc_id: int) -> None:
    """Handle cleanup when a doc loses all linked instances."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Check if any instances still linked
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE session_doc_id = ?",
            (doc_id,)
        )
        count = (await cursor.fetchone())[0]
        if count > 0:
            return  # Not orphaned

        # Get doc info
        cursor = await db.execute("SELECT file_path, title FROM session_documents WHERE id = ?", (doc_id,))
        row = await cursor.fetchone()
        if not row:
            return

        fp = Path(row[0])
        if not fp.exists():
            return

        content = fp.read_text()
        # Check if unedited (still has the template placeholder)
        if "_No plan defined yet._" in content and "## Activity Log\n\n" in content.rstrip():
            # Unedited — delete file and DB row
            fp.unlink()
            await db.execute("DELETE FROM session_documents WHERE id = ?", (doc_id,))
            await db.commit()
            logger.info(f"Orphan cleanup: deleted unedited session doc {doc_id} ({row[1]})")
        else:
            # Edited — archive it
            await db.execute(
                "UPDATE session_documents SET status = 'archived', updated_at = ? WHERE id = ?",
                (datetime.now().isoformat(), doc_id)
            )
            await db.commit()
            logger.info(f"Orphan cleanup: archived edited session doc {doc_id} ({row[1]})")
```

**Step 5: Test linking**

```bash
# Get an active instance ID
INST=$(curl -s http://localhost:7777/api/instances | jq -r '.[0].id')

# Create and assign
curl -s -X POST "http://localhost:7777/api/instances/$INST/create-doc" \
  -H "Content-Type: application/json" \
  -d '{"title": "test-linking"}' | jq .

# Verify assignment
curl -s "http://localhost:7777/api/instances" | jq '.[0].session_doc_id'
```

**Step 6: Commit**

```bash
git add main.py
git commit -m "feat: add instance-doc linking endpoints with orphan cleanup"
```

---

## Task 4: Merge Primitive

**Files:**
- Modify: `main.py` — add merge endpoint + Minimax client helper
- Reference: `post_run_graph.py:24-35` — Minimax API pattern

**Step 1: Add Minimax client helper in main.py**

Near the top of main.py with other config/helpers:

```python
_MINIMAX_BASE_URL = "https://api.minimax.io/anthropic"
_MINIMAX_MODEL = "MiniMax-M2.5"
_MINIMAX_AUTH_PROFILES = Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"

def _get_minimax_key() -> str:
    """Read MiniMax API key from auth-profiles."""
    try:
        profiles = json.loads(_MINIMAX_AUTH_PROFILES.read_text())
        return profiles["profiles"]["minimax:default"]["key"]
    except Exception as e:
        raise RuntimeError(f"Could not load MiniMax API key: {e}")

async def minimax_chat(system_prompt: str, user_content: str, max_tokens: int = 1024) -> str:
    """Send a chat message to Minimax and return the text response."""
    key = _get_minimax_key()
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{_MINIMAX_BASE_URL}/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _MINIMAX_MODEL,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_content}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return "".join(
            block["text"] for block in data.get("content", [])
            if block.get("type") == "text"
        )
```

**Step 2: Add merge request model**

```python
class SessionDocMergeRequest(BaseModel):
    content: str
    source: str = "agent"  # tui | agent | minimax
    context: Optional[str] = None  # hint about what this content is
```

**Step 3: Add merge endpoint**

```python
@app.post("/api/session-docs/{doc_id}/merge")
async def merge_into_session_doc(doc_id: int, request: SessionDocMergeRequest):
    """Intelligently merge content into a session document using LLM."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT file_path, title FROM session_documents WHERE id = ?", (doc_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Session document not found")

    fp = Path(row[0])
    if not fp.exists():
        raise HTTPException(404, f"File not found: {fp}")

    current_content = fp.read_text()
    context_hint = f"\nContext: {request.context}" if request.context else ""

    system_prompt = f"""You are a document editor for a session planning document. You will receive the current document and new content to merge in.

Rules:
- If the new content is an activity update or progress note, add it to the Activity Log section as a new entry with today's date and time.
- If the new content contains architectural decisions or plan changes, update the Plan section.
- If the new content is a quick note or thought, place it where it makes most sense.
- Preserve ALL existing content. Do not remove or summarize existing entries.
- Use markdown formatting. Activity log entries use ### headers with date and agent name.
- Return the COMPLETE updated document, including frontmatter.
- Do NOT add commentary or explanation outside the document."""

    user_msg = f"""Current document:
```markdown
{current_content}
```

New content to merge ({request.source} source{context_hint}):
```
{request.content}
```

Return the complete updated document."""

    try:
        updated = await minimax_chat(system_prompt, user_msg, max_tokens=4096)

        # Strip markdown code fences if the LLM wrapped it
        if updated.startswith("```"):
            lines = updated.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            updated = "\n".join(lines)

        fp.write_text(updated)

        # Update timestamp in DB
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE session_documents SET updated_at = ? WHERE id = ?",
                (datetime.now().isoformat(), doc_id)
            )
            await db.commit()

        await log_event("session_doc_merged", details={
            "doc_id": doc_id, "source": request.source, "content_length": len(request.content)
        })
        return {"status": "merged", "doc_id": doc_id, "source": request.source}

    except Exception as e:
        logger.error(f"Session doc merge failed for doc {doc_id}: {e}")
        raise HTTPException(500, f"Merge failed: {e}")
```

**Step 4: Test merge**

```bash
# Merge a quick note
curl -s -X POST http://localhost:7777/api/session-docs/1/merge \
  -H "Content-Type: application/json" \
  -d '{"content": "Started working on the DB migration. Tables created successfully.", "source": "agent", "context": "Task 1 progress update"}' | jq .

# Verify content was updated
curl -s http://localhost:7777/api/session-docs/1/content | jq .content
```

**Step 5: Commit**

```bash
git add main.py
git commit -m "feat: add merge primitive with Minimax LLM integration"
```

---

## Task 5: instance-name CLI --session Flag

**Files:**
- Modify: `~/Scripts/cli-tools/bin/instance-name`

**Step 1: Add --session flag parsing**

In the argument parsing section (after line 57), extend to handle `--session`, `--session-id`, and `--path`:

```bash
# Parse arguments
NAME=""
EXPLICIT_ID=""
SESSION_FLAG=""
SESSION_ID=""
SESSION_PATH=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --id)
            EXPLICIT_ID="$2"
            shift 2
            ;;
        --session)
            if [[ "${2:-}" =~ ^[0-9]+$ ]] || [[ "${2:-}" == --* ]] || [[ -z "${2:-}" ]]; then
                SESSION_FLAG="create"
                shift
            else
                SESSION_FLAG="assign-fuzzy"
                SESSION_ID="$2"
                shift 2
            fi
            ;;
        --session-id)
            SESSION_FLAG="assign-id"
            SESSION_ID="$2"
            shift 2
            ;;
        --path)
            SESSION_PATH="$2"
            shift 2
            ;;
        *)
            NAME="$1"
            shift
            ;;
    esac
done
```

**Step 2: Add session doc logic after successful rename**

After the successful rename echo (line 143), add:

```bash
# Handle session document if requested
if [[ -n "$SESSION_FLAG" ]]; then
    case "$SESSION_FLAG" in
        create)
            # Create new doc and assign
            CREATE_BODY="{\"title\": \"$NAME\"}"
            if [[ -n "$SESSION_PATH" ]]; then
                FULL_PATH="${SESSION_PATH%/}/${NAME}.md"
                CREATE_BODY="{\"title\": \"$NAME\", \"file_path\": \"$FULL_PATH\"}"
            fi
            SESSION_RESP=$(curl -s -X POST "$API_URL/api/instances/$INSTANCE_ID/create-doc" \
                -H "Content-Type: application/json" \
                -d "$CREATE_BODY" 2>/dev/null || true)
            if echo "$SESSION_RESP" | jq -e '.id' >/dev/null 2>&1; then
                DOC_PATH=$(echo "$SESSION_RESP" | jq -r '.file_path')
                echo "Session doc: $DOC_PATH"
            else
                echo "Warning: session doc creation failed" >&2
            fi
            ;;
        assign-id)
            # Assign to existing doc by ID
            ASSIGN_RESP=$(curl -s -X POST "$API_URL/api/instances/$INSTANCE_ID/assign-doc?doc_id=$SESSION_ID" 2>/dev/null || true)
            if echo "$ASSIGN_RESP" | jq -e '.status == "assigned"' >/dev/null 2>&1; then
                DOC_PATH=$(echo "$ASSIGN_RESP" | jq -r '.file_path')
                echo "Assigned to session: $DOC_PATH"
            else
                echo "Warning: session doc assignment failed" >&2
            fi
            ;;
        assign-fuzzy)
            # Find doc by title (fuzzy match) and assign
            DOCS=$(curl -s "$API_URL/api/session-docs?status=active" 2>/dev/null || true)
            DOC_ID=$(echo "$DOCS" | jq -r --arg t "$SESSION_ID" \
                '[.[] | select(.title | ascii_downcase | contains($t | ascii_downcase))] | .[0].id // ""' 2>/dev/null || true)
            if [[ -n "$DOC_ID" && "$DOC_ID" != "null" ]]; then
                ASSIGN_RESP=$(curl -s -X POST "$API_URL/api/instances/$INSTANCE_ID/assign-doc?doc_id=$DOC_ID" 2>/dev/null || true)
                DOC_PATH=$(echo "$ASSIGN_RESP" | jq -r '.file_path // "unknown"')
                echo "Assigned to session: $DOC_PATH"
            else
                echo "Warning: no session doc matching '$SESSION_ID' found" >&2
            fi
            ;;
    esac
fi
```

**Step 3: Update help text**

Update the help section to include session flags.

**Step 4: Test**

```bash
# Create session + rename
instance-name "test-session" --session

# Assign to existing
instance-name "another-agent" --session "test-session"
```

**Step 5: Commit**

```bash
git add ~/Scripts/cli-tools/bin/instance-name
git commit -m "feat: add --session flag to instance-name CLI"
```

---

## Task 6: TUI — Session Doc Display + Quick Note Keybind

**Files:**
- Modify: `token-api-tui.py:1730-1731` — add session doc line in detail panel
- Modify: `token-api-tui.py:2960-3030` — add 'n' keybind
- Modify: `token-api-tui.py:3155-3186` — add note input handler (modeled on rename)

**Step 1: Add session doc line to instance detail panel**

After the `Dir:` line (line 1730), add:

```python
    # Session document display
    session_doc_id = instance.get("session_doc_id")
    if session_doc_id:
        # Fetch doc title from DB (or cache)
        try:
            doc_cursor = conn.execute(
                "SELECT title, file_path FROM session_documents WHERE id = ?",
                (session_doc_id,)
            )
            doc_row = doc_cursor.fetchone()
            if doc_row:
                doc_title = doc_row[0] or "untitled"
                doc_path = doc_row[1]
                # Shorten path for display
                short_path = doc_path.replace(str(Path.home()), "~")
                if len(short_path) > 50:
                    short_path = "..." + short_path[-47:]
                lines.append(f"[cyan]Session:[/cyan] [bold]{doc_title}[/bold]  [dim]{short_path}[/dim]")
        except Exception:
            lines.append(f"[cyan]Session:[/cyan] [dim]doc #{session_doc_id}[/dim]")
```

Note: The TUI queries the DB directly (not via API), so this uses the sqlite3 connection `conn` already available in the TUI's data-fetching code. The implementer should check how `conn` is obtained in the TUI context — likely via the `get_instances()` function which returns dicts. The `session_doc_id` must be included in the SELECT query that populates instance dicts.

**Step 2: Ensure session_doc_id is in instance SELECT queries**

Find the `get_instances()` function in the TUI and ensure the SELECT includes `session_doc_id`. It likely uses `SELECT *` so it should already be included after the migration.

**Step 3: Add 'n' keybind to dispatcher**

After the 'm' keybind handler (around line 3027), add:

```python
                    elif key == 'n':
                        with action_lock:
                            action_queue.append('session_note')
                        update_flag.set()
```

**Step 4: Add note input handler in action processing section**

After the rename action handler (around line 3186), add a similar block:

```python
                    if action == 'session_note' and displayed and table_mode == "instances":
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id")
                            session_doc_id = instance.get("session_doc_id")

                            if not session_doc_id:
                                # No session doc — briefly show message
                                input_mode.set()
                                time.sleep(0.1)
                                live.stop()
                                console.print("[yellow]No session doc linked. Use instance-name --session to create one.[/yellow]")
                                time.sleep(1.5)
                                live.start()
                                input_mode.clear()
                                _refresh(live)
                                continue

                            input_mode.set()
                            time.sleep(0.1)
                            live.stop()

                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)

                            console.print(f"\n[yellow]Session note for:[/yellow] {format_instance_name(instance)}")
                            try:
                                note = Prompt.ask("Note")
                                if note and note.strip():
                                    # Fire merge request
                                    try:
                                        resp = requests.post(
                                            f"{API_URL}/api/session-docs/{session_doc_id}/merge",
                                            json={"content": note.strip(), "source": "tui", "context": "Quick note from TUI"},
                                            timeout=30
                                        )
                                        if resp.status_code == 200:
                                            console.print("[green]v[/green] Note merged into session doc")
                                        else:
                                            console.print(f"[red]x[/red] Merge failed: {resp.text}")
                                    except Exception as e:
                                        console.print(f"[red]x[/red] Merge request failed: {e}")
                                else:
                                    console.print("[dim]Cancelled[/dim]")
                            except (KeyboardInterrupt, EOFError):
                                console.print("[dim]Cancelled[/dim]")

                            time.sleep(0.3)
                            tty.setcbreak(sys.stdin.fileno())
                            input_mode.clear()
                            live.start()
                            _refresh(live)
```

**Step 5: Update TUI help/status bar to show 'n' keybind**

Find the status bar or help text that lists keybinds and add `n:note` to it.

**Step 6: Test**

Run the TUI, select an instance with a session doc, press `n`, type a note, press Enter. Verify the note appears in the Obsidian file.

**Step 7: Commit**

```bash
git add token-api-tui.py
git commit -m "feat: TUI session doc display + quick note keybind"
```

---

## Task 7: Session Update Skill

**Files:**
- Create: `~/.claude/plugins/cache/claude-plugins-official/superpowers/4.3.1/skills/session-update/session-update.md` (or wherever skills live in the plugin system)

**Step 1: Create the skill file**

The skill should be created using the `superpowers:writing-skills` skill. Content outline:

```markdown
name: session-update
description: Update this session's persistent session document with progress, decisions, and plan revisions.

## When to Use
- After completing a significant task or milestone
- When architectural decisions are made
- When the plan needs revision based on new findings
- Before ending a session

## Process
1. Read session doc via API: GET /api/session-docs/{doc_id}/content
2. Read current task list (TaskList tool)
3. Read recent git log: git log --oneline -10
4. Draft update content with:
   - Activity log entry (what was done, decisions made)
   - Plan revisions if needed
5. POST /api/session-docs/{doc_id}/merge with source="agent"
```

**Step 2: Determine how to discover the current instance's session_doc_id**

The skill needs to know which session doc this instance is linked to. Options:
- Query the API: `GET /api/instances` filtered by current instance ID (from `TOKEN_API_INSTANCE_ID` env var)
- Or add a convenience endpoint: `GET /api/instances/{id}/session-doc`

Add convenience endpoint in main.py:

```python
@app.get("/api/instances/{instance_id}/session-doc")
async def get_instance_session_doc(instance_id: str):
    """Get the session document linked to this instance."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT session_doc_id FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Instance not found")
        if not row[0]:
            return {"session_doc_id": None}

        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM session_documents WHERE id = ?",
            (row[0],)
        )
        doc = await cursor.fetchone()
        if not doc:
            return {"session_doc_id": None}
        return dict(doc)
```

**Step 3: Commit**

```bash
git add main.py
git commit -m "feat: add session-update skill and instance session-doc lookup endpoint"
```

---

## Task 8: Minimax Swarm on Stop Hook

**Files:**
- Modify: `main.py:7693-7759` — add swarm trigger in `handle_stop`

**Step 1: Add swarm roles config**

Near the Minimax config section:

```python
SESSION_SWARM_ROLES = {
    "activity_scribe": {
        "system": "You are an Activity Scribe. Given an agent's recent output, write a concise activity log entry. Format: ### YYYY-MM-DD HH:MM — <agent_name>\n<2-3 sentences of what was done>. Include specific file names, decisions made, and outcomes. Be factual, not flowery.",
        "max_tokens": 512,
    },
    "plan_auditor": {
        "system": "You are a Plan Auditor. Given a session document and recent activity, identify if any part of the Plan section needs updating based on what just happened. If no updates needed, respond with exactly: NO_UPDATE. Otherwise, describe the specific plan changes needed in 2-3 sentences.",
        "max_tokens": 512,
    },
}
```

**Step 2: Add swarm fire function**

```python
async def fire_session_doc_swarm(session_doc_id: int, instance_tab_name: str, context: str = "") -> None:
    """Fire Minimax agents to update session doc after a stop event."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT file_path FROM session_documents WHERE id = ?", (session_doc_id,))
            row = await cursor.fetchone()
            if not row:
                return
        fp = Path(row[0])
        if not fp.exists():
            return
        doc_content = fp.read_text()

        # Activity Scribe — summarize what happened
        scribe_config = SESSION_SWARM_ROLES["activity_scribe"]
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        scribe_prompt = f"""Session document:
{doc_content[:2000]}

Recent agent activity context:
{context[:2000]}

Agent name: {instance_tab_name}
Current time: {now}

Write the activity log entry."""

        scribe_result = await minimax_chat(scribe_config["system"], scribe_prompt, scribe_config["max_tokens"])

        if scribe_result.strip():
            await merge_into_session_doc(
                session_doc_id,
                SessionDocMergeRequest(content=scribe_result, source="minimax", context="Activity scribe update")
            )

        # Plan Auditor — check if plan needs updating
        auditor_config = SESSION_SWARM_ROLES["plan_auditor"]
        auditor_prompt = f"""Session document:
{doc_content[:2000]}

Recent activity just logged:
{scribe_result[:500]}

Does the Plan section need any updates based on this activity?"""

        auditor_result = await minimax_chat(auditor_config["system"], auditor_prompt, auditor_config["max_tokens"])

        if auditor_result.strip() and "NO_UPDATE" not in auditor_result:
            await merge_into_session_doc(
                session_doc_id,
                SessionDocMergeRequest(content=f"Plan audit note: {auditor_result}", source="minimax", context="Plan auditor finding")
            )

        logger.info(f"Session swarm completed for doc {session_doc_id}")

    except Exception as e:
        logger.error(f"Session swarm failed for doc {session_doc_id}: {e}")
```

**Step 3: Add swarm trigger in handle_stop**

In `handle_stop()`, after the idle status update (around line 7728), before the subagent check, add:

```python
    # Fire session doc swarm if instance has a linked doc
    session_doc_id = instance.get("session_doc_id")
    if session_doc_id and not is_subagent_instance:
        # Fire in background — don't block the stop response
        stop_context = payload.get("transcript_summary", "")[:2000]
        asyncio.create_task(fire_session_doc_swarm(
            session_doc_id, tab_name, context=stop_context
        ))
```

Note: `transcript_summary` may not exist in the payload. The implementer should check what data is available in the stop hook payload and use whatever context is present (could be `payload.get("last_tool_result")` or similar).

**Step 4: Test**

Trigger a stop event on an instance with a session doc. Check if the Obsidian file gets updated with an activity log entry.

**Step 5: Commit**

```bash
git add main.py
git commit -m "feat: Minimax swarm fires on stop hook for session doc updates"
```

---

## Task 9: Minimax Rate Limiter

**Files:**
- Modify: `main.py` — add rate limiter around Minimax calls

**Step 1: Add sliding window rate limiter**

```python
import collections

class MiniMaxRateLimiter:
    """Sliding window rate limiter for MiniMax API calls."""
    def __init__(self, max_calls: int = 300, window_seconds: int = 18000):  # 300 per 5 hours
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.calls: collections.deque = collections.deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """Return True if a call can be made, False if rate limited."""
        async with self._lock:
            now = time.time()
            # Remove expired entries
            while self.calls and self.calls[0] < now - self.window_seconds:
                self.calls.popleft()
            # Check budget (80% threshold for backoff)
            if len(self.calls) >= int(self.max_calls * 0.8):
                return False
            self.calls.append(now)
            return True

    @property
    def remaining(self) -> int:
        now = time.time()
        while self.calls and self.calls[0] < now - self.window_seconds:
            self.calls.popleft()
        return max(0, self.max_calls - len(self.calls))

minimax_limiter = MiniMaxRateLimiter()
```

**Step 2: Wrap minimax_chat with rate limiting**

Update `minimax_chat` to check the limiter:

```python
async def minimax_chat(system_prompt: str, user_content: str, max_tokens: int = 1024) -> str:
    if not await minimax_limiter.acquire():
        logger.warning(f"MiniMax rate limited ({minimax_limiter.remaining} remaining)")
        return ""
    # ... rest of existing implementation
```

**Step 3: Add rate limit status endpoint**

```python
@app.get("/api/minimax/status")
async def minimax_rate_status():
    return {"remaining": minimax_limiter.remaining, "max": minimax_limiter.max_calls}
```

**Step 4: Commit**

```bash
git add main.py
git commit -m "feat: add MiniMax rate limiter for session doc swarms"
```

---

## Summary

| Task | What | Key Files |
|------|------|-----------|
| 1 | DB migration | init_db.py, main.py |
| 2 | CRUD endpoints | main.py |
| 3 | Instance linking | main.py |
| 4 | Merge primitive | main.py |
| 5 | CLI --session flag | cli-tools/bin/instance-name |
| 6 | TUI display + note | token-api-tui.py |
| 7 | Session update skill | skills/, main.py |
| 8 | Minimax stop hook swarm | main.py |
| 9 | Rate limiter | main.py |

Tasks 1-4 are the critical path. Tasks 5-6 are UX. Tasks 7-9 are the Minimax swarm layer. Each task is independently committable.
