"""Process-isolated adapter over an installed memory plugin, never a new store.

Use that plugin's Python environment. Its generic module names (config, index,
providers) must not share a process with other independently installed plugins.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


TEMPLATE = """## User corrections and preferences
<Only explicit user statements; quote the important constraint. Say none when absent.>
## Work and evidence
<Distinguish assistant claims from observed execution status. Do not claim user acceptance.>
## Open commitments
<What remains, blockers, and the next useful action; do not invent a commitment.>
## Source
<Copy session_id, run_id, source_seq, and project from the supplied data.>
"""


def register(mcp, *, project, recorder, writer, reader, provider, index, memory_lock):
    """Bind the real plugin APIs. All record creation goes through its writer."""
    destination_id = hashlib.sha256(json.dumps(
        ["memory-v1", str(Path(provider.root).resolve()), project]).encode()).hexdigest()

    def validate(capture_id, selected):
        if selected != project:
            raise ValueError("capture project differs from this server's configured project")
        if not re.fullmatch(r"[0-9a-f]{64}", capture_id):
            raise ValueError("invalid capture id")

    def encoded(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @mcp.tool()
    def capture_prepare(capture_id: str, project: str, transcript: str) -> str:
        """Prepare the installed recorder prompt. This operation writes no memory."""
        validate(capture_id, project)
        if len(transcript.encode()) > 65536:
            raise ValueError("transcript exceeds 65536 bytes")
        prompt = recorder.build_prompt(transcript, f"Project: {project}\nCapture: {capture_id}", TEMPLATE)
        return encoded(dict(version=1, idempotent=True, prompt=prompt,
                            skip_sentinel=recorder.SKIP_SENTINEL, destination=destination_id))

    @mcp.tool()
    def capture_write(capture_id: str, project: str, record: str, destination: str) -> str:
        """Idempotently write a prepared record, then verify the normal indexed read.

        Repeating an identical capture returns the same receipt. Reusing an ID
        for different content fails; it never overwrites an earlier record.
        """
        validate(capture_id, project)
        if destination != destination_id:
            raise ValueError("prepared capture belongs to another memory destination")
        if not record.strip() or record.strip() == recorder.SKIP_SENTINEL or len(record.encode()) > 16384:
            raise ValueError("expected a nonempty prepared record of at most 16384 bytes")
        digest = hashlib.sha256(record.encode()).hexdigest()
        name = f"harness-capture-{capture_id}"
        body = f"Harness capture: {capture_id}\nRecord SHA-256: {digest}\n\n{record}\n"

        def matches(entry):
            return (entry is not None and entry.name == name and entry.type == "project"
                    and entry.subject == project and entry.body == body)

        with memory_lock(Path(provider.root), context="harness.capture"):
            existing = provider.get(name, "project")
            if existing is not None:
                if not matches(existing):
                    raise ValueError("capture id already exists with different content")
            else:
                def write(text):
                    if text != record:
                        raise ValueError("recorder changed the prepared record")
                    writer.write(name=name, type="project", subject=project,
                                 description="Resident continuity: " + " ".join(record.split())[:180],
                                 body=body, provider=provider)

                # Generation was performed by Harness, under its inference policy.
                # Use the installed recorder's injected-runner/writer contract;
                # do not launch its default external CLI model.
                result = recorder.record("", "", TEMPLATE, runner=lambda _: record, writer=write)
                if not result.written:
                    raise ValueError("recorder did not write the prepared record")
            if not matches(provider.get(name, "project")):
                raise ValueError("memory readback does not match the prepared record")
            if not any(e.name == name and e.type == "project" for e in index.read(provider.root)):
                # A process can die after put() but before index.append(). The
                # plugin owns both the scan and index format; reuse its recovery.
                index.rebuild_from_scan(provider.root)
            if not matches(reader.get(name, "project")):
                raise ValueError("normal memory reader cannot retrieve the written record")
        return encoded(dict(version=1, capture_id=capture_id, project=project,
                            record_sha256=digest, name=name, status="saved", destination=destination_id))

    return capture_prepare, capture_write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-plugin", type=Path, required=True)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    lib = args.memory_plugin.resolve() / "lib"
    if not (lib / "session_recorder.py").is_file():
        parser.error("--memory-plugin must contain lib/session_recorder.py")
    sys.path.insert(0, str(lib))
    from mcp.server.fastmcp import FastMCP
    import bounded_read
    import index
    import memory_reader
    import memory_writer
    import session_recorder
    from config import resolve_vault_root
    from lock import memory_lock
    from providers.vault import VaultProvider

    server = FastMCP("resident-memory")
    register(server, project=args.project, recorder=session_recorder, writer=memory_writer,
             reader=memory_reader, provider=VaultProvider(resolve_vault_root()),
             index=index, memory_lock=memory_lock)

    @server.tool()
    def memory_search(query: str = "", type: str = "", subject: str = "", offset: int = 0,
                      limit: int = 10, max_bytes: int = 4096) -> str:
        """Search the normal memory index with the installed bounded reader."""
        return bounded_read.search(query, type, subject, offset, limit, max_bytes)

    @server.tool()
    def memory_read(name: str, type: str, offset: int = 0, max_bytes: int = 4096,
                    revision: str = "") -> str:
        """Read a normal memory entry, with revision-bound pagination."""
        return bounded_read.read(name, type, offset, max_bytes, revision)

    server.run()


if __name__ == "__main__":
    main()
