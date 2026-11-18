"""CLI commands for research ingestion, claims and claim-to-code mapping.

Registration is explicit so ``maya --help`` always exposes the Phase 2 workflow
(MWB-001). Handlers are thin: all behaviour lives in the research and mapping
services.
"""

from __future__ import annotations

import argparse
from typing import Any

from mayaworkbench.domain.enums import ClaimConfidence
from mayaworkbench.errors import UsageError
from mayaworkbench.research.ingestion import MetadataOverrides


def _services(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from mayaworkbench.cli.main import services

    return services(args)


def register_research_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    research = sub.add_parser("research", help="Register documents and extract source-linked claims")
    research_sub = research.add_subparsers(dest="research_command", metavar="<action>")

    add = research_sub.add_parser("add", help="Register a local file, URL or DOI")
    add.add_argument("path", nargs="?", help="Local document path")
    add.add_argument("--project")
    source = add.add_mutually_exclusive_group()
    source.add_argument("--url", help="Fetch a public document URL")
    source.add_argument("--doi", help="Fetch a DOI resolver document")
    add.add_argument("--title")
    add.add_argument("--author", action="append", default=[])
    add.add_argument("--organization")
    add.add_argument("--published")
    add.add_argument("--license")
    add.add_argument("--metadata-doi")
    add.add_argument("--source-url")
    add.add_argument("--document-key")
    add.add_argument("--version-of", help="Register these bytes as a new version")
    add.set_defaults(func=cmd_research_add)

    extract = research_sub.add_parser("extract", help="Extract claims from a document")
    extract.add_argument("document_id")
    extract.add_argument("--model", action="store_true", help="Use the configured model")
    extract.add_argument("--section", help="Restrict to matching section titles")
    extract.add_argument(
        "--min-confidence",
        choices=[item.value for item in ClaimConfidence],
        default=ClaimConfidence.LOW.value,
    )
    extract.add_argument("--max-claims", type=int)
    extract.add_argument("--force", action="store_true")
    extract.set_defaults(func=cmd_research_extract)

    documents = research_sub.add_parser("documents", help="List registered documents")
    documents.add_argument("--project")
    documents.add_argument("--limit", type=int, default=50)
    documents.set_defaults(func=cmd_research_documents)

    show = research_sub.add_parser("show", help="Show one document and its claims")
    show.add_argument("document_id")
    show.set_defaults(func=cmd_research_show)

    claims = research_sub.add_parser("claims", help="List extracted claims")
    claims.add_argument("--project")
    claims.add_argument("--document")
    claims.add_argument("--limit", type=int, default=100)
    claims.set_defaults(func=cmd_research_claims)

    memo = research_sub.add_parser("memo", help="Build a source-linked research memo")
    memo.add_argument("--project")
    memo.add_argument("--document")
    memo.add_argument("--snapshot")
    memo.add_argument("--title")
    memo.add_argument("--deviations", action="append", default=[])
    memo.add_argument("--out", help="Write the memo Markdown to this path")
    memo.set_defaults(func=cmd_research_memo)

    mapping = sub.add_parser("map", help="Map claims to code in a pinned snapshot")
    map_sub = mapping.add_subparsers(dest="map_command", metavar="<action>")

    run = map_sub.add_parser("run", help="Map every claim and store the matrix artifact")
    run.add_argument("--project")
    run.add_argument("--repo")
    run.add_argument("--snapshot")
    run.add_argument("--model", action="store_true")
    run.set_defaults(func=cmd_map_run)

    matrix = map_sub.add_parser("matrix", help="Rebuild and show the claim-to-code matrix")
    matrix.add_argument("--project")
    matrix.add_argument("--repo")
    matrix.add_argument("--snapshot")
    matrix.set_defaults(func=cmd_map_matrix)

    claim = map_sub.add_parser("claim", help="Map one claim against a snapshot")
    claim.add_argument("claim_id")
    claim.add_argument("--project")
    claim.add_argument("--repo")
    claim.add_argument("--snapshot")
    claim.add_argument("--model", action="store_true")
    claim.set_defaults(func=cmd_map_claim)


def _overrides(args: argparse.Namespace) -> MetadataOverrides:
    return MetadataOverrides(
        title=args.title,
        authors=args.author or None,
        organization=args.organization,
        published_at=args.published,
        doi=args.metadata_doi,
        source_url=args.source_url,
        license=args.license,
    )


def _ingest_payload(result) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "document": result.document.model_dump(mode="json"),
        "created": result.created,
        "passages": len(result.passages),
        "warnings": result.warnings,
        "report": result.report.model_dump(mode="json") if result.report else None,
    }


