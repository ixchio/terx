import os
import json
import logging
from typing import Optional

try:
    import litellm

    HAS_LITELLM = True
except ImportError:
    HAS_LITELLM = False

logger = logging.getLogger(__name__)


def _sanitize_dom_for_llm(elements: list) -> list[dict]:
    """
    Sanitize DOM elements before sending to LLM.

    Only includes semantic_name (stable aria-label/placeholder), never current_value
    which may contain sensitive data like passwords, tokens, PII.
    """
    return [
        {
            "id": el.id,
            "role": el.role,
            "semantic_name": el.semantic_name,
            "backendDOMNodeId": el.backend_dom_id,
        }
        for el in elements
    ]


class SelfHealer:
    """
    LLM-powered self-healing fallback for broken CDP replays.
    Uses litellm to reason about the DOM changes and propose new CDP parameters.
    """

    def __init__(self, model_name: str = "gpt-4o"):
        self.model_name = model_name

    async def heal_command(
        self, failed_method: str, old_params: dict, current_dom: list, task_desc: str
    ) -> Optional[dict]:
        """
        Takes the failed command and the current DOM state,
        and asks the LLM to fix the parameters (e.g., providing a new backendNodeId).
        """
        if not HAS_LITELLM:
            logger.warning("litellm is not installed. Self-healing is disabled.")
            return None

        if not os.environ.get("OPENAI_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
            logger.warning(
                "No API key found for self-healing (needs OPENAI_API_KEY or ANTHROPIC_API_KEY)."
            )
            return None

        # Sanitize DOM: only send semantic_name, never current_value (which may contain PII)
        sanitized_dom = _sanitize_dom_for_llm(current_dom)

        prompt = f"""
        You are an autonomous browser agent memory layer. A previously recorded sequence failed during replay due to DOM drift.
        
        Task Context: {task_desc}
        Failed CDP Method: {failed_method}
        Old Parameters: {json.dumps(old_params)}
        
        Current DOM State (Interactable Elements):
        {json.dumps(sanitized_dom, indent=2)}
        
        Your job is to find the correct new parameters for this CDP method.
        If it was a click or focus on a specific element, identify the new element's backendNodeId from the current DOM based on the role and semantic_name that best matches the intent.
        
        Return ONLY valid JSON with the new parameters. Do not include markdown blocks.
        """

        try:
            response = await litellm.acompletion(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content.strip()
            # Clean up markdown if present
            if content.startswith("```json"):
                # Remove first and last lines
                content = "\n".join(content.split("\n")[1:-1])
            elif content.startswith("```"):
                # Remove first and last lines
                content = "\n".join(content.split("\n")[1:-1])

            return json.loads(content.strip())
        except Exception as e:
            logger.error("Self-healing prediction failed: %s", e)
            return None
