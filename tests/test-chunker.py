#!/usr/bin/env python
# ===========================================================================
# CHUNKER TESTS — THE RULES, WITHOUT A CLUSTER OR A MODEL
# ===========================================================================
#   studio/backend/.venv/bin/python tests/test-chunker.py
#   (any python3 works — chunker.py is stdlib only)
#
# studio/images/rag-toolkit/chunker.py is the one piece of RAG logic that is
# pure, deterministic and dependency-free, and it is also the piece that decides
# retrieval quality. Testing it here rather than only inside the ingestion Job
# means a boundary bug is caught in a second on a laptop instead of after a
# 900-second Job on a cluster.
#
# THE PROPERTIES BELOW ARE THE CONTRACT THE RAG SERVICE'S CITATIONS DEPEND ON.
# A chunk id is what an answer cites; if ids moved between runs, every citation
# a PI had written down would point somewhere else.
# ===========================================================================

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "studio" / "images" / "rag-toolkit"))

from chunker import (  # noqa: E402
    MAX_CHARS,
    OVERLAP_CHARS,
    chunk_corpus,
    chunk_text,
    paragraphs,
)

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {label}")
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m  {label}")
        if detail:
            print(f"        {detail}")


PROSE = "\n\n".join(
    f"Paragraph {i}. " + " ".join(f"word{j}" for j in range(30)) for i in range(10)
)


def main() -> int:
    print("\n==> splitting\n")

    check("blank lines separate paragraphs", len(paragraphs(PROSE)) == 10,
          str(len(paragraphs(PROSE))))
    check("whitespace-only paragraphs are dropped",
          paragraphs("a\n\n   \n\nb") == ["a", "b"], str(paragraphs("a\n\n   \n\nb")))
    check("CRLF chunks identically to LF — a Windows corpus must not shift ids",
          chunk_text(PROSE, "s.md") == chunk_text(PROSE.replace("\n", "\r\n"), "s.md"))

    print("\n==> chunk size\n")

    chunks = chunk_text(PROSE, "notes.md")
    check("produces more than one chunk from a long document", len(chunks) > 1, str(len(chunks)))
    over = [c["chunk_id"] for c in chunks if len(c["text"]) > MAX_CHARS]
    check(f"every chunk is <= {MAX_CHARS} characters", not over, str(over))

    # One "word" longer than the limit is emitted WHOLE rather than cut. Cutting
    # it would produce two pieces that are each meaningless and neither
    # searchable — a URL or a base64 blob halfway through.
    single = chunk_text("a" * 1500, "blob.txt")
    check("an unsplittable single word is kept whole, not cut mid-word",
          len(single) == 1 and len(single[0]["text"]) == 1500,
          f"{len(single)} chunk(s)")

    print("\n==> never mid-word\n")

    tokens = set(re.findall(r"\S+", PROSE))
    bad_start = [c["chunk_id"] for c in chunks if c["text"].split()[0] not in tokens]
    bad_end = [c["chunk_id"] for c in chunks if c["text"].split()[-1] not in tokens]
    check("no chunk STARTS with a partial word", not bad_start, str(bad_start))
    check("no chunk ENDS with a partial word", not bad_end, str(bad_end))

    long_para = chunk_text(" ".join(f"tok{i}" for i in range(400)), "long.txt")
    long_tokens = {f"tok{i}" for i in range(400)}
    split_bad = [
        c["chunk_id"] for c in long_para
        if c["text"].split()[0] not in long_tokens or c["text"].split()[-1] not in long_tokens
    ]
    check("hard-splitting an over-long paragraph also respects word boundaries",
          not split_bad, str(split_bad))

    print("\n==> overlap\n")

    # THE FAILURE OVERLAP PREVENTS: a fact that straddles a boundary, where the
    # question matches the end of one chunk and the answer is the start of the
    # next, and neither chunk alone contains both.
    overlapping = 0
    for previous, following in zip(chunks, chunks[1:]):
        tail_words = previous["text"].split()[-6:]
        if tail_words and " ".join(tail_words) in following["text"]:
            overlapping += 1
    check("consecutive chunks share a tail",
          overlapping >= len(chunks) - 2,
          f"{overlapping} of {len(chunks) - 1} boundaries overlap")
    check(f"the overlap is about {OVERLAP_CHARS} characters, not a whole chunk",
          OVERLAP_CHARS < MAX_CHARS // 2)

    print("\n==> ids and citations\n")

    check("chunk_id is <source>#<index within that file>",
          [c["chunk_id"] for c in chunks[:3]] == ["notes.md#0", "notes.md#1", "notes.md#2"],
          str([c["chunk_id"] for c in chunks[:3]]))
    check("source is the basename a PI recognises, not a container path",
          all(c["source"] == "notes.md" for c in chunks))
    check("every chunk carries exactly text, source and chunk_id",
          all(set(c) == {"text", "source", "chunk_id"} for c in chunks))
    check("ids are unique within a document",
          len({c["chunk_id"] for c in chunks}) == len(chunks))

    print("\n==> determinism\n")

    check("the same input always produces the same chunks",
          chunk_text(PROSE, "notes.md") == chunks)

    # Indices are PER FILE, so adding a document does not renumber the chunks of
    # every document after it — which is what would invalidate citations a PI
    # had already written down.
    demo = REPO / "templates" / "catalog" / "rag-stack" / "demo-corpus"
    if demo.is_dir():
        first_chunks, first_sources = chunk_corpus(str(demo))
        second_chunks, _ = chunk_corpus(str(demo))
        check("the demo corpus chunks identically on a second run",
              first_chunks == second_chunks)
        check("the demo corpus produces chunks from every file",
              len(first_sources) == 4 and len(first_chunks) > 10,
              f"{len(first_sources)} file(s), {len(first_chunks)} chunk(s)")
        print(f"        (demo corpus: {len(first_sources)} files -> {len(first_chunks)} chunks)")
        check("a file's ids are numbered within that file",
              all(
                  c["chunk_id"] == f"{c['source']}#{i}"
                  for source in first_sources
                  for i, c in enumerate([x for x in first_chunks if x["source"] == source])
              ))
    else:
        print("  (demo corpus not found — skipping corpus assertions)")

    print(f"\n==> {PASS} passed, {FAIL} failed\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
