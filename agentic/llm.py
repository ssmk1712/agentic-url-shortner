import json
import os

from app.config import settings

REQUIREMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "ambiguities": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "clarifying_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "intent",
        "ambiguities",
        "assumptions",
        "acceptance_criteria",
        "risks",
        "clarifying_questions",
    ],
    "additionalProperties": False,
}


def analyze_requirement(requirement: str) -> dict:
    """Use GPT-4o for structured analysis of genuinely ambiguous requirements.

    The SDK is imported lazily so the URL-shortener product plane does not depend on
    OpenAI at import time. Secrets are read from the environment and never added to run state.
    """
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required for LLM-assisted requirement analysis")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("Install dependencies with: pip install -r requirements.txt") from exc

    client = OpenAI(timeout=settings.openai_timeout_seconds)
    response = client.chat.completions.create(
        model=settings.openai_model,
        temperature=0.2,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "requirement_analysis",
                "strict": True,
                "schema": REQUIREMENT_SCHEMA,
            },
        },
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a senior requirements analyst. Do not silently resolve ambiguity. "
                    "Identify intent, ambiguities, explicit assumptions, testable acceptance criteria, "
                    "engineering/security risks, and clarifying questions. Keep every item concrete."
                ),
            },
            {"role": "user", "content": requirement},
        ],
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("LLM returned an empty requirement analysis")
    analysis = json.loads(content)
    if not isinstance(analysis, dict):
        raise RuntimeError("LLM requirement analysis must be a JSON object")
    return analysis
