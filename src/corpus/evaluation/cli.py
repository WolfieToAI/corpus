"""``corpus-eval`` command line.

    corpus-eval retrieval [--dataset eval/dataset] [--out eval/results]
    corpus-eval answers   [--limit N]            # needs LLM_API_KEY and LLM_MODEL

Uses DATABASE_URL and the usual settings (embedding provider, reranker...). The corpus is
indexed in a temporary tenant that is deleted afterwards; migrations are applied first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from corpus.config import get_settings
from corpus.core.embeddings import build_embedder
from corpus.core.generation import ChatCompletionsGenerator
from corpus.core.reranking import build_reranker
from corpus.core.rewriting import LLMQueryRewriter, QueryRewriter
from corpus.db.session import create_engine, create_sessionmaker, run_migrations
from corpus.evaluation import answer_eval, retrieval_eval
from corpus.evaluation.dataset import load_dataset
from corpus.llm import build_llm_client


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    dataset = load_dataset(Path(args.dataset))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - one-off, before any I/O starts

    await run_migrations(settings.database_url)
    engine = create_engine(settings.database_url)
    sm = create_sessionmaker(engine)
    embedder = build_embedder(settings)
    reranker = build_reranker(settings)
    client = build_llm_client(settings)
    model = settings.llm_model or ""
    rewriter: QueryRewriter | None = LLMQueryRewriter(client, model=model) if client else None

    try:
        if args.command == "retrieval":
            report = await retrieval_eval.evaluate_retrieval(
                sm, embedder, settings, dataset, reranker=reranker, rewriter=rewriter
            )
            markdown = retrieval_eval.render_markdown(report)
            payload = report.to_dict()
        else:
            if client is None:
                print("corpus-eval answers needs LLM_API_KEY and LLM_MODEL", file=sys.stderr)
                return 2
            generator = ChatCompletionsGenerator(
                client,
                model=model,
                max_tokens=settings.llm_max_tokens,
                temperature=settings.llm_temperature,
            )
            answers = await answer_eval.evaluate_answers(
                sm,
                embedder,
                settings,
                dataset,
                generator=generator,
                judge=answer_eval.LLMJudge(client, model),
                reranker=reranker,
                rewriter=rewriter,
                limit=args.limit,
            )
            markdown = answer_eval.render_markdown(answers)
            payload = answers.to_dict()
    finally:
        await engine.dispose()

    (out / f"{args.command}.json").write_text(json.dumps(payload, indent=2, default=str))
    (out / f"{args.command}.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="corpus-eval", description="Evaluate retrieval quality and answer quality."
    )
    parser.add_argument("command", choices=["retrieval", "answers"])
    parser.add_argument("--dataset", default="eval/dataset")
    parser.add_argument("--out", default="eval/results")
    parser.add_argument("--limit", type=int, default=None, help="answers: first N questions only")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
