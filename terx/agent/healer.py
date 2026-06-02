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

class SelfHealer:
    """
    LLM-powered self-healing fallback for broken CDP replays.
    Uses litellm to reason about the DOM changes and propose new CDP parameters.
    """
    def __init__(self, model_name: str = "gpt-4o"):
        self.model_name = model_name

    async def heal_command(self, failed_method: str, old_params: dict, current_dom: list, task_desc: str) -> Optional[dict]:
        """
        Takes the failed command and the current DOM state,
        and asks the LLM to fix the parameters (e.g., providing a new backendNodeId).
        """
        if not HAS_LITELLM:
            logger.warning("litellm is not installed. Self-healing is disabled.")
            return None
            
        if not os.environ.get("OPENAI_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
            logger.warning("No API key found for self-healing (needs OPENAI_API_KEY or ANTHROPIC_API_KEY).")
            return None

        prompt = f"""
        You are an autonomous browser agent memory layer. A previously recorded sequence failed during replay due to DOM drift.
        
        Task Context: {task_desc}
        Failed CDP Method: {failed_method}
        Old Parameters: {json.dumps(old_params)}
        
        Current DOM State (Interactable Elements):
        {json.dumps([{"id": el.id, "role": el.role, "label": el.label, "backendDOMNodeId": el.backend_dom_id} for el in current_dom], indent=2)}
        
        Your job is to find the correct new parameters for this CDP method.
        If it was a click or focus on a specific element, identify the new element's backendNodeId from the current DOM based on the role and label that best matches the intent.
        
        Return ONLY valid JSON with the new parameters. Do not include markdown blocks.
        """

        try:
            response = await litellm.acompletion(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            content = response.choices[0].message.content
            # Clean up markdown if present
            if content.startswith("```json"):
                content = content.strip()[7:-3]
            elif content.startswith("```"):
                content = content.strip()[3:-3]
                
            return json.loads(content)
        except Exception as e:
            logger.error("Self-healing prediction failed: %s", e)
            return None
