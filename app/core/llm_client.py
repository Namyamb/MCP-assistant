import logging
import requests
from app.core.config import LM_STUDIO_URL, MODEL_NAME, LLM_TEMPERATURE, LLM_TIMEOUT

logger = logging.getLogger(__name__)

def call_model(messages):
    resp = requests.post(LM_STUDIO_URL, json={
        "model": MODEL_NAME, "messages": messages, "temperature": LLM_TEMPERATURE
    }, timeout=LLM_TIMEOUT)
    if not resp.ok:
        logger.error("LM Studio error %s: %s", resp.status_code, resp.text[:300])
    resp.raise_for_status()
    data = resp.json()
    served_model = data.get("model", "unknown")
    if served_model != MODEL_NAME:
        logger.warning("Requested '%s' but LM Studio is serving '%s'", MODEL_NAME, served_model)
    return data
