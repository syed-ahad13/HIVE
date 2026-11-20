# ===========================================================================
# RAG TOOLKIT — THE CHUNKER
# ===========================================================================
# Turns a folder of documents into the passages that get embedded and indexed.
#
# ---------------------------------------------------------------------------
# WHY THIS IS DELIBERATELY BORING
# ---------------------------------------------------------------------------
# Chunking is where RAG quality is won or lost, and it is also where it is
# easiest to be clever in ways nobody can debug. This splitter is entirely
# deterministic: the same folder always produces the same chunks, in the same
# order, with the same ids. That matters more than sophistication here for three
# reasons:
#
#   1. A CHUNK ID IS A CITATION. Every answer the RAG service returns names the
#      chunks it used, and a PI checking one must be able to find the same text
#      again. Ids that move between ingestion runs make citations worthless.
#   2. RE-INGESTION MUST BE COMPARABLE. The Job recreates the collection each
#      run; if the chunking drifted, "the answers got worse" could never be
#      attributed to the corpus rather than the splitter.
#   3. IT IS TESTABLE WITHOUT A CLUSTER, WITHOUT A MODEL AND WITHOUT A NETWORK.
#      Pure stdlib, pure functions — see tests/test-chunker.py.
#
# ---------------------------------------------------------------------------
# THE RULES, AND WHY EACH ONE IS THERE
# ---------------------------------------------------------------------------
#   SPLIT ON BLANK LINES FIRST. A blank line is the one structural signal that
#   means the same thing in Markdown and in plain text: "a new thought". It
#   survives round-tripping, needs no parser, and does not care about heading
#   syntax. Splitting on sentences instead would cut mid-list and mid-code-block.
#
#   THEN PACK GREEDILY UP TO 600 CHARACTERS. A chunk should be big enough to
#   contain an answer and small enough that several fit in the prompt. At topK=3
#   that is ~1,800 characters of context, comfortable inside the 4096-token
#   window the pinned runtime provides (host/serving/runtimes/llama-cpp-rag.yaml).
#   Packing whole paragraphs rather than cutting at exactly 600 keeps most
#   chunks semantically whole.
#
#   OVERLAP ~100 CHARACTERS. The failure overlap prevents is an answer that
#   straddles a boundary: the question matches the end of chunk 4, the answer is
#   the first sentence of chunk 5, and neither chunk alone contains both. A tail
#   of the previous chunk carried into the next means a boundary-straddling fact
#   appears whole in at least one chunk.
#
#   NEVER SPLIT MID-WORD. Both when hard-splitting an over-long paragraph and
#   when taking the overlap tail. A chunk beginning "…ferenceService is
#   reconciled" embeds badly and reads worse when quoted back as a citation.
# ===========================================================================

from __future__ import annotations

import os
import re
from typing import Iterator

# The two extensions the wizard's help text promises. Not a general document
# pipeline: a PDF or a .docx would need extraction, which is a different problem
# with different failure modes, and quietly ignoring one is worse than saying so
# — ingest.py reports the files it skipped.
TEXT_EXTENSIONS: tuple[str, ...] = (".md", ".txt")

# Characters, not tokens. Tokens would be more accurate and would require the
# tokenizer, which would make this module depend on the embedding model and stop
# it being testable on its own. For English prose the ratio is roughly 4:1, so
# 600 characters is ~150 tokens and the arithmetic above holds with room to spare.
MAX_CHARS = 600
OVERLAP_CHARS = 100

_BLANK_LINE = re.compile(r"\n[ \t]*\n")


def paragraphs(text: str) -> list[str]:
    """Split on blank lines. Whitespace-only paragraphs are dropped.

    Line endings are normalised first so a corpus authored on Windows chunks
    identically to the same corpus authored on macOS — otherwise every `\\r\\n`
    would be a character of difference and the chunk boundaries would shift.
    """
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    return [p.strip() for p in _BLANK_LINE.split(normalised) if p.strip()]


