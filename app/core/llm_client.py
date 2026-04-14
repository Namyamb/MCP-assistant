import requests
from app.core.config import LM_STUDIO_URL, MODEL_NAME, LLM_TEMPERATURE, LLM_TIMEOUT

def call_model(messages):
    resp = requests.post(LM_STUDIO_URL, json={
        "model": MODEL_NAME, "messages": messages, "temperature": LLM_TEMPERATURE
    }, timeout=LLM_TIMEOUT)
    resp.raise_for_status()
    return resp.json()
