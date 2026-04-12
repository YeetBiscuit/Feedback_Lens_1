from abc import ABC, abstractmethod


class LLMProvider(ABC):
    name: str
    default_model: str

    @abstractmethod
    def generate(
        self,
        prompt: str,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> str:
        raise NotImplementedError
