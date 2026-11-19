"""Chat completion behind one interface (used for answers, query rewriting and briefings)."""

from typing import Protocol


class ChatModelError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class ChatModel(Protocol):
    model_name: str
    offline: bool

    async def complete(self, messages: list[dict], max_tokens: int = 800, json_mode: bool = False) -> str: ...


class OpenAIChat:
    offline = False

    def __init__(self, api_key: str, model: str) -> None:
        from openai import AsyncOpenAI

        self.model_name = model
        self._client = AsyncOpenAI(api_key=api_key, timeout=90, max_retries=2)

    async def complete(self, messages: list[dict], max_tokens: int = 800, json_mode: bool = False) -> str:
        import openai

        extra = {"response_format": {"type": "json_object"}} if json_mode else {}
        try:
            response = await self._client.chat.completions.create(
                model=self.model_name, messages=messages, max_completion_tokens=max_tokens, **extra
            )
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise ChatModelError("OpenAI rejected the API key", retryable=False) from exc
        except openai.NotFoundError as exc:
            raise ChatModelError(f"Model '{self.model_name}' not available", retryable=False) from exc
        except openai.OpenAIError as exc:
            raise ChatModelError(f"AI service temporarily unavailable ({type(exc).__name__})",
                                 retryable=True) from exc
        return response.choices[0].message.content or ""


class OfflineChat:
    """Offline stand-in that references the retrieved passages instead of generating an answer."""

    model_name = "offline-extractive"
    offline = True

    async def complete(self, messages: list[dict], max_tokens: int = 800, json_mode: bool = False) -> str:
        prompt = messages[-1]["content"]
        if prompt.startswith("Rewrite"):
            return prompt.rsplit("Latest question:", 1)[-1].strip()
        labels = [line.split("]")[0] + "]" for line in prompt.splitlines() if line.startswith("[S")]
        if not labels:
            return "I couldn't find this in your documents."
        return ("Offline mode (no AI): the most relevant passages are " + ", ".join(labels[:3])
                + ". Add an OpenAI key in .env for a written answer.")
