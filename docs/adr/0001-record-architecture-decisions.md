# ADR 0001: Record architecture decisions

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
AegisDesk is built to practise enterprise agent architecture. The reasoning behind a decision (why LangGraph, why OPA, why a deterministic policy layer) matters as much as the code, and it gets lost if it only lives in chat history or commit messages.

## Decision
Record significant decisions as short Architecture Decision Records in `docs/adr/`, numbered sequentially, using the format: Context, Decision, Consequences, Alternatives considered.

An ADR is written when a decision introduces a framework or infrastructure component, sets a security or governance boundary, or would be expensive to reverse.

Accepted ADRs are not edited to change their meaning. A later ADR supersedes an earlier one and says so.

## Consequences
* Every milestone that introduces a framework ships with an ADR explaining the choice.
* Reviewers can see *why* a structure exists before proposing to change it.

## Alternatives considered
* **Design notes in the README:** these grow unstructured and get rewritten, which loses the history.
* **No written record:** decisions become folklore.
