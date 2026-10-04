"""Clinical Trials Question-to-Visualization service."""

import argparse
import asyncio
import json


def main() -> None:
    parser = argparse.ArgumentParser(prog="clinical-trials-viz")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    ask = sub.add_parser("ask", help="answer one question and print the JSON response")
    ask.add_argument("query")
    ask.add_argument("--fields", default="{}", help='structured fields as JSON, e.g. \'{"drug_name": "Keytruda"}\'')
    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "clinical_trials_viz.api:create_app", factory=True, host=args.host, port=args.port, reload=args.reload
        )
    else:
        asyncio.run(_ask(args.query, json.loads(args.fields)))


async def _ask(query: str, fields: dict) -> None:
    from httpx import ASGITransport, AsyncClient

    from clinical_trials_viz.api import create_app

    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://local") as client,
    ):
        response = await client.post("/v1/query", json={"query": query, **fields})
        print(json.dumps(response.json(), indent=2))