def cmd_research_add(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    project = svc.resolve_project(args.project)
    sources = [bool(args.path), bool(args.url), bool(args.doi)]
    if sum(sources) != 1:
        raise UsageError(
            "Provide exactly one source: a path, --url or --doi",
            hint='For example: maya research add paper.md --project demo',
        )
    overrides = _overrides(args)
    if args.url:
        result = svc.ingestion.ingest_url(
            project,
            args.url,
            overrides=overrides,
            document_key=args.document_key,
            version_of=args.version_of,
        )
    elif args.doi:
        result = svc.ingestion.ingest_doi(
            project,
            args.doi,
            overrides=overrides,
            document_key=args.document_key,
            version_of=args.version_of,
        )
    else:
        result = svc.ingestion.ingest_file(
            project,
            args.path,
            overrides=overrides,
            document_key=args.document_key,
            version_of=args.version_of,
        )
    payload = _ingest_payload(result)
    if console.json_mode:
        console.json(payload)
        return 0
    document = result.document
    console.heading(f"{'Registered' if result.created else 'Already registered'}: {document.title}")
    console.kv(
        [
            ("document", document.id),
            ("version", document.version),
            ("media", document.media_kind.value),
            ("origin", document.origin.value),
            ("sha256", document.content_sha256),
            ("pages", document.page_count or "—"),
            ("passages", len(result.passages)),
            ("extraction", document.extraction_status.value),
        ]
    )
    for warning in result.warnings:
        console.warning(warning)
    for warning in document.extraction_warnings:
        console.warning(warning)
    console.print("")
    console.print(f"Next: maya research extract {document.id}")
    return 0


def cmd_research_extract(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    summary = svc.claim_service.extract(
        args.document_id,
        use_model=args.model,
        force=args.force,
        section=args.section,
        min_confidence=ClaimConfidence(args.min_confidence),
        max_claims=args.max_claims,
    )
    payload = summary.model_dump(mode="json")
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Claims for {summary.document_id}")
    console.kv(
        [
            ("method", summary.method),
            ("algorithm", summary.algorithm_version),
            ("claims", len(summary.claims)),
            ("created", summary.claims_created),
            ("preserved", summary.claims_preserved),
            ("dropped", summary.claims_dropped),
            ("artifact", summary.artifact_id or "—"),
        ]
    )
    console.table(
        ["claim", "type", "confidence", "review", "title"],
        [
            [
                claim.id,
                claim.claim_type.value,
                claim.confidence.value,
                claim.review_status.value,
                claim.title,
            ]
            for claim in summary.claims
        ],
        max_column=60,
    )
    for warning in summary.warnings:
        console.warning(warning)
    return 0


def cmd_research_documents(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    project = svc.resolve_project(args.project)
    documents = svc.documents.list_by_project(project.id, limit=args.limit)
    payload = {"documents": [item.model_dump(mode="json") for item in documents]}
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Research documents in {project.slug}")
    console.table(
        ["doc", "version", "media", "extraction", "pages", "title"],
        [
            [
                item.id,
                item.version,
                item.media_kind.value,
                item.extraction_status.value,
                item.page_count or "—",
                item.title,
            ]
            for item in documents
        ],
        max_column=55,
    )
    if not documents:
        console.note("No documents yet. Register one with `maya research add <path>`.")
    return 0


def cmd_research_show(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    document = svc.documents.get(args.document_id)
    passages = svc.passages.list_by_document(document.id)
    claims = svc.claims.list(document_id=document.id, limit=1000)
    payload = {
        "document": document.model_dump(mode="json"),
        "passages": len(passages),
        "claims": [claim.model_dump(mode="json") for claim in claims],
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(document.title)
    console.kv(
        [
            ("document", document.id),
            ("version", document.version),
            ("authors", ", ".join(document.authors) or "—"),
            ("license", document.license or "—"),
            ("doi", document.doi or "—"),
            ("sha256", document.content_sha256),
            ("passages", len(passages)),
            ("claims", len(claims)),
            ("extraction", document.extraction_status.value),
        ]
    )
    for warning in document.extraction_warnings:
        console.warning(warning)
    if claims:
        console.section("Claims")
        console.table(
            ["claim", "type", "confidence", "title"],
            [
                [claim.id, claim.claim_type.value, claim.confidence.value, claim.title]
                for claim in claims
            ],
            max_column=60,
        )
    return 0


def cmd_research_claims(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    project = svc.resolve_project(args.project)
    claims = svc.claim_service.list_claims(
        project_id=project.id, document_id=args.document, limit=args.limit
    )
    payload = {"claims": [claim.model_dump(mode="json") for claim in claims]}
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading("Research claims")
    console.table(
        ["claim", "type", "confidence", "review", "signals", "title"],
        [
            [
                claim.id,
                claim.claim_type.value,
                claim.confidence.value,
                claim.review_status.value,
                len(claim.expected_code_signals),
                claim.title,
            ]
            for claim in claims
        ],
        max_column=55,
    )
    return 0


def cmd_research_memo(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    project = svc.resolve_project(args.project)
    document = svc.documents.get(args.document) if args.document else None
    snapshot = None
    if args.snapshot:
        snapshot = svc.snapshots.get(args.snapshot)
    build = svc.memo_service.build(
        project,
        document=document,
        snapshot=snapshot,
        deviations=args.deviations or None,
        title=args.title,
    )
    payload = {
        "markdown": build.markdown,
        "artifact_id": build.artifact_id,
        "readiness": build.readiness.model_dump(mode="json"),
        "dag": build.dag.model_dump(mode="json"),
        "matrix": build.matrix.model_dump(mode="json") if build.matrix else None,
        "warnings": build.warnings,
    }
    if args.out:
        from pathlib import Path

        Path(args.out).expanduser().write_text(build.markdown, encoding="utf-8")
    if console.json_mode:
        console.json(payload)
        return 0
    console.print(build.markdown)
    for warning in build.warnings:
        console.warning(warning)
    console.note(f"memo artifact: {build.artifact_id}")
    return 0


def cmd_map_run(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    project = svc.resolve_project(args.project)
    _project, _repository, snapshot = svc.resolve_snapshot_context(
        project_identifier=args.project,
        repository_identifier=args.repo,
        snapshot_id=args.snapshot,
    )
    run = svc.mapping_service.map_project(
        project, snapshot, use_model=args.model
    )
    payload = {
        "matrix": run.matrix.model_dump(mode="json"),
        "artifact_id": run.artifact_id,
        "notes": run.notes,
        "outcomes": [
            {
                "claim_id": outcome.mapping.claim_id,
                "status": outcome.mapping.status.value,
                "reason": outcome.mapping.status_reason,
                "evidence": len(outcome.evidence),
            }
            for outcome in run.outcomes
        ],
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Claim-to-code matrix for {snapshot.identity_label}")
    console.kv(
        [
            ("snapshot", snapshot.id),
            ("claims", run.matrix.claims_total),
            ("counts", ", ".join(f"{k}={v}" for k, v in sorted(run.matrix.counts.items()))),
            ("artifact", run.artifact_id or "—"),
        ]
    )
    console.table(
        ["claim", "status", "evidence", "reason"],
        [
            [
                outcome.mapping.claim_id,
                outcome.mapping.status.value,
                len(outcome.evidence),
                outcome.mapping.status_reason,
            ]
            for outcome in run.outcomes
        ],
        max_column=60,
    )
    for note in run.notes:
        console.note(note)
    return 0


def cmd_map_matrix(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    project = svc.resolve_project(args.project)
    _project, _repository, snapshot = svc.resolve_snapshot_context(
        project_identifier=args.project,
        repository_identifier=args.repo,
        snapshot_id=args.snapshot,
    )
    matrix = svc.mapping_service.build_matrix(project, snapshot)
    payload = {"matrix": matrix.model_dump(mode="json")}
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"Claim-to-code matrix for {snapshot.identity_label}")
    console.kv(
        [
            ("claims", matrix.claims_total),
            ("mapped", len(matrix.mapped_rows)),
            ("unmapped", len(matrix.unmapped_claim_ids)),
            ("rejected", len(matrix.rejected_claim_ids)),
        ]
    )
    console.table(
        ["claim", "status", "evidence"],
        [
            [row.claim_id, row.mapping_status or "UNMAPPED", len(row.evidence)]
            for row in matrix.rows
        ],
        max_column=60,
    )
    return 0


def cmd_map_claim(args: argparse.Namespace) -> int:
    console = args.console
    svc = _services(args)
    claim = svc.claims.get(args.claim_id)
    project = svc.resolve_project(args.project or claim.project_id)
    _project, _repository, snapshot = svc.resolve_snapshot_context(
        project_identifier=project.slug,
        repository_identifier=args.repo,
        snapshot_id=args.snapshot,
    )
    outcome = svc.mapping_service.map_claim(claim, snapshot, use_model=args.model)
    payload = {
        "mapping": outcome.mapping.model_dump(mode="json"),
        "evidence": [item.model_dump(mode="json") for item in outcome.evidence],
        "notes": outcome.notes,
    }
    if console.json_mode:
        console.json(payload)
        return 0
    console.heading(f"{claim.title}: {outcome.mapping.status.value}")
    console.kv(
        [
            ("claim", claim.id),
            ("status", outcome.mapping.status.value),
            ("reason", outcome.mapping.status_reason),
            ("author", outcome.mapping.created_by.value),
            ("rule pack", outcome.mapping.rule_pack_version),
        ]
    )
    console.table(
        ["path", "lines", "relation", "strength"],
        [
            [
                item.path,
                f"{item.start_line}-{item.end_line}",
                item.relation.value,
                item.strength.value,
            ]
            for item in outcome.evidence
        ],
        max_column=55,
    )
    for gap in outcome.mapping.gaps:
        console.warning(gap)
    for note in outcome.notes:
        console.note(note)
    return 0


__all__ = ["register_research_commands"]
