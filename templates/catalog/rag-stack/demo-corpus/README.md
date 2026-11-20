# The demo corpus

These four files are indexed when a PI deploys the RAG template without
uploading anything of their own. They are here so the template can be
demonstrated end to end on a laptop with no internet and no documents to hand,
and so the very first question someone asks has a checkable answer.

**They are about HIVE itself, deliberately.** A demo corpus of generic sample
text proves the machinery works but teaches nothing, and nobody in the room can
tell a good answer from a fluent wrong one. Everyone watching this demo knows
what HIVE is, so everyone can judge whether the answer is right — which is the
only way to see that retrieval is actually happening rather than the model
answering from memory.

The content is drawn from `CLAUDE.md` and `DEVIATIONS.md`. It is a summary, not
a copy: those two files are the authority, and if they disagree with these, they
win.

Good questions to try:

- *What is a vCluster used for in HIVE?*
- *How many KServe installations are there?*
- *What is the difference between always-on and on-demand?*
- *How do tenants ask for a GPU?*
- *What happens when a project's allocation runs out?*
- *Who is the Queen of England?* — should be refused, and that refusal is the
  most important thing in the demo.
