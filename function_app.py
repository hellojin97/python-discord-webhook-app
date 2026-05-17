import logging
import os

import azure.functions as func
import httpx

from discord_relay.webhook import build_discord_payload, send_to_discord

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

@app.route(route="notify", methods=["POST"])
def relay_to_discord(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Lakeflow notification received.")

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logging.error("DISCORD_WEBHOOK_URL is not configured.")
        return func.HttpResponse(
            "Server misconfigured: DISCORD_WEBHOOK_URL missing",
            status_code=500,
        )
    
    try:
        event = req.get_json()
    except ValueError:
        logging.warning("Invalid JSON body.")
        return func.HttpResponse("Invalid JSON body", status_code=400)
    
    payload = build_discord_payload(event)

    try:
        send_to_discord(webhook_url, payload)
    except httpx.HTTPStatusError as e:
        logging.error(
            "Discord returned %d: %s", e.response.status_code, e.response.text
        )
        return func.HttpResponse(
            f"Discord rejected the webhook: {e.response.status_code}",
            status_code=502,
        )
    except httpx.HTTPError:
        logging.exception("Network error while calling Discord.")
        return func.HttpResponse("Network error calling Discord", status_code=502)
    
    return func.HttpResponse("ok", status_code=200)