def _split_long(paragraph: str, limit: int = MAX_CHARS) -> list[str]:
    """Break one over-long paragraph into pieces, always at a word boundary.

    Reached by a wall-of-text paragraph or a long code block. Splitting on
    whitespace is enough: a single "word" longer than the limit (a URL, a base64
    blob) is emitted whole and over-length rather than cut, because cutting it
    would produce two pieces that are each meaningless and neither searchable.
    """
    words = paragraph.split()
    if not words:
        return []

    pieces: list[str] = []
    current: list[str] = []
    length = 0
    for word in words:
        # +1 for the space that will join it, except for the first word.
        addition = len(word) + (1 if current else 0)
        if current and length + addition > limit:
            pieces.append(" ".join(current))
            current, length = [word], len(word)
        else:
            current.append(word)
            length += addition
    if current:
        pieces.append(" ".join(current))
    return pieces


def _overlap_tail(text: str, size: int = OVERLAP_CHARS) -> str:
    """The last `size`-ish characters of a chunk, trimmed to a word boundary.

    Returns "" when the chunk is shorter than the overlap: carrying a whole
    chunk forward as the next chunk's prefix would duplicate it entirely rather
    than overlap it.
    """
    if size <= 0 or len(text) <= size:
        return ""
    tail = text[-size:]
    # Drop the leading partial word. If the whole tail is one word there is no
    # space to cut at, and the tail is dropped rather than cut mid-word.
    space = tail.find(" ")
    return tail[space + 1 :].strip() if space != -1 else ""


def chunk_text(text: str, source: str) -> list[dict]:
    """One document's chunks, in order.

    Returns [{text, source, chunk_id}]. `chunk_id` is "<source>#<index>", which
    is what an answer cites and therefore what a PI uses to find the passage
    again — hence an index within the file rather than a global counter, so
    adding a document does not renumber every chunk after it.
    """
    chunks: list[dict] = []
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        body = buffer.strip()
        if body:
            chunks.append(
                {
                    "text": body,
                    "source": source,
                    "chunk_id": f"{source}#{len(chunks)}",
                }
            )
        buffer = ""

    for para in paragraphs(text):
        # An over-long paragraph is pre-split so the packing loop below only
        # ever handles pieces that can fit.
        for piece in _split_long(para) if len(para) > MAX_CHARS else [para]:
            if not buffer:
                buffer = piece
                continue
            if len(buffer) + 2 + len(piece) <= MAX_CHARS:
                # Rejoined with a blank line, so a chunk reads as the document
                # did and a quoted citation keeps its paragraph structure.
                buffer = f"{buffer}\n\n{piece}"
                continue
            tail = _overlap_tail(buffer)
            flush()
            # The overlap only earns its place if the new chunk still has room
            # for real content after it; otherwise the tail is dropped and the
            # chunk starts clean.
            buffer = f"{tail}\n\n{piece}" if tail and len(tail) + 2 + len(piece) <= MAX_CHARS else piece

    flush()
    return chunks


def chunk_file(path: str) -> list[dict]:
    """One file's chunks. `source` is the BASENAME, not the path.

    The basename is what a PI recognises and what gets shown as a citation; the
    absolute path inside a container ("/mnt/scratch/corpus/notes.md") is an
    implementation detail of where the corpus was staged.

    Undecodable bytes are replaced rather than raising: one file with a stray
    byte must not abort an ingestion of forty.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return chunk_text(handle.read(), os.path.basename(path))


def iter_corpus(directory: str) -> Iterator[str]:
    """Every indexable file under `directory`, in a STABLE order.

    Sorted at every level, so two ingestion runs of the same corpus visit files
    in the same order and produce the same ids. `os.walk` alone does not
    guarantee that — it returns whatever order the filesystem gives.

    Hidden files and directories are skipped: `.DS_Store` and `.git` are not
    part of anybody's corpus, and a macOS user zipping a folder will include
    the first without noticing.
    """
    for root, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            if name.lower().endswith(TEXT_EXTENSIONS):
                yield os.path.join(root, name)


def chunk_corpus(directory: str) -> tuple[list[dict], list[str]]:
    """Chunk a whole folder.

    Returns (chunks, sources) — the chunks in document order, and the list of
    files that produced them. The second is not derivable from the first: a file
    that is present but empty produces no chunks, and the ingestion log should
    say it was read rather than leave it unmentioned.
    """
    chunks: list[dict] = []
    sources: list[str] = []
    for path in iter_corpus(directory):
        sources.append(os.path.basename(path))
        chunks.extend(chunk_file(path))
    return chunks, sources
